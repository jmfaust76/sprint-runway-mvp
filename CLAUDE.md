# SprintRunway AI — Development Team Budget, Runway & Turnover-Risk Forecaster

## 1. Role & Product Objective
You are collaborating on **SprintRunway AI**, a budget-burn, runway, scope-fit, and turnover-risk forecasting tool for software engineering and product leaders (VPs of Engineering, Delivery Directors, Product Owners, and Scrum Masters).
Your goal is to connect Agile delivery signals (velocity, throughput, carryover/rework, backlog scope) with financial realities (remaining product budgets, blended rates, FTE vs. contractor mix, contract end dates, and split team allocations) so leaders spot year-end budget cliffs early—before abrupt cuts trigger team turnover and knowledge loss.

## 2. Workflow & Change-Control Rules
- **Plan Before Major Changes:** Always propose a concise implementation or architecture plan and wait for explicit user approval before creating, deleting, or heavily refactoring files.
- **No Fabricated Numbers:** Never invent budgets, rates, dates, or ticket counts not present in input data; surface missing values explicitly.
- **No Timesheet Assumption:** Never require individual developer hour logs. Calculate sprint and weekly burn from `(allocation_pct / 100) * blended_hourly_rate * weekly_hours`, scaled to `sprint_length_weeks`.

## 3. Core MVP Scope & Architecture Rules
- **Stage 1 (MVP):** Support local CSV / spreadsheet inputs (`budgets.csv`, `roster_allocations.csv`, `delivery_metrics.csv`) and generate:
  1. A local interactive Streamlit + Plotly web dashboard (`src/app.py`) with data-freshness timestamps and scenario controls.
  2. Structured exports in `/output`: an executive Markdown brief (`runway_and_risk_report.md`) and a machine-readable forecast table (`team_runway_forecast.csv`).
- **Stage 2 Ready:** Keep data ingestion modular behind clean loader functions so CSV/Google Sheet readers can later extend to scheduled Jira Cloud / Azure DevOps REST API syncs (hourly/daily) without rewriting forecasting math.
- **Export Privacy Rule:** Exported files (`output/*.md`, `output/*.csv`) MUST use `member_id` only and never include personal employee or contractor names. Full names may appear only in the local interactive dashboard UI.

## 4. Core Forecasting Math & Risk Guardrails
For each product/squad as of the configured `--as-of-date`:
- **Burn & Scenario Runway Projections:**
  - **Base Case:** Current weekly/sprint burn rate and trailing velocity/throughput adjusted for carryover/rework drag.
  - **Optimistic Case:** Burn rate equal to Base (same depletion date), with +10% velocity upside and 50% lower carryover/rework drag to show scope-fit upside.
  - **Slip / Rework Risk Case:** Applies the configurable slip/rework multiplier (default `1.20x`) to weekly burn (modeling surge/backfill cost) plus +20% drag on net velocity.
  - Compute exact **Projected Depletion Date** and **Runway Variance (Days/Weeks)** relative to `period_end`.
- **Contractor Roll-Off & Turnover Cliff Signals:**
  - Explicitly evaluate `contract_end_date` in `roster_allocations.csv`. When any contractor's `contract_end_date` precedes `period_end` (or falls within the cliff buffer), raise an explicit **Contractor Roll-Off / Knowledge-Loss Risk Flag**.
  - Keep burn flat through `period_end` by default (assuming backfill/extension cost), while supporting an optional toggle (`Honor Contract End Dates`) to model headcount drop-off.
- **Scope & Quality Guardrails:**
  - Compare `remaining_backlog_points` against affordable capacity before depletion (`affordable_sprints * effective_net_velocity`).
  - Flag **False Velocity** when nominal velocity is steady but `carryover_pct` or `rework_pct` is high, and **Context-Switching Risk** when team members are split across products (`allocation_pct < 100%`).

## 5. Business Plan & Go-To-Market (GTM) Support
When asked to work on business strategy, investor/pilot materials, or `BUSINESS_PLAN.md`, anchor recommendations on:
- **ICP & Buyers:** Mid-market and enterprise Engineering VPs, PMO/Delivery Directors, Product Owners, and Scrum Masters running multi-squad Agile teams with mixed FTE/contractor rosters.
- **Pilot Positioning:** "Zero-IT-friction CSV/Sheet diagnostic in 48 hours" (Stage 1) before requesting read-only Jira/Azure DevOps API access (Stage 2).
- **Packaging & Roadmap:** Free/Diagnostic Snapshot $\rightarrow$ Team/Portfolio Subscription (automated daily/hourly Jira & ADO sync + Slack/Teams cliff alerts) $\rightarrow$ Enterprise Governance.
