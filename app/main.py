"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment
      variables take priority over .env).

Design notes
------------
* Instants are stored as BSON datetimes in UTC and exchanged at the API boundary as
  integer epoch milliseconds. A tz-aware client is used so every datetime read from
  MongoDB is timezone-aware UTC, matching what we write; the helpers still normalise
  defensively so a naive value can never cause a naive/aware comparison.
* Shift times and calendar dates are IST (UTC+05:30). A record's ``date`` is an IST
  calendar date, not an instant (R1).
* All reported rounding is half-up via ``decimal`` (R4, R8). Python's ``round`` is
  banker's rounding and is deliberately not used.
* Derived fields (work_hours, late_minutes, overtime_minutes, half_day) are computed
  only by this API, and analytics read the stored values rather than recomputing them.
"""
from __future__ import annotations

import calendar
import os
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()  # real environment variables win over .env (override=False is the default)

# --------------------------------------------------------------------------- #
# Configuration and database client
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True -> every datetime read from MongoDB is aware (UTC), matching what we
# write. This removes the naive/aware mixing that made the starter raise TypeError.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees_col = db["employees"]
logs_col = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")

# Epoch-millisecond bounds from the contract: below the minimum looks like seconds.
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

HALF_DAY_HOURS = Decimal("4.50")  # R5: rounded work_hours < 4.50
GRACE_SECONDS = 600  # R2: 10 minutes
OVERTIME_MIN_SECONDS = 1800  # R3: 30 minutes


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotently)
# --------------------------------------------------------------------------- #
EMPLOYEE_INDEXES = (
    ([("emp_code", ASCENDING)], {"unique": True, "name": "uq_emp_code"}),
    ([("joined_on", ASCENDING)], {"name": "ix_emp_joined_on"}),
    ([("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "ix_emp_dept_joined"}),
    # Extended key so a department equality has its candidates ordered by emp_code:
    # the $lookup on employees then finds a match without an in-memory sort (SORT stage
    # would be fine too, but this keeps the fallback plan clean).
    (
        [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        {"name": "ix_emp_dept_joined_code"},
    ),
)

LOG_INDEXES = (
    # Natural key (emp_code, date): enforces one record per day and powers the
    # punch-in race (409) and per-employee month range scans.
    ([("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "uq_log_emp_date"}),
    # Default listing sort (date desc, emp_code asc) and date-range scans.
    ([("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "ix_log_date_emp"}),
    # Status-filtered listing.
    ([("status", ASCENDING), ("date", DESCENDING)], {"name": "ix_log_status_date"}),
)


def ensure_indexes() -> None:
    """Create every index the app relies on. Idempotent: safe to run on every start."""
    for keys, opts in EMPLOYEE_INDEXES:
        employees_col.create_index(keys, **opts)
    for keys, opts in LOG_INDEXES:
        logs_col.create_index(keys, **opts)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    try:
        yield
    finally:
        client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time / epoch helpers
# --------------------------------------------------------------------------- #
def as_utc(dt: datetime) -> datetime:
    """Return an aware UTC datetime; treat a naive value as UTC (PyMongo default)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: instants are truncated to whole seconds before any calculation."""
    return dt.replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    """Aware/naive UTC datetime -> epoch milliseconds (exact integer arithmetic)."""
    dt = as_utc(dt)
    delta = dt - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def now_utc_seconds() -> datetime:
    return truncate_to_seconds(datetime.now(tz=UTC))


def ist(dt: datetime) -> datetime:
    return as_utc(dt).astimezone(IST)


# --------------------------------------------------------------------------- #
# Rounding (R4, R8)
# --------------------------------------------------------------------------- #
def half_up(value: Decimal | int | float, places: int) -> float:
    """Round half-up (NOT banker's) to ``places`` decimals."""
    quantum = Decimal(1).scaleb(-places)  # 0.01 for places=2
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def seconds_to_hours(seconds: int) -> float:
    """R4: (seconds / 3600) rounded to 2 decimals half-up, computed exactly."""
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Shift / attendance-date helpers (R1)
# --------------------------------------------------------------------------- #
def hhmm_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """A shift is overnight when it ends at or before it starts (R1)."""
    return hhmm_to_minutes(shift_end) <= hhmm_to_minutes(shift_start)


def shift_start_instant(date_str: str, shift_start: str) -> datetime:
    """The shift start as an IST instant on the given attendance date."""
    d = date_cls.fromisoformat(date_str)
    minutes = hhmm_to_minutes(shift_start)
    return datetime(d.year, d.month, d.day, tzinfo=IST) + timedelta(minutes=minutes)


def shift_end_instant(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """The shift end as an IST instant; the next calendar day for an overnight shift."""
    base = shift_start_instant(date_str, shift_end)
    if is_overnight(shift_start, shift_end):
        base += timedelta(days=1)
    return base


def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date the punch-in belongs to, honouring overnight shifts."""
    local = ist(truncate_to_seconds(punch_in_utc))
    if is_overnight(shift_start, shift_end):
        if (local.hour * 60 + local.minute) < hhmm_to_minutes(shift_end):
            local -= timedelta(days=1)
    return local.date().isoformat()


# --------------------------------------------------------------------------- #
# Derived-value helpers (R2 - R5)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in_utc: datetime, date_str: str, shift_start: str) -> int:
    """R2: 0 within the 10-minute grace, otherwise floor minutes since shift start."""
    punch_in = truncate_to_seconds(punch_in_utc)
    start = shift_start_instant(date_str, shift_start)
    elapsed = int((punch_in - start).total_seconds())
    if elapsed <= GRACE_SECONDS:
        return 0
    return elapsed // 60


def compute_overtime_minutes(
    punch_out_utc: datetime, date_str: str, shift_start: str, shift_end: str
) -> int:
    """R3: floor minutes past shift end, only when at least 30, else 0."""
    punch_out = truncate_to_seconds(punch_out_utc)
    end = shift_end_instant(date_str, shift_start, shift_end)
    elapsed = int((punch_out - end).total_seconds())
    if elapsed < OVERTIME_MIN_SECONDS:
        return 0
    return elapsed // 60


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in hours, 2 decimals half-up."""
    seconds = int((truncate_to_seconds(punch_out_utc) - truncate_to_seconds(punch_in_utc)).total_seconds())
    return seconds_to_hours(seconds)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half day when the rounded work_hours is below 4.50."""
    if work_hours is None:
        return False
    return Decimal(str(work_hours)) < HALF_DAY_HOURS


def is_present(status: str) -> bool:
    """R6: PRESENT, WFH and ON_DUTY count as present."""
    return status in PRESENCE_STATUSES


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def _ms_or_none(value):
    return dt_to_ms(value) if isinstance(value, datetime) else value


def serialize_history(history) -> list[dict]:
    """history[].at and the from/to of punch_in/punch_out become epoch ms."""
    out = []
    for entry in history or []:
        changes = {}
        for field, delta in (entry.get("changes") or {}).items():
            changes[field] = {
                "from": _ms_or_none(delta.get("from")),
                "to": _ms_or_none(delta.get("to")),
            }
        out.append(
            {
                "at": _ms_or_none(entry.get("at")),
                "by": entry.get("by"),
                "reason": entry.get("reason"),
                "changes": changes,
            }
        )
    return out


def serialize_employee(doc: dict) -> dict:
    """Employee document -> API shape (never leaks _id; created_at as epoch ms)."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def serialize_record(doc: dict) -> dict:
    """Attendance document -> API shape with defaults and epoch-ms instants."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms_or_none(doc.get("punch_in")),
        "punch_out": _ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0) or 0,
        "overtime_minutes": doc.get("overtime_minutes", 0) or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history")),
    }


# --------------------------------------------------------------------------- #
# Validation models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)]


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _check_joined_on(cls, v: str) -> str:
        try:
            date_cls.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a YYYY-MM-DD date")
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    """A manual correction (PATCH). Derived fields cannot be sent; extra keys are ignored.

    ``reason`` and ``regularized_by`` are required. ``status``/``punch_in``/``punch_out``
    are optional, and ``None`` means "unchanged".
    """

    model_config = ConfigDict(extra="ignore")

    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """200 only when MongoDB answers a ping; 503 otherwise."""
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employee endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    """Create an employee. Uniqueness is enforced by the unique index, not a read."""
    doc = body.model_dump()
    doc["created_at"] = now_utc_seconds()
    try:
        employees_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Filtered, paginated employee list sorted by emp_code ascending (R10)."""
    query: dict = {}
    if department:
        query["department"] = department
    total = employees_col.count_documents(query)
    cursor = (
        employees_col.find(query)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    """Create the day's record (R1, R2). Race-safe: the unique index arbitrates."""
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    punch_in_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    date_str = attendance_date(punch_in_at, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(punch_in_at, date_str, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status.value,
        "punch_in": punch_in_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
        "_rev": 1,  # internal optimistic-concurrency counter, never serialised
    }
    try:
        logs_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_record(doc)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Filtered, paginated attendance, sorted by date desc then emp_code asc (R10)."""
    for label, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None:
            try:
                date_cls.fromisoformat(value)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"{label} must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")

    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        date_range = {}
        if date_from:
            date_range["$gte"] = date_from
        if date_to:
            date_range["$lte"] = date_to
        query["date"] = date_range
    if status:
        query["status"] = status.value

    total = logs_col.count_documents(query)
    cursor = (
        logs_col.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_record(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutIn):
    """Close the latest open record at or before ``punched_at`` (R3-R5).

    Works across midnight: the record is chosen by an instant comparison, so an
    overnight shift's punch-out lands on the record the shift started on. The
    close is an atomic guarded update, so simultaneous punch-outs yield exactly
    one 200 and the rest 409.
    """
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    punch_out_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    punch_out_at = truncate_to_seconds(punch_out_at)

    record = logs_col.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_out_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="no open punch-in found")

    # Already punched out -> 409 on the current state, before the timing checks.
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record already punched out")

    punch_in = truncate_to_seconds(record["punch_in"])
    delta = int((punch_out_at - punch_in).total_seconds())
    if delta <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if delta > 86400:
        raise HTTPException(status_code=422, detail="punched_at must be within 24 hours of punch_in")

    work_hours = compute_work_hours(punch_in, punch_out_at)
    overtime = compute_overtime_minutes(
        punch_out_at, record["date"], emp["shift_start"], emp["shift_end"]
    )
    half_day = compute_half_day(work_hours)

    # Guard on punch_out still being null: the loser of a concurrent pair matches
    # nothing and gets 409, so any number of simultaneous punch-outs yield one 200.
    updated = logs_col.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="record already punched out")
    return serialize_record(updated)


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """Manual correction of one record, addressed by (emp_code, date).

    Applies the supplied fields, recomputes every derived field from the final
    times (R2-R5) and appends exactly one ``history`` entry describing what
    actually changed. A request that changes nothing is 422.
    """
    emp = employees_col.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    record = logs_col.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="no attendance record for that date")

    # --- resolve the final state -------------------------------------------------
    status = body.status.value if body.status is not None else record.get("status", "PRESENT")

    if body.punch_in is not None:
        punch_in = ms_to_dt(body.punch_in)
        # R1: the punch-in must still fall on this record's attendance date. The
        # midnight-to-midnight window makes this a plain IST calendar-date check.
        if attendance_date(punch_in, "00:00", "00:00") != date:
            raise HTTPException(status_code=422, detail="punch_in does not fall on the record's date")
    else:
        punch_in = record.get("punch_in")

    if body.punch_out is not None:
        punch_out = ms_to_dt(body.punch_out)
    else:
        punch_out = record.get("punch_out")

    # --- status / punch compatibility -------------------------------------------
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422, detail="ABSENT/LEAVE clears punch times and cannot carry a punch"
            )
        punch_in = punch_out = None
    else:  # presence status needs a punch-in, supplied or already stored
        if punch_in is None:
            raise HTTPException(status_code=422, detail="a presence status requires a punch_in")
        if punch_out is not None:
            delta = int(
                (truncate_to_seconds(punch_out) - truncate_to_seconds(punch_in)).total_seconds()
            )
            if delta <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if delta > 86400:
                raise HTTPException(
                    status_code=422, detail="punch_out must be within 24 hours of punch_in"
                )

    # --- recompute derived values from the final state --------------------------
    if status in ("ABSENT", "LEAVE"):
        work_hours, late, overtime, half_day = None, 0, 0, False
    else:
        late = compute_late_minutes(punch_in, date, emp["shift_start"])
        if punch_out is not None:
            work_hours = compute_work_hours(punch_in, punch_out)
            overtime = compute_overtime_minutes(
                punch_out, date, emp["shift_start"], emp["shift_end"]
            )
            half_day = compute_half_day(work_hours)
        else:
            work_hours, overtime, half_day = None, 0, False

    final = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }

    # --- diff against the stored snapshot: only real changes are logged ----------
    changes = {}
    for field, to_value in final.items():
        from_value = record.get(field)
        if field == "work_hours" and from_value is not None and to_value is not None:
            if float(from_value) == float(to_value):
                continue
        elif from_value == to_value:
            continue
        changes[field] = {"from": from_value, "to": to_value}

    if not changes:
        raise HTTPException(status_code=422, detail="no changes to apply")

    entry = {
        "at": now_utc_seconds(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Optimistic concurrency: only write if nobody else corrected the record since we
    # read it, so concurrent edits cannot silently lose history (loser -> 409). Legacy
    # records have no _rev yet; match on its absence and start the counter at 1.
    rev = record.get("_rev")
    if rev is None:
        guard = {"_rev": {"$exists": False}}
        new_rev = 1
    else:
        guard = {"_rev": rev}
        new_rev = rev + 1

    updated = logs_col.find_one_and_update(
        {"_id": record["_id"], **guard},
        {
            "$set": {
                "status": final["status"],
                "punch_in": final["punch_in"],
                "punch_out": final["punch_out"],
                "work_hours": final["work_hours"],
                "late_minutes": final["late_minutes"],
                "overtime_minutes": final["overtime_minutes"],
                "half_day": final["half_day"],
                "_rev": new_rev,
            },
            "$push": {"history": entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="record was modified concurrently; retry")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# Analytics
#
# Everything below is a MongoDB aggregation. The same builder functions are used
# by the endpoints and by /admin/explain, so the explained pipeline is byte-for-
# byte the one that runs. Analytics read the stored derived values (R8 note in
# DATA_MODEL.md), they never recompute work_hours / late_minutes from the times.
# --------------------------------------------------------------------------- #
MonthQuery = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


def month_bounds(month: str) -> tuple[str, str]:
    """'YYYY-MM' -> ('YYYY-MM-01', 'YYYY-MM-<last day>'), the inclusive date strings."""
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}"


def _is_iso_date(value: str) -> bool:
    try:
        date_cls.fromisoformat(value)
        return True
    except ValueError:
        return False


def _present_days_expr() -> dict:
    """R5-R7: Mon-Fri presence records, a half day counting as 0.5."""
    return {
        "$cond": [
            {
                "$and": [
                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                ]
            },
            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
            0,
        ]
    }


def _late_count_expr() -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


def _is_present_with_hours_expr() -> dict:
    return {
        "$and": [
            {"$in": ["$status", list(PRESENCE_STATUSES)]},
            {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
        ]
    }


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    """One aggregation over this employee's records for the month."""
    lo, hi = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": lo, "$lte": hi}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _present_days_expr()},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": _late_count_expr()},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: Optional[str]) -> list[dict]:
    """Logs side of the department summary: metrics grouped by the employee's department."""
    lo, hi = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": lo, "$lte": hi}}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$emp_code", "$$ec"]}}},
                    {"$project": {"_id": 0, "department": 1}},
                ],
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.append(
        {
            "$group": {
                "_id": "$emp.department",
                "present_days": {"$sum": _present_days_expr()},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                "late_count": {"$sum": _late_count_expr()},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "work_hours_sum": {
                    "$sum": {"$cond": [_is_present_with_hours_expr(), {"$ifNull": ["$work_hours", 0]}, 0]}
                },
                "work_hours_cnt": {"$sum": {"$cond": [_is_present_with_hours_expr(), 1, 0]}},
            }
        }
    )
    return pipeline


def build_leaderboard_pipeline(
    month: str, department: Optional[str], limit: int
) -> list[dict]:
    """Rank employees by total late minutes with standard competition ranking.

    Ranking sorts by ``total_late_minutes`` only, so ties share a rank and the next
    rank is skipped (1, 2, 2, 4); ``limit`` is applied to the rank afterwards.
    """
    lo, hi = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": lo, "$lte": hi}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": _late_count_expr()},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"ec": "$_id"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$emp_code", "$$ec"]}}},
                    {"$project": {"_id": 0, "name": 1, "department": 1}},
                ],
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},  # logs whose employee no longer exists are dropped
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$set": {"emp_code": "$_id", "name": "$emp.name", "department": "$emp.department"}},
        # sortBy is the total alone -> equal totals get the same rank.
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def build_trend_pipeline(
    department: str, date_from: str, date_to: str, dept_employees: list[dict]
) -> list[dict]:
    """One row per calendar day in [from, to], including weekends and empty days.

    Densify works on a date field ($densify rejects strings), so the day is
    converted to a BSON date, densified, and formatted back to YYYY-MM-DD.
    """
    start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=UTC)
    # $densify treats the upper bound as EXCLUSIVE, so densify up to the day after
    # the requested end to get one row per calendar day in the inclusive range.
    end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    return [
        # Scope to this department's employees; the index {emp_code,date} plus the
        # date range keeps this an IXSCAN even with a large $in list.
        {"$match": {"emp_code": {"$in": [e["emp_code"] for e in dept_employees]},
                    "date": {"$gte": date_from, "$lte": date_to}}},
        {"$addFields": {"day_dt": {"$dateFromString": {"dateString": "$date"}}}},
        {
            "$group": {
                "_id": "$day_dt",
                "present_count": {"$sum": {"$cond": [
                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                    0,
                ]}},
                "late_count": {"$sum": _late_count_expr()},
            }
        },
        # One row per calendar day in the range, gaps filled with {present,late}=0.
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [start, end]}}},
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$_id"}, 5]},
                "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": dept_employees,
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$day"]},  # R9
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        # $avg skips nulls, so the window averages only the non-null rates it holds.
        # The window is the current row and up to 6 preceding rows inside the range.
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$day",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [{"$ne": ["$attendance_rate", None]}, {"$round": ["$attendance_rate", 4]}, None]
                },
                "moving_avg_7d": {
                    "$cond": [{"$ne": ["$moving_avg_7d", None]}, {"$round": ["$moving_avg_7d", 4]}, None]
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: MonthQuery):
    """Monthly attendance summary for one employee (404 for an unknown employee)."""
    emp = employees_col.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    docs = list(logs_col.aggregate(build_employee_monthly_pipeline(emp_code, month), allowDiskUse=True))
    agg = docs[0] if docs else {}

    # R7: Mon-Fri between joined_on and the end of the month, clamped to the month.
    lo, hi = month_bounds(month)
    start = max(emp["joined_on"], lo)
    working_days = 0
    if start <= hi:
        d0 = date_cls.fromisoformat(start)
        d1 = date_cls.fromisoformat(hi)
        working_days = sum(
            1 for i in range((d1 - d0).days + 1) if (d0 + timedelta(days=i)).weekday() < 5
        )

    present_days = agg.get("present_days", 0) or 0
    attendance_pct = half_up(present_days / working_days * 100, 2) if working_days else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": agg.get("leave_days", 0) or 0,
        "late_count": agg.get("late_count", 0) or 0,
        "total_late_minutes": agg.get("total_late_minutes", 0) or 0,
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0) or 0,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(month: MonthQuery, department: Optional[str] = None):
    """Per-department monthly summary across both collections.

    Headcount (R9) comes from ``employees`` and includes people with no logs at all;
    the metrics come from the ``attendance_logs`` aggregation. A department with a
    headcount of 0 for the month is omitted.
    """
    _, month_end = month_bounds(month)

    emp_query: dict = {"joined_on": {"$lte": month_end}}
    if department:
        emp_query["department"] = department
    headcounts = {
        d["_id"]: d["headcount"]
        for d in employees_col.aggregate(
            [{"$match": emp_query}, {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}]
        )
    }

    metrics = {
        d["_id"]: d
        for d in logs_col.aggregate(
            build_department_summary_pipeline(month, department), allowDiskUse=True
        )
    }

    items = []
    for dept, headcount in headcounts.items():
        if headcount <= 0:
            continue
        g = metrics.get(dept, {})
        wh_sum, wh_cnt = g.get("work_hours_sum", 0), g.get("work_hours_cnt", 0)
        items.append(
            {
                "department": dept,
                "headcount": headcount,
                "present_days": g.get("present_days", 0) or 0,
                "avg_work_hours": half_up(wh_sum / wh_cnt, 2) if wh_cnt else None,
                "late_count": g.get("late_count", 0) or 0,
                "total_late_minutes": g.get("total_late_minutes", 0) or 0,
                "leave_count": g.get("leave_count", 0) or 0,
                "on_duty_count": g.get("on_duty_count", 0) or 0,
            }
        )
    items.sort(key=lambda x: x["department"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: MonthQuery,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers ranking for a month (standard competition ranking, limit post-rank)."""
    items = list(
        logs_col.aggregate(build_leaderboard_pipeline(month, department, limit), allowDiskUse=True)
    )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    """Daily attendance trend for a department over an inclusive date range."""
    for label, value in (("from", date_from), ("to", date_to)):
        if not _is_iso_date(value):
            raise HTTPException(status_code=422, detail=f"{label} must be a YYYY-MM-DD date")
    if date_to < date_from:
        raise HTTPException(status_code=422, detail="to must not be before from")
    span = (date_cls.fromisoformat(date_to) - date_cls.fromisoformat(date_from)).days + 1
    if span > 92:
        raise HTTPException(status_code=422, detail="range must not exceed 92 days")

    # 404 when the department has no employees at all; also gives us the joined_on
    # dates the pipeline needs for the per-day headcount (R9).
    dept_employees = list(
        employees_col.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1})
    )
    if not dept_employees:
        raise HTTPException(status_code=404, detail="department not found")

    items = list(
        logs_col.aggregate(
            build_trend_pipeline(department, date_from, date_to, dept_employees),
            allowDiskUse=True,
        )
    )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #