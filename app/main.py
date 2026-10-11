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

import os
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
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


# --------------------------------------------------------------------------- #
# TODO - remaining endpoints (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #