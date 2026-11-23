# SprintRunway AI

**Budget-burn, runway, scope-fit and turnover-risk forecasting for engineering teams, without timesheets.**

SprintRunway joins agile delivery signals (velocity, carryover, rework, backlog) with the money side (remaining budget, blended rates, FTE vs. contractor mix, contract end dates, split allocations). The goal is to show engineering and product leaders a year-end budget cliff *before* it forces abrupt cuts.

## What it shows

- **When each product's budget runs out**, in Base, Optimistic and Slip scenarios, and how far that lands before or after the period end.
- **How much backlog fits** before the money runs out, and how many points are unfunded.
- **Risk flags:**
  - **False Velocity:** high carryover or rework hiding behind steady velocity.
  - **Context-Switching:** people split across products.
  - **Contractor Roll-Off:** contracts ending before period end.
- **Recommended actions** for Product Owners and leadership, such as a bridge-funding amount, a decide-by date for contractor renewals, or a descoping target.

## Quick start

Tested with Python 3.14.

```bash
git clone https://github.com/jmfaust76/sprint-runway-mvp.git
cd sprint-runway-mvp
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**Dashboard.** This opens at http://localhost:8501 and runs on the bundled sample data:

```bash
streamlit run src/app.py
```

The sidebar has controls for the as-of date, cliff buffer, Slip multiplier, scenario assumptions, the **Honor Contract End Dates** toggle and the data directory. Edits to the CSVs are picked up the next time you interact with or reload the page.

**Command line.** This writes a Markdown brief and a forecast CSV to `output/`:

```bash
python src/forecast_engine.py --as-of-date 2026-11-21
```

| Option | Default | Meaning |
|---|---|---|
| `--as-of-date` | today | Date the inputs are valid on (`YYYY-MM-DD`) |
| `--cliff-buffer-weeks` | `4` | Window after period end that still counts as Warning |
| `--slip-multiplier` | `1.2` | Weekly burn multiplier in the Slip case |
| `--honor-contract-end-dates` | off | Stop contractor burn after `contract_end_date` |
| `--data-dir` | `data/` | Folder containing the three input CSVs |
| `--output-dir` | `output/` | Where the brief and CSV are written |

## Inputs

Three CSV files:

| File | Contains |
|---|---|
| `budgets.csv` | Budget period, total and remaining budget, remaining backlog points per product |
| `roster_allocations.csv` | Allocation %, blended hourly rate, weekly hours, employment type and contract end date per person per product |
| `delivery_metrics.csv` | Sprint length, committed velocity, carryover %, rework %, throughput, active blockers and last-synced time per product |

Blank templates are in [`templates/`](templates/). The [fill-in guide](templates/FILL_IN_GUIDE.md) defines every column and explains how to pull the metrics from Jira or Azure DevOps.

**To use your own data**, keep it *outside* this repo and point the tool at it:

```bash
python src/forecast_engine.py --data-dir ~/sprintrunway-data/my-team
```

In the dashboard, the same setting is the **Data directory** field in the sidebar. Real budgets and rates don't belong in a public repository.

## How the forecast works

- **Burn:** `(allocation_pct / 100) × blended_hourly_rate × weekly_hours`, per person per product, split into FTE and contractor. No hour logs.
- **Net velocity:** `committed velocity × (1 − carryover%) × (1 − rework%)`.
- **Scenarios:**

  | Scenario | Burn | Velocity | Carryover / rework drag |
  |---|---|---|---|
  | Base | current | as reported | as reported |
  | Optimistic | same as Base | +10% | −50% |
  | Slip | ×1.20 | as reported | +20% |

- **Scope fit:** `affordable sprints × net velocity`, compared with the remaining backlog.
- **Risk tier:**
  - 🔴 **Critical:** Base runs out before period end.
  - 🟠 **Warning:** Slip runs out before period end, or Base runs out within the cliff buffer after it.
  - 🟢 **Healthy:** otherwise.
  - ⚪ **Insufficient Data:** budget, roster or period end is missing.
- **Flag thresholds:**
  - False Velocity at carryover ≥ 25% or rework ≥ 15%.
  - Context-Switching at any allocation below 100%.
  - Blocker escalation at 3 or more active blockers.

Every assumption in effect is printed in the dashboard and in the brief.

## Outputs

- **Dashboard** (`src/app.py`): KPI cards, a remaining-budget runway chart (Base vs. Slip), FTE vs. contractor burn, a per-product risk table, product details with recommended actions, and a "What Changed" summary against the last export.
- **`output/runway_and_risk_report.md`:** an executive brief.
- **`output/team_runway_forecast.csv`:** a machine-readable table with one row per product per scenario.

Exports identify people **only by `member_id`**. Names appear only in the local dashboard.

## Data principles

- **No timesheets.** Burn comes from allocations and rates.
- **No invented numbers.** Missing or invalid inputs are listed as data issues, never quietly filled in. The two documented defaults (40 h/week, 2-week sprints) are flagged whenever they're used.
- **No personal names in exports.**
- **The sample data is fictional:** a four-product portfolio with synthetic labels such as `Contractor Dev 1`.

## Project layout

```text
src/
  forecast_engine.py   # loaders, validation, forecasting math, exporters, CLI
  app.py               # Streamlit + Plotly dashboard
data/                  # fictional sample inputs
templates/             # blank input CSVs + fill-in guide
output/                # generated briefs and forecasts (gitignored)
```

## Roadmap

- Load the input tables from published Google Sheets, with a scheduled refresh.
- Read-only Jira Cloud and Azure DevOps connectors for delivery metrics.
- Hosted dashboard with Slack / Teams alerts when a product's risk tier changes.
- Throughput-based scope fit for teams that don't use story points.

## License

Copyright © 2026 jmfaust76. All rights reserved. The source is published for viewing; no license is granted to use, copy, modify or distribute it.
