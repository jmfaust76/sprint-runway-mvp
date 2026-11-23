# SprintRunway Diagnostic: Fill-In Guide

Fill in **three small tables**: budgets, team allocations and delivery metrics. Within **48 hours of receiving complete files**, you'll get back:
- a runway & risk brief,
- a forecast table, and
- a 45-minute readout.

You don't need timesheets, Jira access or real names. Expect about 1–2 hours of work. Most of it is pulling four numbers per team from your sprint reports.

**Before you start, pick one as-of date**, for example today. Pull every number as of that date, and tell us the date when you send the files.

---

## 1. `budgets.csv`: one row per product or squad budget

| Column | What to enter |
|---|---|
| `product_id` | A short ID you choose, e.g. `PROD-ALPHA`. Use the **exact same ID** in the other two files. |
| `product_name` | Plain name, e.g. `Member Portal` |
| `period_start`, `period_end` | The budget period, e.g. `2026-01-01` and `2026-12-31` |
| `total_annual_budget` | The full budget for the period |
| `remaining_budget` | What's left **on your as-of date** |
| `remaining_backlog_points` | Points still to deliver this period, in the same point scale the team uses for velocity |
| `target_release_note` | *Optional.* The release this budget must land, e.g. `Q4 Enrollment Release` |

## 2. `roster_allocations.csv`: one row per person, per product

If someone splits time across products, give them **one row per product with the same `member_id`**. For example, a 50/50 split is two rows at `50`.

| Column | What to enter |
|---|---|
| `member_id` | Any stable ID, e.g. `EMP-01`. It's the only identifier that appears in our reports. |
| `name` | **Any label.** Real names aren't needed. `Contractor Dev 1` or `FTE QA 2` works fine. |
| `role` | e.g. `Senior Backend Eng`, `QA Engineer`, `Product Owner` |
| `employment_type` | `FTE` or `Contractor` (`Vendor` is also accepted) |
| `product_id` | Must match a row in `budgets.csv` |
| `allocation_pct` | Share of this person's time on this product, `0`–`100`. One person's rows shouldn't add up to more than 100. |
| `blended_hourly_rate` | Hourly cost. For FTEs, a loaded cost (salary + benefits + overhead ÷ hours). For contractors, the bill rate. **Averages by role band are fine.** |
| `weekly_hours` | Usually `40`. Leave blank to use 40. |
| `contract_end_date` | **Required for contractors and vendors.** Leave blank for FTEs. |

## 3. `delivery_metrics.csv`: one row per product

Use the **same last 3–6 completed sprints** for every number in a row.

| Column | What to enter | Where to find it |
|---|---|---|
| `sprint_length_weeks` | e.g. `2` (blank = 2) | Your sprint settings |
| `avg_velocity_points` | **Average points committed at sprint start.** This is the planned amount, *not* the completed amount. | Jira Velocity Chart: the **Commitment** bar · Azure DevOps Velocity widget: **Planned** |
| `carryover_pct` | Share of committed points **not finished** by sprint end: `(committed − completed) ÷ committed × 100`. If completed is more than committed, enter `0`. | Same chart: Commitment vs. Completed (Azure DevOps: Planned vs. Completed) |
| `rework_pct` | Share of completed points spent **fixing or redoing work already marked done**: bugs against shipped stories, reopened items. If bugs aren't pointed, use item counts instead. | A filter on bug and reopened issues within those sprints |
| `avg_throughput_items` | Average number of items completed per sprint, of any type | Sprint reports or a Done-items filter |
| `active_blockers` | How many items are blocked or impeded **right now** | Board filter for flagged or blocked items |
| `last_synced_utc` | When you pulled these numbers, e.g. `2026-11-21T15:00:00Z` | — |

> **Why committed, not completed?** We subtract carryover from velocity ourselves. If you enter completed points, unfinished work gets subtracted twice, and your runway looks worse than it is.

---

## Formatting rules (these prevent most back-and-forth)

- **Keep the header row exactly as it is.** Don't rename or reorder columns. Extra columns are ignored.
- **Dates:** `YYYY-MM-DD`, e.g. `2026-12-18`. In Sheets or Excel, format date columns as plain text or as custom `yyyy-mm-dd` before exporting, so they don't turn into `12/18/2026`.
- **Percentages:** plain numbers, e.g. `32`, not `32%` and not `0.32`.
- **Money:** `1400000`, `$1,400,000` and `1,400,000` all work.
- **No empty rows**, and no totals or notes rows at the bottom.
- **`product_id` must match exactly** across all three files.

## What happens with blanks

We never guess a missing number.
- **Blank `weekly_hours`:** treated as 40, and noted in the brief.
- **Blank `sprint_length_weeks`:** treated as 2, and noted in the brief.
- **Contractor with no `contract_end_date`:** flagged as "roll-off exposure unknown".
- **Any other blank or unreadable value:** listed as a data issue. The affected numbers show as `n/a` until it's fixed.

Within the first day, we'll send you a short list of anything that needs fixing. One round of fixes is usually enough.

## Exporting and sending

- **Google Sheets:** keep each table on its own tab. On each tab, choose **File → Download → Comma-separated values (.csv)**. This exports only the current tab.
- **Excel:** choose **File → Save As → CSV UTF-8 (Comma delimited)**, once per sheet.
- **Sending:** send the three files and your as-of date through a link with restricted access, not a public channel.

## Privacy, in short

| We need | We don't need |
|---|---|
| Budgets, allocation %, hourly rates (role-band averages OK), contract end dates, four sprint metrics per team | Real names, individual salaries, timesheets, access to Jira or Azure DevOps |

Our reports identify people **only by `member_id`**.
