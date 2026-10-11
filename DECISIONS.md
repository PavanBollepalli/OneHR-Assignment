# DECISIONS.md

**1. Indexes.** `employees`: unique `emp_code` (identity; duplicate create → 409), `joined_on`
(R9 headcount range), `department` (filters and `$lookup`). `attendance_logs`: unique
`(emp_code, date)` — one record a day, the punch-in race, per-employee month scans;
`(date desc, emp_code asc)` — the default listing and date ranges; `(status, date desc)` —
status listings. I rejected a standalone `date` index: the compound index's prefix already
serves date ranges, so it would only add write cost.

**2. Punch-in race.** Neither request reads first; both insert against the unique
`(emp_code, date)` index. MongoDB serialises them: the first wins (201), the second raises
`DuplicateKeyError`, which I return as 409. No check-then-insert window, so exactly one
record is ever created.

**3. Ties.** `$rank` sorts by total late minutes alone, so equal totals share a rank and the
next is skipped (1, 2, 2, 4). The `rank <= limit` filter runs *after* ranking, so two
employees tied at the cutoff both appear — more than `limit` rows — ordered by total
late desc, then `emp_code` asc.

**4. Headcount.** Headcount comes from a separate `employees` query, not the logs: match
`joined_on <= month_end` (R9), group by department, merge with the log metrics. A department
therefore appears whenever it has any employee, even one with no records at all.

**5. At 100×.** Add a pre-aggregated daily rollup collection updated on each punch, so
analytics read small summaries instead of scanning tens of millions of raw logs.