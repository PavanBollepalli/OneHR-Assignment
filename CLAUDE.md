# CLAUDE.md

Guidance for working in this repository.

## What this is

A **take-home assignment for HROne (Software Engineer hiring, contract v2.0)**: build the backend of an
**Employee Attendance & Analytics API** with **FastAPI + MongoDB**.

Read the source material, in this order, before writing code:

1. `PROBLEM_STATEMENT.docx` — the assignment, grading weights, ground rules (extract with `python -c` + `zipfile`, it's a .docx).
2. `openapi.yaml` — **the contract**: paths, methods, params, status codes, response fields, and business rules **R1–R10**. This is authoritative; a wrong field name or status code is a failed test.
3. `DATA_MODEL.md` — what MongoDB stores (`employees`, `attendance_logs`) + nine annotated sample documents.
4. `app/main.py` — the half-finished starter (see "Starter defects" below).
5. `REVIEW.md`, `DECISIONS.md` — templates the candidate must fill in.

The grader starts this app against a **hidden dataset of ~100,000 attendance records** and runs an automated
test suite. "Plausible-looking output" is not the same as correct output — verify against the rules, not intuition.

## Hard constraints (violating these fails the submission)

- **All application code lives in `app/main.py` — one file.** No extra modules.
- The app must start with **`uvicorn app.main:app --port 8000`** from the repo root and be ready
  (`GET /health` → 200) within 20 seconds.
- **`MONGO_URI` and `MONGO_DB` come from environment variables** (a local `.env` is fine, but real env vars must
  win). Never hard-code connection details.
- **Create all indexes at startup, idempotently.** The seeded DB has only the default `_id` index.
- **No network access except MongoDB.** Do not call external services.
- Analytics endpoints must be **MongoDB aggregation pipelines** — not fetch-then-loop-in-Python.
- Must be **concurrency-safe**: the grader sends parallel requests.
- `requirements.txt` at repo root; must include everything imported.
- See "Do not commit" below.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # NOTE: no .env.example ships in the kit — create it, set MONGO_URI / MONGO_DB
                                # local docker: docker run -p 27017:27017 mongo:7
python sample_seed.py                                        # load the 9 sample docs
uvicorn app.main:app --port 8000 --reload
```

- MongoDB **6.0+** required (grader uses 7.0). Atlas free tier (M0) works.
- `sample_seed.py` reads `MONGO_URI`/`MONGO_DB` from env or `.env`.
- The `sample_data/*.json` files are **MongoDB Extended JSON** (`{"$date": "..."}`); load with
  `bson.json_util.loads(..., json_options=json_util.JSONOptions(tz_aware=True))`.

## Where the work is

Part A and B and C, per the problem statement:

- **A. Review** — find and fix **10+ defects** in the starter helpers/endpoints; document each in `REVIEW.md`.
- **B. Build** — the 7 endpoints still in the `TODO` block at the bottom of `app/main.py`:
  `POST /attendance/punch-out`, `PATCH /attendance/{emp_code}/{date}`,
  `GET /analytics/employees/{emp_code}/monthly`, `GET /analytics/departments/summary`,
  `GET /analytics/leaderboard/late`, `GET /analytics/departments/{department}/trend`,
  `GET /admin/explain/{endpoint}`.
- **C. Prove & explain** — `/admin/explain/{endpoint}` must return raw `explain` (`executionStats`) whose plan
  contains **`IXSCAN` and no `COLLSCAN`** at 100k docs; answer 5 questions in `DECISIONS.md` (~150–200 words total).

Grading: Review 10% · CRUD/rules/concurrency 30% · Analytics correctness 30% · MongoDB usage & speed 15% ·
Decisions & code quality 15%. Shortlisted candidates do a **live walkthrough** of the code.

## The rules (R1–R10) — the traps live here

These are **not** visible in the JSON schemas. Most wrong answers come from skipping one.

- **R1 Time zone / date.** Shift times and dates are **IST (UTC+05:30)**. A record's `date` is the IST calendar
  date of its `punch_in`. **Overnight shift** (`shift_end <= shift_start`, e.g. 22:00–06:00): a punch-in earlier
  than `shift_end` belongs to the shift starting the **previous** day; shift end is on the next calendar day.
  **Truncate instants to whole seconds before any calculation** — the truncated value is stored and returned.
- **R2 Late.** Grace **10 min**, late only if **strictly > 10:00** after `shift_start`; then
  `late_minutes` = **floor** of minutes since `shift_start` (not since grace ended). Shift 09:30:
  `09:40:00 → 0`; `09:40:01 → 10`; `10:15:59 → 45`.
- **R3 Overtime.** Floor minutes from `shift_end` to `punch_out`, **only if >= 30**, else 0.
- **R4 Work hours.** `(punch_out - punch_in)` seconds / 3600, rounded **2 decimals HALF-UP** — not banker's
  rounding, not Python's default `round()`. `null` until punch-out.
- **R5 Half day.** After punch-out, `half_day = true` when the **rounded** `work_hours < 4.50`. Half day = 0.5 of
  a present day.
- **R6 Presence.** `PRESENT`, `WFH`, `ON_DUTY` count as present; `ABSENT`, `LEAVE` do not.
- **R7 Working days.** Mon–Fri, no holiday calendar; from `joined_on` for mid-month joiners. `present_days` counts
  only Mon–Fri records; **weekend records still add to late and overtime totals**.
- **R8 Rounding.** 2 decimals half-up (rates: 4 decimals half-up).
- **R9 Headcount.** Employees with `joined_on <= end of period`, **whether or not they have any logs**.
- **R10 Pagination.** `page` starts at **1**; `page_size` default 20, max 100; `total` always reflects filters.

## Conventions

- **Employees are identified by `emp_code`** (e.g. `EMP0001`) — a customer-supplied business code, unique,
  immutable. **`_id` is internal: never in a request or response.** Attendance records have no id either; a record
  is addressed by **(`emp_code`, `date`)**.
- **Instants are epoch milliseconds (int)** in the API — every time value (`punched_at`, `punch_in`, `punch_out`,
  `created_at`, `history[].at`, and the `from`/`to` inside `history[].changes`). MongoDB stores BSON datetimes
  (UTC). Seconds-looking values (< `100000000000`), floats and strings → **422**. PyMongo returns **naive**
  datetimes by default — handle the tz conversion at the boundary.
- **Calendar values are strings**: `YYYY-MM-DD`, `YYYY-MM` (month), `HH:MM` (shift times), all IST.
- **Derived fields are read-only**: `work_hours`, `late_minutes`, `overtime_minutes`, `half_day`. Computed only by
  the API, at punch-in / punch-out / after every correction. Clients must not send them (ignore if they do).
- **Analytics read the STORED derived values** — do not recompute them from the punch times.
- **`history`** is the audit trail of **manual corrections only** (PATCH). Each successful PATCH appends exactly one
  `{at, by, reason, changes}`; `changes` holds one key per field that **actually changed**, as
  `{"from": ..., "to": ...}`. Never edited or removed. Punch-in/punch-out never write to it. A never-corrected
  record has `history: []`.
- Missing fields in seeded data mean their default: no `history` → `[]`, no `half_day` → `false`,
  no `late_minutes`/`overtime_minutes` → 0.
- Redefine helpers rather than trusting the starter's math (see below).

## Starter defects (Part A — find, fix, document)

The starter "runs and the happy path works" but has **10+ defects**. Known examples; verify each yourself and add
any others you find to `REVIEW.md`:

- `compute_late_minutes` — boundary wrong (returns 0 where R2 wants 10), floors before comparing, no truncation.
- `compute_overtime` — returns values < 30 min (should be 0); builds shift-end on the **same** date (breaks
  overnight); mixes naive/aware datetimes → `TypeError` (500).
- `compute_work_hours` — uses `round()` (banker's) where R4 demands **half-up**.
- `punch_in` — check-then-insert **race** (needs unique index + `DuplicateKeyError`); missing employee → 500
  instead of **404**; wrongly accepts `ABSENT`/`LEAVE`; no seconds truncation; local-time conversion, not IST.
- `create_employee` — returns `created_at` as a datetime, not **epoch ms**; no `shift_start != shift_end` check;
  relies on a read-then-insert instead of a unique index.
- `list_employees` — off-by-one `skip = page * page_size` (page 1 skips the first 20 rows; should be
  `(page - 1) * page_size`); `total` ignores the `department` filter; not sorted by `emp_code`.
- `list_attendance` — **loads the whole collection into memory** and sorts in Python (fails at 100k); sort
  order/`page` handling wrong.
- `/health` — never pings MongoDB (must 503 when it's down); no indexes created at startup; responses leak raw
  datetimes instead of epoch ms.

## Non-obvious analytics behaviour

- **Department summary** — cross-collection: join logs to employees for `department`; headcount must include
  **zero-log** employees and exclude not-yet-joined; `avg_work_hours` is a mean **over records**, not an average of
  per-employee averages; omit a department whose headcount is 0 for the month.
- **Late leaderboard** — **standard competition ranking** (1, 2, 2, 4), ties share a rank and the next is skipped.
  `limit` applies **after** ranking, so the response can contain **more than `limit` rows**. Order: total late desc,
  then `emp_code` asc. Ignore logs whose employee no longer exists. Rank inside `department` when given.
- **Department trend** — **exactly one row per calendar day** in `[from, to]` inclusive, weekends and empty days
  included (use `$densify`, **not** a Python loop). `attendance_rate = present_count / headcount` (4 dp half-up),
  **only on working days with headcount > 0, else null**; a working day with no records → `0.0`.
  `moving_avg_7d` = mean of non-null rates over the current row and up to 6 preceding rows **inside the range**
  (first rows use a shorter window); `null` if the window has no non-null rate. Range > 92 days → 422.
- **Employee monthly** — `attendance_pct = present_days / working_days × 100`, or `null` when `working_days == 0`.

## Errors

- FastAPI default body: `{"detail": ...}`. Validation failures → **422**; missing resource → **404**; conflict →
  **409**. A 500 is always a failed test.

## Do not commit 
## if the user asks to commit it then don't add yourself as contributor

`.env`, any secrets, a `Dockerfile`, virtual environments, or data dumps. Submit only `app/main.py`,
`requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and a short `README.md`, in a **public** Git repo.