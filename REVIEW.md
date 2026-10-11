# REVIEW.md

Defects found in the starter `app/main.py` (the "colleague who left" version), and how each was
fixed. Line numbers refer to that original file.

| # | Where (function / line) | What is wrong | How you'd notice it | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` (33–37) | Builds the shift start with `punch_in.replace(hour=h, minute=m)`, so it lands on the punch's own instant and timezone; floors minutes **before** comparing to the grace, and measures from the grace edge rather than the shift start. No seconds truncation. | Shift 09:30: a punch at `09:40:01` returns **0** (R2 wants 10); `10:15:59` returns 15 (wants 45). | Rewrote: truncate both instants to whole seconds, `elapsed = punch_in − shift_start`; return `0` when `elapsed ≤ 600 s`, else `elapsed // 60` (floor). |
| 2 | `compute_work_hours` (40–41) | Uses Python `round()`, which is **banker's** rounding, not half-up (R4). | `8.335 → 8.33` and `2.675 → 2.67`, where R4/R8 require `8.34` / `2.68`. | `decimal.Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)`. |
| 3 | `compute_overtime` (44–47) | No `>= 30 min` gate (returns any positive overrun); builds the shift end on the **same** date, so overnight shifts are wrong; sets `tzinfo=IST` on the shift end while `punch_out` read from Mongo is **naive** → `TypeError` → 500. | A 20-minute overrun returns 20 (R3 wants 0); a 22:00–06:00 shift's end is computed for the wrong day; any naive `punch_out` raises. | Rewrote: shift end is the **next day** when `shift_end ≤ shift_start`; return 0 when `< 1800 s`, else `elapsed // 60`. |
| 4 | (missing) half-day / presence | R5 (`half_day` from **rounded** `work_hours < 4.50`) and R6 (PRESENT/WFH/ON_DUTY are present) have no implementation at all. | `present_days` and `half_day` can never be right. | Added `compute_half_day` and `is_present`. |
| 5 | `EmployeeIn` (53–60) | No validation: `emp_code`, `name`/`email`/`department` lengths, `HH:MM` shift format and `joined_on` date are all unchecked; no `shift_start != shift_end` rule. | Any string is accepted; equal shift start/end is stored. | Added patterns/lengths and a model validator → **422**. |
| 6 | `PunchInIn` (63–66) | `status: str` accepts `ABSENT`/`LEAVE` (must be a presence status); `punched_at: int` accepts floats, strings and seconds-scale values. | `status:"ABSENT"` creates a record (should be 422); `punched_at: 1.7e12` or a 10-digit value is accepted. | Typed `status` as the presence enum and `punched_at` as a strict epoch-ms range → **422**. |
| 7 | `/health` (72–74) | Returns 200 unconditionally and never contacts MongoDB. | With Mongo down it still answers 200; the contract requires **503**. | Pings via `admin.command("ping")`; 200 when it answers, 503 otherwise. |
| 8 | `POST /employees` (`create_employee`, 77–85) | Returns `created_at` as a **naive local datetime**, not epoch ms; uses `datetime.now()` (local, not UTC); checks-then-inserts with **no unique index**. | `created_at` is a string, not an integer; two concurrent creates both return 201. | Server-set UTC timestamp → epoch ms; rely on the unique `emp_code` index and map `DuplicateKeyError` → **409**. |
| 9 | `GET /employees` (`list_employees`, 88–96) | `skip = page * page_size` (off-by-one — page 1 skips the first page); `total = count_documents({})` ignores the `department` filter; results are unsorted. | Page 1 is missing its first `page_size` rows; a department filter returns a global `total`; order is arbitrary. | `skip = (page − 1) * page_size`; `total = count_documents(filter)`; sort by `emp_code` asc. |
| 10 | `POST /attendance/punch-in` (`punch_in`, 99–121) | `datetime.fromtimestamp(ms/1000)` yields **local** time and `ts.date()` a local date, not the IST attendance date (R1, no overnight handling); no seconds truncation; a missing employee raises on `emp["shift_start"]` → **500**; accepts `ABSENT`/`LEAVE`; check-then-insert **race**; leaks an internal `id`. | A 05:00 IST punch lands on the wrong date; unknown employee → 500 instead of 404; duplicate punch-ins can slip through. | 404 when the employee is missing; convert epoch ms → UTC and truncate; compute `date` via the R1 overnight-aware helper; insert against the unique `(emp_code, date)` index and map `DuplicateKeyError` → **409**; serialise without `_id`/`id`. |
| 11 | `GET /attendance` (`list_attendance`, 124–146+) | Loads the **whole collection** into memory and sorts in Python (fails on the 100k dataset); the sort has no `emp_code` tiebreaker; leaks an internal `id`; no `date_from ≤ date_to` or format validation; `page`/`page_size` unbounded. | Memory/CPU blow-up at 100k; pagination order is unstable across equal dates; the response carries `"id"`. | Push filter + sort + `skip` + `limit` into Mongo (`date` desc, `emp_code` asc); `count_documents(filter)` for the total; validate dates and pagination → **422**. |
| 12 | Indexes (startup) | No indexes are created; only the default `_id` exists. | Every query is a collection scan, and the punch-in race is unguarded. | A lifespan handler calls `ensure_indexes()` idempotently — unique `(emp_code, date)` (race → 409), `emp_code` unique, plus the listing/analytics indexes. |

Beyond the starter, the seven endpoints in the `TODO` block (punch-out, PATCH, the four analytics,
and `/admin/explain`) were implemented in `app/main.py`, with all instants converted to epoch ms at
the boundary and never leaking `_id`.

## Examined and decided *not* a defect

- **`load_dotenv()` with the default `override=False`** — correct: a real environment variable is
  meant to win over `.env`, and that is the default behaviour already.
- **`IST = timezone(timedelta(hours=5, minutes=30))`** — the right offset for R1.
- **`HTTPException(409, "…")` with a bare int** — works, and the resulting body is still
  `{"detail": …}` as required; kept the same shape while making codes explicit.
- **`punched_at` defaulting to "now" when omitted** — this matches the contract (the field is
  optional and defaults to the current time); the bug was *how* "now" was computed, not the default.
- **`app = FastAPI(title=…, version="2.0.0")`** — no problem; left as is.
- **`"history": []` seeded on punch-in** — correct: a fresh record has an empty audit trail, and
  punch-in/punch-out must never write to it.
