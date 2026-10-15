# Architectural & Design Decisions

1. **Index Strategy**
   - `employees`: A unique index on `emp_code` enforces primary identity and prevents duplicate registrations. A compound index on `(department, emp_code)` accelerates department-filtered employee listings and department trend resolution without memory sorting. An index on `joined_on` allows the summary aggregation to quickly filter active headcounts.
   - `attendance_logs`: A unique compound index on `(emp_code, date)` guarantees at most one record per employee per calendar day while serving point lookups, punch-outs, and monthly employee range scans. A compound index on `(date: -1, emp_code: 1)` optimizes the default sorted attendance feed and whole-month scans for the leaderboard.

2. **Punch-In Concurrency**
   Both simultaneous requests compute the identical `(emp_code, date)` key and issue `insert_one` directly without a prior read. The unique compound index lets the first write commit (`201 Created`); the competing write immediately fails with a MongoDB `DuplicateKeyError`, caught and converted into `409 Conflict`. Zero locks or read-before-write race windows exist.

3. **Leaderboard Ties at the Boundary**
   The pipeline calculates competition ranks via `$setWindowFields` using `$rank` (e.g., ranks 1, 2, 2, 4). The `rank <= limit` filter runs *after* window ranking, followed by a secondary sort on `emp_code ASC`. If employees tie at the cutoff rank, all tied individuals are retained, allowing the response payload to cleanly exceed `limit` rows when ties occur.

4. **Department Headcount Integrity**
   The aggregation begins from `employees` filtered by `joined_on <= month_end`, inherently excluding future joiners. We `$lookup` matching monthly attendance logs and `$unwind` with `preserveNullAndEmptyArrays: true`. Consequently, employees with zero monthly records still group into their respective departments and increment headcount via `$addToSet: "$emp_code"`, while avoiding distortion of average work hours.

5. **Scaling for 100x Volume (~10M+ Records)**
   At 100x scale, scanning millions of raw logs during on-demand analytics becomes impractical. I would introduce pre-aggregated monthly summary documents per employee/department, updated atomically using `$inc` on punch-out and regularization events. Sharding on `emp_code` (hash-based) would distribute write traffic evenly across clusters while keeping per-employee attendance logs co-located.
