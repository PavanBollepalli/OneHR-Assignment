# Employee Attendance & Analytics API

FastAPI + MongoDB backend for the HROne assignment. All application code lives in the single
file [`app/main.py`](app/main.py).

## Run

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Set the connection details (real environment variables win over a local `.env`):

```bash
# macOS / Linux
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

# Windows PowerShell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB  = "attendance_db"
```

Or drop the same two variables into a git-ignored `.env` at the repo root (there is **no**
`.env.example` in the kit — create it yourself). Atlas (`mongodb+srv://…`) works; `pymongo[srv]`
pulls in `dnspython`, which Atlas URIs need.

Start the app and check health:

```bash
uvicorn app.main:app --port 8000
curl http://localhost:8000/health        # -> {"status":"ok"} once MongoDB answers
```

Optionally load the nine sample documents first (they come from MongoDB Extended JSON, so
`sample_seed.py` parses them with `json_util`), then open <http://localhost:8000/docs>.

```bash
python sample_seed.py
```

MongoDB **6.0 or newer** is required (the grader uses 7.0). Indexes are created idempotently at
startup, so a freshly seeded database needs no manual setup.

## What is implemented

- **Review (Part A)** — the starter's helpers and CRUD endpoints were rewritten; every defect found is
  listed with the fix in [`REVIEW.md`](REVIEW.md).
- **Build (Part B)** — all seven previously `TODO` endpoints:
  `POST /attendance/punch-out`, `PATCH /attendance/{emp_code}/{date}`,
  `GET /analytics/employees/{emp_code}/monthly`, `GET /analytics/departments/summary`,
  `GET /analytics/leaderboard/late`, `GET /analytics/departments/{department}/trend`,
  and `GET /admin/explain/{endpoint}`.
- **Prove (Part C)** — `/admin/explain/{endpoint}` returns the raw `explain` at `executionStats`
  verbosity for `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard` and
  `department_trend`, using the same query/pipeline builders as the real endpoints. The plan shows
  `IXSCAN` with no `COLLSCAN` on a 100k-record dataset. [`DECISIONS.md`](DECISIONS.md) answers the
  five design questions.

Business rules R1–R10 (IST dates and overnight shifts, late/overtime/work-hours thresholds,
half-day, presence, working days, headcount, rounding, pagination) are implemented per `openapi.yaml`.
Analytics read the stored derived values rather than recomputing them, and are MongoDB aggregation
pipelines (`$group`, `$lookup`, `$setWindowFields`/`$rank`, `$densify`, windowed `$avg`) — not
fetch-then-loop. Writes are concurrency-safe: punch-in relies on a unique `(emp_code, date)` index
(duplicate → 409), punch-out closes the open record atomically, and PATCH uses optimistic concurrency
so concurrent corrections cannot lose history.

## Not included

- No test suite is committed; verification was run separately against a synthetic awkward-case
  dataset (zero-log and mid-month employees, overnight shifts, weekend records, ties, legacy records,
  concurrency bursts).
- No authentication or multi-tenancy — the contract does not ask for either.
- No `.env`, secrets, `Dockerfile`, virtualenv or data dumps are committed.
