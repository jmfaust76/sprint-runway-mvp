"""SprintRunway AI — forecasting engine.

Layers (top to bottom):
  1. Data sources  — `CsvSource` today; a Jira / Azure DevOps / Google Sheets source
                     only needs the same three methods (Stage 2).
  2. Validation    — schema + type checks; missing values become `DataIssue`s,
                     never invented defaults.
  3. Forecasting   — deterministic, pure functions over DataFrames + `ForecastConfig`.
  4. Exporters     — Markdown brief + machine-readable CSV (member_id only, no names).

CLI:
  python src/forecast_engine.py --as-of-date 2026-11-21 --cliff-buffer-weeks 4 --slip-multiplier 1.2
"""

from __future__ import annotations

import argparse
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"

REPORT_FILENAME = "runway_and_risk_report.md"
FORECAST_FILENAME = "team_runway_forecast.csv"
SNAPSHOT_FILENAME = ".last_run_snapshot.csv"

SCENARIOS = ("Base", "Optimistic", "Slip")
TIER_ORDER = {"Critical": 0, "Warning": 1, "Insufficient Data": 2, "Healthy": 3}

# Hard cap on day-by-day simulation so a near-zero burn cannot loop forever.
SIMULATION_HORIZON_DAYS = 3 * 365

BUDGET_COLUMNS = [
    "product_id", "product_name", "period_start", "period_end",
    "total_annual_budget", "remaining_budget", "remaining_backlog_points",
]
ROSTER_COLUMNS = [
    "member_id", "name", "role", "employment_type", "product_id",
    "allocation_pct", "blended_hourly_rate", "weekly_hours", "contract_end_date",
]
METRICS_COLUMNS = [
    "product_id", "sprint_length_weeks", "avg_velocity_points", "avg_throughput_items",
    "carryover_pct", "rework_pct", "active_blockers", "last_synced_utc",
]


# --------------------------------------------------------------------------------------
# Configuration & result containers
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ForecastConfig:
    as_of_date: date = field(default_factory=date.today)
    cliff_buffer_weeks: float = 4.0
    # Slip case: multiplier on weekly burn (deadline surge / backfill cost) ...
    slip_cost_multiplier: float = 1.20
    # ... plus this relative increase in carryover/rework drag on net velocity.
    slip_drag_increase: float = 0.20
    # Optimistic case: burn equals Base; velocity uplift and drag reduction only.
    optimistic_velocity_uplift: float = 0.10
    optimistic_drag_reduction: float = 0.50
    # Documented fallbacks (logged as assumptions, never silent).
    default_weekly_hours: float = 40.0
    default_sprint_length_weeks: float = 2.0
    # Quality guardrail thresholds.
    false_velocity_carryover_pct: float = 25.0
    false_velocity_rework_pct: float = 15.0
    blocker_escalation_threshold: int = 3
    # Optional: stop contractor burn after contract_end_date (dashboard toggle).
    honor_contract_end_dates: bool = False

    def assumptions(self) -> list[str]:
        lines = [
            f"As-of date: {self.as_of_date.isoformat()}; `remaining_budget` is treated as valid on this "
            "date (no unrecorded spend is estimated).",
            "Burn = (allocation_pct / 100) x blended_hourly_rate x weekly_hours; no timesheets used. "
            "Weekly burn is spread evenly across calendar days.",
            f"Base: current burn; net velocity = velocity x (1 - carryover%) x (1 - rework%).",
            f"Optimistic: burn = Base (same depletion date); velocity +{self.optimistic_velocity_uplift:.0%}, "
            f"carryover/rework drag -{self.optimistic_drag_reduction:.0%}.",
            f"Slip / Rework: weekly burn x{self.slip_cost_multiplier:.2f} (surge/backfill cost); "
            f"carryover/rework drag +{self.slip_drag_increase:.0%}.",
            f"Cliff buffer: {self.cliff_buffer_weeks:g} weeks. Critical = Base depletes before period_end; "
            "Warning = Slip depletes before period_end, or Base depletes within the buffer after period_end.",
            "Scope fit: deliverable points = affordable sprints (until budget exhaustion) x effective net velocity.",
            f"False Velocity flag: carryover >= {self.false_velocity_carryover_pct:g}% or rework >= "
            f"{self.false_velocity_rework_pct:g}%.",
        ]
        if self.honor_contract_end_dates:
            lines.append(
                "Honor Contract End Dates: ON — contractor burn stops after contract_end_date. Velocity is NOT "
                "reduced for roll-off, so scope-fit under this toggle is an upper bound."
            )
        else:
            lines.append(
                "Honor Contract End Dates: OFF — burn held flat through period_end (work assumed to continue via "
                "renewal or backfill); roll-off surfaces as a Turnover Risk flag."
            )
        return lines


@dataclass
class DataIssue:
    source: str
    product_id: str | None
    field: str
    message: str

    def __str__(self) -> str:
        where = f"{self.product_id} · " if self.product_id else ""
        return f"[{self.source}] {where}{self.field}: {self.message}"


@dataclass
class InputBundle:
    budgets: pd.DataFrame
    roster: pd.DataFrame
    metrics: pd.DataFrame
    issues: list[DataIssue]
    source_label: str
    loaded_at_utc: datetime


@dataclass
class ForecastResult:
    config: ForecastConfig
    products: pd.DataFrame          # one row per product (summary + flags + actions)
    scenarios: pd.DataFrame         # one row per product x scenario
    member_burn: pd.DataFrame       # one row per roster allocation (includes names — dashboard only)
    issues: list[DataIssue]
    freshness_warnings: list[str]
    kpis: dict
    delta_summary: list[str]
    generated_at_utc: datetime


# --------------------------------------------------------------------------------------
# 1. Data sources (Stage 2 swap point)
# --------------------------------------------------------------------------------------
class DataSource(Protocol):
    label: str

    def budgets(self) -> pd.DataFrame: ...
    def roster(self) -> pd.DataFrame: ...
    def delivery_metrics(self) -> pd.DataFrame: ...


class CsvSource:
    """Reads the three MVP CSVs from a directory."""

    FILES = {
        "budgets": "budgets.csv",
        "roster": "roster_allocations.csv",
        "delivery_metrics": "delivery_metrics.csv",
    }

    def __init__(self, data_dir: str | Path = DEFAULT_DATA_DIR):
        self.data_dir = Path(data_dir)
        self.label = f"CSV: {self.data_dir}"

    def _read(self, key: str) -> pd.DataFrame:
        return pd.read_csv(self.data_dir / self.FILES[key], dtype=str, keep_default_na=False)

    def budgets(self) -> pd.DataFrame:
        return self._read("budgets")

    def roster(self) -> pd.DataFrame:
        return self._read("roster")

    def delivery_metrics(self) -> pd.DataFrame:
        return self._read("delivery_metrics")

    def file_mtimes(self) -> tuple[float, ...]:
        """Used by the dashboard as a cache key so CSV edits trigger a refresh."""
        return tuple((self.data_dir / f).stat().st_mtime for f in self.FILES.values())


# --------------------------------------------------------------------------------------
# 2. Validation
# --------------------------------------------------------------------------------------
def _blank(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value)) or str(value).strip() == ""


def _require_columns(df: pd.DataFrame, required: list[str], source: str, issues: list[DataIssue]) -> pd.DataFrame:
    missing = [c for c in required if c not in df.columns]
    for col in missing:
        issues.append(DataIssue(source, None, col, "required column missing from input"))
    df = df.copy()
    for col in missing:
        df[col] = ""
    return df


def _parse_numeric(df, cols, source, issues, id_col="product_id", allow_blank=()):
    for col in cols:
        parsed = []
        for _, row in df.iterrows():
            raw = row[col]
            if _blank(raw):
                if col not in allow_blank:
                    issues.append(DataIssue(source, row.get(id_col) or None, col, "missing value"))
                parsed.append(float("nan"))
                continue
            try:
                parsed.append(float(str(raw).replace(",", "").replace("$", "")))
            except ValueError:
                issues.append(DataIssue(source, row.get(id_col) or None, col, f"not a number: {raw!r}"))
                parsed.append(float("nan"))
        df[col] = pd.Series(parsed, index=df.index, dtype="float64")
    return df


def _parse_dates(df, cols, source, issues, id_col="product_id", allow_blank=()):
    for col in cols:
        parsed = []
        for _, row in df.iterrows():
            raw = row[col]
            if _blank(raw):
                if col not in allow_blank:
                    issues.append(DataIssue(source, row.get(id_col) or None, col, "missing date"))
                parsed.append(None)
                continue
            try:
                parsed.append(date.fromisoformat(str(raw).strip()[:10]))
            except ValueError:
                issues.append(DataIssue(source, row.get(id_col) or None, col, f"invalid date: {raw!r}"))
                parsed.append(None)
        df[col] = pd.Series(parsed, index=df.index, dtype="object")
    return df


def validate_budgets(raw: pd.DataFrame, issues: list[DataIssue]) -> pd.DataFrame:
    df = _require_columns(raw, BUDGET_COLUMNS, "budgets", issues)
    df = _parse_numeric(df, ["total_annual_budget", "remaining_budget", "remaining_backlog_points"], "budgets", issues)
    df = _parse_dates(df, ["period_start", "period_end"], "budgets", issues)
    dupes = df["product_id"][df["product_id"].duplicated()].unique()
    for pid in dupes:
        issues.append(DataIssue("budgets", pid, "product_id", "duplicate product row; first row used"))
    return df.drop_duplicates("product_id", keep="first").reset_index(drop=True)


def validate_roster(raw: pd.DataFrame, cfg: ForecastConfig, issues: list[DataIssue]) -> pd.DataFrame:
    df = _require_columns(raw, ROSTER_COLUMNS, "roster", issues)
    df = _parse_numeric(df, ["allocation_pct", "blended_hourly_rate"], "roster", issues, id_col="product_id")
    df = _parse_numeric(df, ["weekly_hours"], "roster", issues, allow_blank=("weekly_hours",))
    filled = df["weekly_hours"].isna()
    for _, row in df[filled].iterrows():
        issues.append(DataIssue("roster", row["product_id"], "weekly_hours",
                                f"{row['member_id']} blank — documented default {cfg.default_weekly_hours:g} h/wk applied"))
    df.loc[filled, "weekly_hours"] = cfg.default_weekly_hours
    df = _parse_dates(df, ["contract_end_date"], "roster", issues, allow_blank=("contract_end_date",))
    df["employment_type"] = df["employment_type"].astype(str).str.strip()
    df["is_contractor"] = df["employment_type"].str.lower().isin(["contractor", "vendor", "contract"])

    for _, row in df.iterrows():
        if row["is_contractor"] and row["contract_end_date"] is None:
            issues.append(DataIssue("roster", row["product_id"], "contract_end_date",
                                    f"{row['member_id']} is a contractor with no contract_end_date — roll-off exposure unknown"))
    totals = df.groupby("member_id")["allocation_pct"].sum()
    for mid, total in totals.items():
        if total > 100:
            issues.append(DataIssue("roster", None, "allocation_pct", f"{mid} allocated {total:g}% in total (>100%)"))
    return df


def validate_metrics(raw: pd.DataFrame, cfg: ForecastConfig, issues: list[DataIssue]) -> pd.DataFrame:
    df = _require_columns(raw, METRICS_COLUMNS, "delivery_metrics", issues)
    df = _parse_numeric(df, ["avg_velocity_points", "avg_throughput_items", "carryover_pct", "rework_pct",
                             "active_blockers"], "delivery_metrics", issues)
    df = _parse_numeric(df, ["sprint_length_weeks"], "delivery_metrics", issues, allow_blank=("sprint_length_weeks",))
    filled = df["sprint_length_weeks"].isna()
    for _, row in df[filled].iterrows():
        issues.append(DataIssue("delivery_metrics", row["product_id"], "sprint_length_weeks",
                                f"blank — documented default {cfg.default_sprint_length_weeks:g} weeks applied"))
    df.loc[filled, "sprint_length_weeks"] = cfg.default_sprint_length_weeks

    synced = []
    for _, row in df.iterrows():
        raw_ts = row["last_synced_utc"]
        if _blank(raw_ts):
            issues.append(DataIssue("delivery_metrics", row["product_id"], "last_synced_utc", "missing sync timestamp"))
            synced.append(None)
            continue
        try:
            ts = datetime.fromisoformat(str(raw_ts).strip().replace("Z", "+00:00"))
            synced.append(ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))
        except ValueError:
            issues.append(DataIssue("delivery_metrics", row["product_id"], "last_synced_utc", f"invalid timestamp: {raw_ts!r}"))
            synced.append(None)
    df["last_synced_utc"] = pd.Series(synced, index=df.index, dtype="object")
    return df.drop_duplicates("product_id", keep="first").reset_index(drop=True)


def load_all(source: DataSource | str | Path = DEFAULT_DATA_DIR, cfg: ForecastConfig | None = None) -> InputBundle:
    cfg = cfg or ForecastConfig()
    if isinstance(source, (str, Path)):
        source = CsvSource(source)
    issues: list[DataIssue] = []
    budgets = validate_budgets(source.budgets(), issues)
    roster = validate_roster(source.roster(), cfg, issues)
    metrics = validate_metrics(source.delivery_metrics(), cfg, issues)

    known = set(budgets["product_id"])
    for pid in sorted(set(roster["product_id"]) - known):
        issues.append(DataIssue("roster", pid, "product_id", "allocations reference a product with no budget row"))
    for pid in sorted(set(metrics["product_id"]) - known):
        issues.append(DataIssue("delivery_metrics", pid, "product_id", "metrics reference a product with no budget row"))
    for pid in sorted(known - set(roster["product_id"])):
        issues.append(DataIssue("roster", pid, "allocations", "no roster allocations — burn cannot be computed"))
    for pid in sorted(known - set(metrics["product_id"])):
        issues.append(DataIssue("delivery_metrics", pid, "metrics", "no delivery metrics — scope fit cannot be computed"))

    return InputBundle(budgets, roster, metrics, issues, source.label, datetime.now(timezone.utc))


# --------------------------------------------------------------------------------------
# 3. Forecasting (pure functions)
# --------------------------------------------------------------------------------------
def compute_member_burn(roster: pd.DataFrame, metrics: pd.DataFrame) -> pd.DataFrame:
    """Per-allocation weekly and sprint cost, tagged FTE vs Contractor."""
    df = roster.copy()
    df["weekly_cost"] = (df["allocation_pct"] / 100.0) * df["blended_hourly_rate"] * df["weekly_hours"]
    sprint_len = metrics.set_index("product_id")["sprint_length_weeks"]
    df["sprint_length_weeks"] = df["product_id"].map(sprint_len)
    df["sprint_cost"] = df["weekly_cost"] * df["sprint_length_weeks"]
    df["cost_type"] = df["is_contractor"].map({True: "Contractor", False: "FTE"})
    return df


def compute_product_burn(member_burn: pd.DataFrame, product_ids: list[str]) -> pd.DataFrame:
    weekly = member_burn.pivot_table(index="product_id", columns="cost_type", values="weekly_cost",
                                     aggfunc="sum", fill_value=0.0)
    out = pd.DataFrame(index=pd.Index(product_ids, name="product_id"))
    out["fte_weekly_burn"] = weekly.get("FTE", pd.Series(dtype=float)).reindex(out.index).fillna(0.0)
    out["contractor_weekly_burn"] = weekly.get("Contractor", pd.Series(dtype=float)).reindex(out.index).fillna(0.0)
    out["weekly_burn"] = out["fte_weekly_burn"] + out["contractor_weekly_burn"]
    out["contractor_share_pct"] = (out["contractor_weekly_burn"] / out["weekly_burn"].where(out["weekly_burn"] > 0) * 100)
    out["has_roster"] = out.index.isin(member_burn["product_id"].unique())
    return out.reset_index()


def net_velocity(velocity: float, carryover_pct: float, rework_pct: float,
                 velocity_mult: float = 1.0, drag_mult: float = 1.0) -> float:
    """velocity x (1 - carryover) x (1 - rework), with scenario multipliers; drag capped at 95%."""
    if any(pd.isna(v) for v in (velocity, carryover_pct, rework_pct)):
        return float("nan")
    carry = min(carryover_pct / 100.0 * drag_mult, 0.95)
    rework = min(rework_pct / 100.0 * drag_mult, 0.95)
    return velocity * velocity_mult * (1 - carry) * (1 - rework)


def daily_burn_schedule(members: pd.DataFrame, as_of: date, horizon_days: int,
                        burn_mult: float, honor_contract_end: bool) -> list[float]:
    """Daily spend for day d = as_of + d. Contractors billed through contract_end_date inclusive."""
    schedule = [0.0] * horizon_days
    for _, m in members.iterrows():
        daily = m["weekly_cost"] / 7.0 * burn_mult
        if pd.isna(daily) or daily == 0:
            continue
        end = m["contract_end_date"] if (honor_contract_end and m["is_contractor"]) else None
        last_day = horizon_days if end is None else max(0, min(horizon_days, (end - as_of).days + 1))
        for d in range(last_day):
            schedule[d] += daily
    return schedule


def simulate_runway(remaining_budget: float, schedule: list[float]) -> float | None:
    """Fractional days until cumulative spend >= remaining budget; None if not within horizon."""
    if pd.isna(remaining_budget):
        return None
    if remaining_budget <= 0:
        return 0.0
    spent = 0.0
    for d, burn in enumerate(schedule):
        if burn > 0 and spent + burn >= remaining_budget:
            return d + (remaining_budget - spent) / burn
        spent += burn
    return None


def scenario_parameters(cfg: ForecastConfig) -> dict[str, dict[str, float]]:
    return {
        "Base": {"burn_mult": 1.0, "velocity_mult": 1.0, "drag_mult": 1.0},
        "Optimistic": {"burn_mult": 1.0, "velocity_mult": 1.0 + cfg.optimistic_velocity_uplift,
                       "drag_mult": 1.0 - cfg.optimistic_drag_reduction},
        "Slip": {"burn_mult": cfg.slip_cost_multiplier, "velocity_mult": 1.0,
                 "drag_mult": 1.0 + cfg.slip_drag_increase},
    }


def project_scenarios(budgets: pd.DataFrame, metrics: pd.DataFrame, member_burn: pd.DataFrame,
                      product_burn: pd.DataFrame, cfg: ForecastConfig) -> pd.DataFrame:
    """One row per product x scenario: burn, depletion date, runway variance, scope fit."""
    as_of = cfg.as_of_date
    m_idx = metrics.set_index("product_id")
    pb_idx = product_burn.set_index("product_id")
    rows = []
    for _, b in budgets.iterrows():
        pid = b["product_id"]
        period_end: date | None = b["period_end"]
        members = member_burn[member_burn["product_id"] == pid]
        met = m_idx.loc[pid] if pid in m_idx.index else None
        sprint_len = met["sprint_length_weeks"] if met is not None else float("nan")
        days_to_pe = (period_end - as_of).days if period_end else None
        horizon = max(SIMULATION_HORIZON_DAYS, (days_to_pe or 0) + 1)

        for scen, p in scenario_parameters(cfg).items():
            schedule = daily_burn_schedule(members, as_of, horizon, p["burn_mult"], cfg.honor_contract_end_dates)
            fte = pb_idx.loc[pid, "fte_weekly_burn"] * p["burn_mult"]
            con = pb_idx.loc[pid, "contractor_weekly_burn"] * p["burn_mult"]
            runway_days = simulate_runway(b["remaining_budget"], schedule) if members.shape[0] else None
            depletion = as_of + timedelta(days=math.floor(runway_days)) if runway_days is not None else None

            spend_to_pe = sum(schedule[: max(days_to_pe, 0)]) if days_to_pe is not None else float("nan")
            gap_at_pe = b["remaining_budget"] - spend_to_pe  # + surplus / - shortfall at period_end
            variance_days = (depletion - period_end).days if (depletion and period_end) else None

            nv = float("nan")
            if met is not None:
                nv = net_velocity(met["avg_velocity_points"], met["carryover_pct"], met["rework_pct"],
                                  p["velocity_mult"], p["drag_mult"])
            affordable_sprints = (runway_days / 7.0 / sprint_len) if (runway_days is not None and sprint_len > 0) else float("nan")
            deliverable = affordable_sprints * nv
            backlog = b["remaining_backlog_points"]
            unfunded = max(backlog - deliverable, 0.0) if not (pd.isna(backlog) or pd.isna(deliverable)) else float("nan")

            rows.append({
                "product_id": pid,
                "scenario": scen,
                "weekly_burn": fte + con,
                "sprint_burn": (fte + con) * sprint_len,
                "fte_weekly_burn": fte,
                "contractor_weekly_burn": con,
                "runway_days": runway_days,
                "runway_weeks": runway_days / 7.0 if runway_days is not None else None,
                "projected_depletion_date": depletion,
                "runway_variance_days": variance_days,
                "runway_variance_weeks": variance_days / 7.0 if variance_days is not None else None,
                "spend_to_period_end": spend_to_pe,
                "budget_gap_at_period_end": gap_at_pe,
                "effective_net_velocity": nv,
                "affordable_sprints": affordable_sprints,
                "deliverable_points": deliverable,
                "remaining_backlog_points": backlog,
                "unfunded_backlog_points": unfunded,
            })
    return pd.DataFrame(rows)


def flag_false_velocity(metrics_row, cfg: ForecastConfig) -> str | None:
    if metrics_row is None:
        return None
    c, r = metrics_row["carryover_pct"], metrics_row["rework_pct"]
    if pd.isna(c) or pd.isna(r):
        return None
    if c >= cfg.false_velocity_carryover_pct or r >= cfg.false_velocity_rework_pct:
        return (f"Nominal velocity {metrics_row['avg_velocity_points']:g} pts/sprint overstates delivery: "
                f"carryover {c:g}%, rework {r:g}%")
    return None


def flag_context_switching(member_burn: pd.DataFrame, pid: str) -> list[dict]:
    """Members on this product with allocation < 100%, and where the rest of their time goes."""
    out = []
    here = member_burn[(member_burn["product_id"] == pid) & (member_burn["allocation_pct"] < 100)]
    for _, m in here.iterrows():
        others = member_burn[(member_burn["member_id"] == m["member_id"]) & (member_burn["product_id"] != pid)]
        total = member_burn.loc[member_burn["member_id"] == m["member_id"], "allocation_pct"].sum()
        out.append({
            "member_id": m["member_id"], "name": m["name"], "role": m["role"],
            "allocation_pct": m["allocation_pct"],
            "other_products": others["product_id"].tolist(),
            "unallocated_pct": max(100 - total, 0),
        })
    return out


def flag_contractor_rolloff(member_burn: pd.DataFrame, pid: str, period_end: date | None,
                            product_weekly_burn: float) -> list[dict]:
    if period_end is None:
        return []
    rows = member_burn[(member_burn["product_id"] == pid) & member_burn["is_contractor"]]
    out = []
    for _, m in rows.iterrows():
        end = m["contract_end_date"]
        if end is not None and end < period_end:
            out.append({
                "member_id": m["member_id"], "name": m["name"], "role": m["role"],
                "contract_end_date": end,
                "weekly_cost": m["weekly_cost"],
                "share_of_burn_pct": (m["weekly_cost"] / product_weekly_burn * 100) if product_weekly_burn else float("nan"),
            })
    return sorted(out, key=lambda x: x["contract_end_date"])


def classify_cliff_risk(base_depletion: date | None, slip_depletion: date | None,
                        period_end: date | None, cfg: ForecastConfig, has_data: bool) -> tuple[str, str]:
    if not has_data or period_end is None:
        return "Insufficient Data", "Budget, burn or period_end missing — risk cannot be assessed."
    buffer_end = period_end + timedelta(weeks=cfg.cliff_buffer_weeks)
    if base_depletion is not None and base_depletion < period_end:
        return "Critical", f"Base case depletes {base_depletion:%b %d} — {(period_end - base_depletion).days} days before period end."
    if slip_depletion is not None and slip_depletion < period_end:
        return "Warning", f"Slip case depletes {slip_depletion:%b %d}, before period end; Base holds to {base_depletion:%b %d, %Y}."
    if base_depletion is not None and base_depletion <= buffer_end:
        return "Warning", f"Base case depletes {base_depletion:%b %d, %Y}, inside the {cfg.cliff_buffer_weeks:g}-week buffer after period end."
    return "Healthy", "Runway extends beyond period end plus buffer in Base and Slip cases."


def has_flag(value) -> bool:
    """True for a non-empty flag; DataFrames coerce a None flag to NaN, which is truthy."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    return bool(value)


def _money(x: float) -> str:
    if pd.isna(x):
        return "n/a"
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.0f}"


def recommend_actions(p: dict, cfg: ForecastConfig) -> list[str]:
    """Rule-based PO / Leadership actions. Uses member_id only (export-safe)."""
    acts: list[str] = []
    tier = p["risk_tier"]
    base_gap, slip_gap = p["base_gap_at_period_end"], p["slip_gap_at_period_end"]
    if tier == "Critical":
        acts.append(f"Leadership: approve bridge funding of at least {_money(-base_gap)} to reach period end "
                    f"(Slip case needs {_money(-slip_gap)}), or plan a controlled ramp-down now.")
    elif tier == "Warning" and not pd.isna(slip_gap) and slip_gap < 0:
        acts.append(f"Leadership: pre-approve a {_money(-slip_gap)} contingency in case slip/rework surge materialises.")
    elif tier == "Warning":
        acts.append("Leadership: confirm Q1 funding before period end — Base runway ends inside the cliff buffer.")
    if p["rolloff"]:
        first = p["rolloff"][0]["contract_end_date"]
        ids = ", ".join(r["member_id"] for r in p["rolloff"])
        decide_by = first - timedelta(weeks=2)
        acts.append(f"Leadership: decide renew / convert / release for {ids} by {decide_by:%b %d} "
                    f"(first roll-off {first:%b %d}); schedule knowledge-transfer sessions now.")
    if not pd.isna(p["base_unfunded_points"]) and p["base_unfunded_points"] > 0:
        acts.append(f"PO: descope or defer ~{p['base_unfunded_points']:.0f} of {p['remaining_backlog_points']:.0f} "
                    f"backlog points; re-sequence so the release-critical items land first.")
    if has_flag(p["false_velocity"]):
        acts.append("PO / Scrum Master: run a carryover & rework root-cause; tighten Definition of Ready and cap WIP "
                    "before committing to further scope.")
    if p["context_switching"]:
        ids = ", ".join(c["member_id"] for c in p["context_switching"])
        acts.append(f"Leadership: review split allocations ({ids}); consolidate where possible to cut context-switch drag.")
    if not pd.isna(p["active_blockers"]) and p["active_blockers"] >= cfg.blocker_escalation_threshold:
        acts.append(f"PO: escalate {p['active_blockers']:.0f} active blockers at the next leadership sync.")
    if tier == "Healthy" and not pd.isna(base_gap) and base_gap > 0:
        acts.append(f"Leadership: projected surplus of {_money(base_gap)} at period end — confirm carry-over rules "
                    "or reallocate to at-risk products.")
    return acts or ["No action required — keep monitoring."]


def check_freshness(metrics: pd.DataFrame, as_of: date) -> list[str]:
    warnings = []
    for _, row in metrics.iterrows():
        ts = row["last_synced_utc"]
        if ts is None:
            warnings.append(f"{row['product_id']}: no last_synced_utc — data freshness unknown.")
        elif ts.date() != as_of:
            diff = (as_of - ts.date()).days
            direction = "after" if diff > 0 else "before"
            warnings.append(f"{row['product_id']}: as-of date {as_of} is {abs(diff)} day(s) {direction} last sync "
                            f"({ts:%Y-%m-%d %H:%M} UTC). remaining_budget is not adjusted for the gap.")
    return warnings


def compute_delta(products: pd.DataFrame, snapshot_path: Path) -> list[str]:
    """Compare against the last exported run. Never invents a baseline."""
    if not snapshot_path.exists():
        return ["No prior baseline — this is the first exported run. Deltas will appear after the next export."]
    prev = pd.read_csv(snapshot_path, dtype={"product_id": str})
    prev = prev.set_index("product_id")
    lines: list[str] = []
    prev_asof = prev["as_of_date"].iloc[0] if "as_of_date" in prev.columns and len(prev) else "unknown"
    for _, p in products.iterrows():
        pid = p["product_id"]
        if pid not in prev.index:
            lines.append(f"{pid}: new product since last run.")
            continue
        q = prev.loc[pid]
        bits = []
        db = p["remaining_budget"] - q["remaining_budget"]
        if abs(db) >= 1:
            bits.append(f"remaining budget {_money(db)}")
        dw = p["base_weekly_burn"] - q["base_weekly_burn"]
        if abs(dw) >= 1:
            bits.append(f"weekly burn {'+' if dw > 0 else ''}{_money(dw)}")
        if pd.notna(q.get("base_depletion_date")) and p["base_depletion_date"] is not None:
            shift = (p["base_depletion_date"] - date.fromisoformat(str(q["base_depletion_date"]))).days
            if shift:
                bits.append(f"Base depletion {'+' if shift > 0 else ''}{shift} days")
        if q["risk_tier"] != p["risk_tier"]:
            bits.append(f"risk {q['risk_tier']} → {p['risk_tier']}")
        du = p["base_unfunded_points"] - q["base_unfunded_points"]
        if pd.notna(du) and abs(du) >= 0.5:
            bits.append(f"unfunded points {'+' if du > 0 else ''}{du:.0f}")
        if bits:
            lines.append(f"{pid}: " + "; ".join(bits) + ".")
    for pid in sorted(set(prev.index) - set(products["product_id"])):
        lines.append(f"{pid}: removed since last run.")
    header = f"Compared with last exported run (as-of {prev_asof}):"
    return [header] + (lines or ["No material changes."])


def key_drivers(products: pd.DataFrame) -> list[str]:
    """Plain-language drivers of current risk, derived only from computed values."""
    out = []
    for _, p in products.sort_values("tier_rank").iterrows():
        if p["risk_tier"] not in ("Critical", "Warning"):
            continue
        parts = [f"**{p['product_id']} ({p['risk_tier']})** — {p['risk_reason'].rstrip('.')}"]
        if not pd.isna(p["contractor_share_pct"]):
            parts.append(f"Contractors are {p['contractor_share_pct']:.0f}% of burn")
        if p["rolloff"]:
            parts.append(f"{len(p['rolloff'])} contractor(s) roll off before period end")
        if has_flag(p["false_velocity"]):
            parts.append(f"carryover {p['carryover_pct']:g}% / rework {p['rework_pct']:g}% depress net velocity to "
                         f"{p['base_net_velocity']:.1f} pts/sprint")
        out.append("; ".join(parts) + ".")
    healthy = products[products["risk_tier"] == "Healthy"]
    if len(healthy):
        surplus = healthy["base_gap_at_period_end"].clip(lower=0).sum()
        out.append(f"{len(healthy)} product(s) healthy, carrying a combined projected surplus of {_money(surplus)} at "
                   "period end — a possible reallocation source.")
    return out


def run_forecast(bundle: InputBundle, cfg: ForecastConfig, output_dir: Path = DEFAULT_OUTPUT_DIR) -> ForecastResult:
    budgets, roster, metrics = bundle.budgets, bundle.roster, bundle.metrics
    member_burn = compute_member_burn(roster, metrics)
    product_ids = budgets["product_id"].tolist()
    product_burn = compute_product_burn(member_burn, product_ids)
    scen = project_scenarios(budgets, metrics, member_burn, product_burn, cfg)

    m_idx = metrics.set_index("product_id")
    pb_idx = product_burn.set_index("product_id")
    s_idx = scen.set_index(["product_id", "scenario"])
    rows = []
    for _, b in budgets.iterrows():
        pid = b["product_id"]
        met = m_idx.loc[pid] if pid in m_idx.index else None
        base, opt, slip = (s_idx.loc[(pid, s)] for s in SCENARIOS)
        has_data = bool(pb_idx.loc[pid, "has_roster"]) and not pd.isna(b["remaining_budget"])
        tier, reason = classify_cliff_risk(base["projected_depletion_date"], slip["projected_depletion_date"],
                                           b["period_end"], cfg, has_data)
        p = {
            "product_id": pid,
            "product_name": b["product_name"],
            "period_end": b["period_end"],
            "total_annual_budget": b["total_annual_budget"],
            "remaining_budget": b["remaining_budget"],
            "remaining_backlog_points": b["remaining_backlog_points"],
            "sprint_length_weeks": met["sprint_length_weeks"] if met is not None else float("nan"),
            "avg_velocity_points": met["avg_velocity_points"] if met is not None else float("nan"),
            "carryover_pct": met["carryover_pct"] if met is not None else float("nan"),
            "rework_pct": met["rework_pct"] if met is not None else float("nan"),
            "active_blockers": met["active_blockers"] if met is not None else float("nan"),
            "last_synced_utc": met["last_synced_utc"] if met is not None else None,
            "fte_weekly_burn": pb_idx.loc[pid, "fte_weekly_burn"],
            "contractor_weekly_burn": pb_idx.loc[pid, "contractor_weekly_burn"],
            "base_weekly_burn": base["weekly_burn"],
            "base_sprint_burn": base["sprint_burn"],
            "contractor_share_pct": pb_idx.loc[pid, "contractor_share_pct"],
            "base_depletion_date": base["projected_depletion_date"],
            "optimistic_depletion_date": opt["projected_depletion_date"],
            "slip_depletion_date": slip["projected_depletion_date"],
            "base_variance_weeks": base["runway_variance_weeks"],
            "slip_variance_weeks": slip["runway_variance_weeks"],
            "base_gap_at_period_end": base["budget_gap_at_period_end"],
            "slip_gap_at_period_end": slip["budget_gap_at_period_end"],
            "base_net_velocity": base["effective_net_velocity"],
            "base_deliverable_points": base["deliverable_points"],
            "optimistic_unfunded_points": opt["unfunded_backlog_points"],
            "base_unfunded_points": base["unfunded_backlog_points"],
            "slip_unfunded_points": slip["unfunded_backlog_points"],
            "risk_tier": tier,
            "tier_rank": TIER_ORDER[tier],
            "risk_reason": reason,
            "false_velocity": flag_false_velocity(met, cfg),
            "context_switching": flag_context_switching(member_burn, pid),
            "rolloff": flag_contractor_rolloff(member_burn, pid, b["period_end"], pb_idx.loc[pid, "weekly_burn"]),
        }
        p["actions"] = recommend_actions(p, cfg)
        rows.append(p)
    products = pd.DataFrame(rows).sort_values(["tier_rank", "base_depletion_date"], na_position="last").reset_index(drop=True)

    kpis = {
        "total_remaining_budget": float(products["remaining_budget"].sum(skipna=True)),
        "portfolio_weekly_burn": float(products["base_weekly_burn"].sum()),
        "portfolio_fte_weekly_burn": float(products["fte_weekly_burn"].sum()),
        "portfolio_contractor_weekly_burn": float(products["contractor_weekly_burn"].sum()),
        "products_at_cliff_risk": int(products["risk_tier"].isin(["Critical", "Warning"]).sum()),
        "products_critical": int((products["risk_tier"] == "Critical").sum()),
        "unfunded_backlog_points": float(products["base_unfunded_points"].sum(skipna=True)),
        "slip_unfunded_backlog_points": float(products["slip_unfunded_points"].sum(skipna=True)),
        "product_count": int(len(products)),
    }
    delta = compute_delta(products, Path(output_dir) / SNAPSHOT_FILENAME)
    return ForecastResult(cfg, products, scen, member_burn, bundle.issues,
                          check_freshness(metrics, cfg.as_of_date), kpis, delta, datetime.now(timezone.utc))


def budget_trajectories(bundle: InputBundle, result: ForecastResult, end_date: date,
                        scenarios: tuple[str, ...] = ("Base", "Slip")) -> pd.DataFrame:
    """Remaining budget per day, per product & scenario (for the runway chart). Floors at 0."""
    cfg = result.config
    horizon = max((end_date - cfg.as_of_date).days + 1, 1)
    params = scenario_parameters(cfg)
    rows = []
    for _, b in bundle.budgets.iterrows():
        pid = b["product_id"]
        members = result.member_burn[result.member_burn["product_id"] == pid]
        if members.empty or pd.isna(b["remaining_budget"]):
            continue
        for scen in scenarios:
            sched = daily_burn_schedule(members, cfg.as_of_date, horizon, params[scen]["burn_mult"],
                                        cfg.honor_contract_end_dates)
            remaining = b["remaining_budget"]
            for d in range(horizon):
                rows.append({"product_id": pid, "scenario": scen,
                             "date": cfg.as_of_date + timedelta(days=d), "remaining_budget": max(remaining, 0.0)})
                remaining -= sched[d]
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------
# 4. Exporters (member_id only — no personal names)
# --------------------------------------------------------------------------------------
def _fmt_date(d) -> str:
    return d.isoformat() if isinstance(d, date) else "n/a"


def _fmt_num(x, nd=1) -> str:
    return "n/a" if x is None or pd.isna(x) else f"{x:,.{nd}f}"


def forecast_table(result: ForecastResult) -> pd.DataFrame:
    """Machine-readable forecast: one row per product x scenario."""
    p_idx = result.products.set_index("product_id")
    df = result.scenarios.copy()
    df.insert(0, "as_of_date", result.config.as_of_date.isoformat())
    df.insert(2, "product_name", df["product_id"].map(p_idx["product_name"]))
    df["period_end"] = df["product_id"].map(p_idx["period_end"]).map(_fmt_date)
    df["remaining_budget"] = df["product_id"].map(p_idx["remaining_budget"])
    df["contractor_share_pct"] = df["product_id"].map(p_idx["contractor_share_pct"])
    df["carryover_pct"] = df["product_id"].map(p_idx["carryover_pct"])
    df["rework_pct"] = df["product_id"].map(p_idx["rework_pct"])
    df["risk_tier"] = df["product_id"].map(p_idx["risk_tier"])
    df["false_velocity_flag"] = df["product_id"].map(p_idx["false_velocity"].map(has_flag))
    df["context_switch_member_ids"] = df["product_id"].map(
        p_idx["context_switching"].map(lambda xs: ";".join(x["member_id"] for x in xs)))
    df["rolloff_member_ids"] = df["product_id"].map(
        p_idx["rolloff"].map(lambda xs: ";".join(f"{x['member_id']}@{x['contract_end_date']}" for x in xs)))
    df["turnover_risk_flag"] = df["rolloff_member_ids"] != ""
    df["honor_contract_end_dates"] = result.config.honor_contract_end_dates
    df["slip_cost_multiplier"] = result.config.slip_cost_multiplier
    df["cliff_buffer_weeks"] = result.config.cliff_buffer_weeks
    df["projected_depletion_date"] = df["projected_depletion_date"].map(_fmt_date)
    for col in ["weekly_burn", "sprint_burn", "fte_weekly_burn", "contractor_weekly_burn", "spend_to_period_end",
                "budget_gap_at_period_end", "remaining_budget"]:
        df[col] = df[col].round(2)
    for col in ["runway_days", "runway_weeks", "runway_variance_weeks", "effective_net_velocity",
                "affordable_sprints", "deliverable_points", "unfunded_backlog_points", "contractor_share_pct"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
    return df


def render_markdown_report(result: ForecastResult, bundle: InputBundle) -> str:
    cfg, k, P = result.config, result.kpis, result.products
    synced = [t for t in P["last_synced_utc"] if t is not None]
    synced_txt = (f"{min(synced):%Y-%m-%d %H:%M} UTC" if synced and min(synced) == max(synced)
                  else f"{min(synced):%Y-%m-%d %H:%M} – {max(synced):%Y-%m-%d %H:%M} UTC" if synced else "unknown")
    tier_icon = {"Critical": "🔴 Critical", "Warning": "🟠 Warning", "Healthy": "🟢 Healthy",
                 "Insufficient Data": "⚪ Insufficient Data"}
    L = [
        "# SprintRunway — Runway & Turnover-Risk Brief",
        "",
        f"**As-of:** {cfg.as_of_date.isoformat()} · **Last synced:** {synced_txt} · "
        f"**Generated:** {result.generated_at_utc:%Y-%m-%d %H:%M} UTC · **Source:** {bundle.source_label.split(':')[0]}",
        "",
    ]
    if result.freshness_warnings:
        L += ["> ⚠️ **Data freshness**", *[f"> - {w}" for w in result.freshness_warnings], ""]
    L += [
        "## Executive KPIs",
        "",
        "| Total Remaining Budget | Portfolio Weekly Burn | Products at Cliff Risk | Unfunded Backlog Points (Base / Slip) |",
        "|---|---|---|---|",
        f"| {_money(k['total_remaining_budget'])} | {_money(k['portfolio_weekly_burn'])} "
        f"(FTE {_money(k['portfolio_fte_weekly_burn'])} · Contractor {_money(k['portfolio_contractor_weekly_burn'])}) "
        f"| {k['products_at_cliff_risk']} of {k['product_count']} ({k['products_critical']} Critical) "
        f"| {k['unfunded_backlog_points']:.0f} / {k['slip_unfunded_backlog_points']:.0f} |",
        "",
        "## What Changed / Executive Delta Summary",
        "",
        *[f"- {line}" for line in result.delta_summary],
        "",
        "**Key drivers**",
        "",
        *[f"- {line}" for line in key_drivers(P)],
        "",
        "## Product Runway",
        "",
        "| Product | Risk | Weekly Burn | Base Depletion | Slip Depletion | Base Variance (wks) | Gap @ Period End (Base) "
        "| Contractor Share | Carryover / Rework | Unfunded Pts (Base) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, p in P.iterrows():
        L.append(
            f"| {p['product_id']} — {p['product_name']} | {tier_icon[p['risk_tier']]} | {_money(p['base_weekly_burn'])} "
            f"| {_fmt_date(p['base_depletion_date'])} | {_fmt_date(p['slip_depletion_date'])} "
            f"| {_fmt_num(p['base_variance_weeks'])} | {_money(p['base_gap_at_period_end'])} "
            f"| {_fmt_num(p['contractor_share_pct'], 0)}% | {_fmt_num(p['carryover_pct'], 0)}% / {_fmt_num(p['rework_pct'], 0)}% "
            f"| {_fmt_num(p['base_unfunded_points'], 0)} of {_fmt_num(p['remaining_backlog_points'], 0)} |"
        )
    L += ["", "## Product Detail & Recommended Actions", ""]
    for _, p in P.iterrows():
        L += [f"### {p['product_id']} — {p['product_name']} · {tier_icon[p['risk_tier']]}", "", f"{p['risk_reason']}", ""]
        L.append(f"- **Burn:** {_money(p['base_weekly_burn'])}/wk · {_money(p['base_sprint_burn'])}/sprint "
                 f"(FTE {_money(p['fte_weekly_burn'])} · Contractor {_money(p['contractor_weekly_burn'])}/wk)")
        L.append(f"- **Scope fit (Base):** net velocity {_fmt_num(p['base_net_velocity'])} pts/sprint → "
                 f"{_fmt_num(p['base_deliverable_points'], 0)} deliverable of {_fmt_num(p['remaining_backlog_points'], 0)} "
                 f"backlog pts (Optimistic unfunded {_fmt_num(p['optimistic_unfunded_points'], 0)}, "
                 f"Slip unfunded {_fmt_num(p['slip_unfunded_points'], 0)})")
        if has_flag(p["false_velocity"]):
            L.append(f"- ⚠️ **False Velocity:** {p['false_velocity']}")
        if p["context_switching"]:
            items = []
            for c in p["context_switching"]:
                where = ", ".join(c["other_products"]) or (f"{c['unallocated_pct']:g}% unallocated in roster")
                items.append(f"{c['member_id']} ({c['role']}, {c['allocation_pct']:g}% here; also {where})")
            L.append("- ⚠️ **Context-Switching Risk:** " + "; ".join(items))
        if p["rolloff"]:
            items = [f"{r['member_id']} ({r['role']}) ends {r['contract_end_date']:%b %d} — "
                     f"{r['share_of_burn_pct']:.0f}% of burn" for r in p["rolloff"]]
            L.append("- 🔻 **Contractor Roll-Off / Turnover Risk:** " + "; ".join(items))
        L += ["", "**Recommended actions**", "", *[f"1. {a}" for a in p["actions"]], ""]
    L += ["## Model Assumptions", "", *[f"- {a}" for a in cfg.assumptions()], ""]
    if result.issues:
        L += ["## Data Issues (surfaced, not filled)", "", *[f"- {i}" for i in result.issues], ""]
    return "\n".join(L)


def write_outputs(result: ForecastResult, bundle: InputBundle, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / REPORT_FILENAME
    table = output_dir / FORECAST_FILENAME
    report.write_text(render_markdown_report(result, bundle), encoding="utf-8")
    forecast_table(result).to_csv(table, index=False)
    write_snapshot(result, output_dir)
    return {"report": report, "forecast": table}


def write_snapshot(result: ForecastResult, output_dir: Path) -> Path:
    """Baseline for the next run's delta summary."""
    cols = ["product_id", "remaining_budget", "base_weekly_burn", "base_depletion_date", "risk_tier", "base_unfunded_points"]
    snap = result.products[cols].copy()
    snap["base_depletion_date"] = snap["base_depletion_date"].map(_fmt_date).replace("n/a", "")
    snap.insert(0, "as_of_date", result.config.as_of_date.isoformat())
    path = Path(output_dir) / SNAPSHOT_FILENAME
    snap.to_csv(path, index=False)
    return path


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SprintRunway budget runway & turnover-risk forecaster")
    ap.add_argument("--as-of-date", type=date.fromisoformat, default=date.today(),
                    help="YYYY-MM-DD (default: today)")
    ap.add_argument("--cliff-buffer-weeks", type=float, default=4.0)
    ap.add_argument("--slip-multiplier", type=float, default=1.20, help="Slip-case weekly burn multiplier")
    ap.add_argument("--honor-contract-end-dates", action="store_true",
                    help="Stop contractor burn after contract_end_date")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = ap.parse_args(argv)

    cfg = ForecastConfig(as_of_date=args.as_of_date, cliff_buffer_weeks=args.cliff_buffer_weeks,
                         slip_cost_multiplier=args.slip_multiplier,
                         honor_contract_end_dates=args.honor_contract_end_dates)
    bundle = load_all(CsvSource(args.data_dir), cfg)
    result = run_forecast(bundle, cfg, args.output_dir)
    paths = write_outputs(result, bundle, args.output_dir)

    for w in result.freshness_warnings:
        print(f"FRESHNESS: {w}")
    for i in result.issues:
        print(f"DATA ISSUE: {i}")
    for _, p in result.products.iterrows():
        print(f"{p['product_id']:<11} {p['risk_tier']:<9} base {_fmt_date(p['base_depletion_date'])}  "
              f"slip {_fmt_date(p['slip_depletion_date'])}  burn {_money(p['base_weekly_burn'])}/wk")
    print(f"Wrote {paths['report']} and {paths['forecast']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
