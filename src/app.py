"""SprintRunway AI — Streamlit dashboard.

Run:  streamlit run src/app.py

All math lives in forecast_engine; this file only handles controls, caching and display.
Full names appear here (local only); exports use member_id.

Public demo (e.g. Streamlit Community Cloud): set SPRINTRUNWAY_DEMO=1 as an environment variable
or secret. The app is then locked to the bundled sample data, and controls that read or write the
server's filesystem are hidden.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import forecast_engine as fe

st.set_page_config(page_title="SprintRunway AI", page_icon="📉", layout="wide")

DEFAULT_AS_OF = date(2026, 11, 21)

# Reference categorical palette (fixed order, validated adjacent-pair CVD-safe) — color follows product.
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
FTE_COLOR, CONTRACTOR_COLOR = SERIES_COLORS[0], SERIES_COLORS[1]
TIER_LABEL = {"Critical": "🔴 Critical", "Warning": "🟠 Warning", "Healthy": "🟢 Healthy",
              "Insufficient Data": "⚪ Insufficient Data"}
GRID = "rgba(128,128,128,0.18)"
REPO_URL = "https://github.com/jmfaust76/sprint-runway-mvp"


def _demo_mode() -> bool:
    """True when SPRINTRUNWAY_DEMO is set to 1/true/yes/on (environment variable first, then st.secrets)."""
    raw = os.environ.get("SPRINTRUNWAY_DEMO")
    if raw is None:
        try:
            raw = st.secrets.get("SPRINTRUNWAY_DEMO")
        except Exception:  # no secrets file (normal for local runs)
            raw = None
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


DEMO_MODE = _demo_mode()


# --------------------------------------------------------------------------------------
# Cached data access — keyed on file mtimes so CSV edits refresh automatically
# --------------------------------------------------------------------------------------
def _mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


@st.cache_data(show_spinner="Loading inputs & forecasting…")
def cached_forecast(data_dir: str, output_dir: str, input_mtimes: tuple, snapshot_mtime: float, cfg_items: tuple):
    cfg = fe.ForecastConfig(**dict(cfg_items))
    bundle = fe.load_all(fe.CsvSource(data_dir), cfg)
    result = fe.run_forecast(bundle, cfg, Path(output_dir))
    return bundle, result


@st.cache_data(show_spinner=False)
def cached_trajectories(data_dir: str, output_dir: str, input_mtimes: tuple, snapshot_mtime: float,
                        cfg_items: tuple, end_date: date) -> pd.DataFrame:
    bundle, result = cached_forecast(data_dir, output_dir, input_mtimes, snapshot_mtime, cfg_items)
    return fe.budget_trajectories(bundle, result, end_date)


# --------------------------------------------------------------------------------------
# Sidebar — scenario controls
# --------------------------------------------------------------------------------------
with st.sidebar:
    st.header("Scenario controls")
    as_of = st.date_input("As-of date", value=DEFAULT_AS_OF)
    buffer_weeks = st.slider("Cliff buffer (weeks)", 0, 12, 4)
    slip_mult = st.slider("Slip / rework burn multiplier", 1.00, 2.00, 1.20, 0.05, format="%.2fx")
    with st.expander("Advanced assumptions"):
        slip_drag = st.slider("Slip: extra carryover/rework drag", 0, 100, 20, 5, format="+%d%%") / 100
        opt_uplift = st.slider("Optimistic: velocity uplift", 0, 50, 10, 1, format="+%d%%") / 100
        opt_drag = st.slider("Optimistic: drag reduction", 0, 100, 50, 5, format="-%d%%") / 100
    honor = st.toggle("Honor Contract End Dates", value=False,
                      help="Drop contractor burn after contract_end_date. Velocity is not reduced, "
                           "so scope fit becomes an upper bound.")
    st.divider()
    if DEMO_MODE:
        data_dir = fe.DEFAULT_DATA_DIR
        st.caption("Demo: fixed to the bundled sample data.")
    else:
        data_dir = Path(st.text_input("Data directory", str(fe.DEFAULT_DATA_DIR)))
    output_dir = fe.DEFAULT_OUTPUT_DIR
    if st.button("Reload data", icon="🔄"):
        st.cache_data.clear()

cfg = fe.ForecastConfig(as_of_date=as_of, cliff_buffer_weeks=float(buffer_weeks), slip_cost_multiplier=slip_mult,
                        slip_drag_increase=slip_drag, optimistic_velocity_uplift=opt_uplift,
                        optimistic_drag_reduction=opt_drag, honor_contract_end_dates=honor)
cfg_items = tuple(sorted(fe.asdict(cfg).items()))

try:
    src = fe.CsvSource(data_dir)
    mtimes = src.file_mtimes()
except FileNotFoundError as exc:
    st.error(f"Input file not found: {exc.filename}")
    st.stop()
snap_mtime = _mtime(output_dir / fe.SNAPSHOT_FILENAME)
bundle, result = cached_forecast(str(data_dir), str(output_dir), mtimes, snap_mtime, cfg_items)
P, K = result.products, result.kpis

# --------------------------------------------------------------------------------------
# Header — freshness & assumptions
# --------------------------------------------------------------------------------------
st.title("SprintRunway AI")
st.caption("Development team budget, runway & turnover-risk forecast")
if DEMO_MODE:
    st.info(f"**Demo with fictional sample data.** To use your own data, run SprintRunway locally; "
            f"see the [README on GitHub]({REPO_URL}).", icon="🧪")

synced = [t for t in P["last_synced_utc"] if t is not None]
synced_txt = f"{max(synced):%Y-%m-%d %H:%M} UTC" if synced else "unknown"
c1, c2, c3 = st.columns(3)
c1.markdown(f"**Last synced:** {synced_txt}")
c2.markdown(f"**As-of date:** {as_of.isoformat()}")
c3.markdown(f"**Inputs loaded:** {bundle.loaded_at_utc:%Y-%m-%d %H:%M:%S} UTC")

if result.freshness_warnings:
    st.warning("**Data freshness:** as-of date differs from last sync — remaining_budget is not adjusted.\n\n"
               + "\n".join(f"- {w}" for w in result.freshness_warnings))
if result.issues:
    with st.expander(f"⚠️ {len(result.issues)} data issue(s) surfaced (not filled)", expanded=True):
        for i in result.issues:
            st.markdown(f"- {i}")
with st.expander("Active model assumptions"):
    for a in cfg.assumptions():
        st.markdown(f"- {a}")

# --------------------------------------------------------------------------------------
# KPI cards
# --------------------------------------------------------------------------------------
k1, k2, k3, k4 = st.columns(4)
k1.metric("Total Remaining Budget", fe._money(K["total_remaining_budget"]), border=True)
k2.metric("Portfolio Weekly Burn", fe._money(K["portfolio_weekly_burn"]), border=True,
          help=f"FTE {fe._money(K['portfolio_fte_weekly_burn'])} · "
               f"Contractor {fe._money(K['portfolio_contractor_weekly_burn'])}")
k2.caption(f"FTE {fe._money(K['portfolio_fte_weekly_burn'])} · Contractor {fe._money(K['portfolio_contractor_weekly_burn'])}")
k3.metric("Products at Cliff Risk", f"{K['products_at_cliff_risk']} of {K['product_count']}", border=True)
k3.caption(f"{K['products_critical']} Critical")
k4.metric("Unfunded Backlog Points", f"{K['unfunded_backlog_points']:,.0f}", border=True)
k4.caption(f"Slip case: {K['slip_unfunded_backlog_points']:,.0f} pts")

# --------------------------------------------------------------------------------------
# Executive delta summary
# --------------------------------------------------------------------------------------
st.subheader("What Changed / Executive Delta Summary")
if DEMO_MODE:
    st.markdown("- Not available in the demo: this section compares each export with the previous one, "
                "and the demo doesn't save exports.")
else:
    for line in result.delta_summary:
        st.markdown(f"- {line}")
st.markdown("**Key drivers**")
for line in fe.key_drivers(P):
    st.markdown(f"- {line}")

# --------------------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------------------
product_order = bundle.budgets["product_id"].tolist()          # stable: color follows the entity
color_of = {pid: SERIES_COLORS[i % len(SERIES_COLORS)] for i, pid in enumerate(product_order)}
period_ends = [d for d in P["period_end"] if isinstance(d, date)]
latest_pe = max(period_ends) if period_ends else as_of + timedelta(days=60)
chart_end = latest_pe + timedelta(weeks=buffer_weeks + 4)

left, right = st.columns([3, 2])
with left:
    st.subheader("Runway: remaining budget, Base vs Slip")
    traj = cached_trajectories(str(data_dir), str(output_dir), mtimes, snap_mtime, cfg_items, chart_end)
    fig = go.Figure()
    if period_ends:
        fig.add_vrect(x0=latest_pe, x1=latest_pe + timedelta(weeks=buffer_weeks),
                      fillcolor="rgba(250,178,25,0.12)", line_width=0, layer="below")
        fig.add_vline(x=latest_pe, line_width=1, line_dash="dot", line_color="rgba(128,128,128,0.8)")
        fig.add_annotation(x=latest_pe, y=1, yref="paper", text="Period end", showarrow=False,
                           xanchor="right", yanchor="bottom", font=dict(size=11))
        if buffer_weeks:
            fig.add_annotation(x=latest_pe + timedelta(weeks=buffer_weeks), y=1, yref="paper",
                               text=f"+{buffer_weeks}w buffer", showarrow=False, xanchor="right",
                               yanchor="bottom", font=dict(size=11))
    for pid in product_order:
        for scen, dash in (("Base", "solid"), ("Slip", "dash")):
            d = traj[(traj["product_id"] == pid) & (traj["scenario"] == scen)] if not traj.empty else traj
            if d.empty:
                continue
            fig.add_trace(go.Scatter(
                x=d["date"], y=d["remaining_budget"], mode="lines", name=f"{pid} · {scen}",
                legendgroup=pid, line=dict(color=color_of[pid], width=2, dash=dash),
                hovertemplate=f"{pid} · {scen}<br>%{{x|%b %d, %Y}}<br>Remaining $%{{y:,.0f}}<extra></extra>",
            ))
    fig.update_layout(height=450, margin=dict(l=10, r=10, t=30, b=10), hovermode="closest",
                      legend=dict(orientation="h", yanchor="top", y=-0.12, x=0),
                      yaxis=dict(title="Remaining budget", tickprefix="$", tickformat=",.0f", gridcolor=GRID,
                                 rangemode="tozero"),
                      xaxis=dict(gridcolor=GRID, range=[as_of, chart_end]))
    st.plotly_chart(fig, height=450, key="runway_chart")
    st.caption("Solid = Base, dashed = Slip. A line reaching $0 is the projected depletion date. "
               "Shaded band = cliff buffer after period end.")

with right:
    st.subheader("Weekly burn: FTE vs Contractor")
    burn = P.set_index("product_id").reindex(product_order)
    fig2 = go.Figure()
    for col, label, color in (("fte_weekly_burn", "FTE", FTE_COLOR),
                              ("contractor_weekly_burn", "Contractor", CONTRACTOR_COLOR)):
        fig2.add_trace(go.Bar(
            y=burn.index, x=burn[col], name=label, orientation="h",
            marker=dict(color=color, line=dict(width=2, color="rgba(0,0,0,0)")),
            hovertemplate=f"%{{y}} · {label}<br>$%{{x:,.0f}}/wk<extra></extra>",
        ))
    totals = burn["fte_weekly_burn"] + burn["contractor_weekly_burn"]
    share = burn["contractor_share_pct"].fillna(0)
    fig2.add_trace(go.Scatter(y=burn.index, x=totals, mode="text", showlegend=False, hoverinfo="skip",
                              text=[f"  ${t:,.0f} · {s:.0f}% contr." for t, s in zip(totals, share)],
                              textposition="middle right", textfont=dict(size=11)))
    fig2.update_layout(barmode="stack", bargap=0.35, height=380, margin=dict(l=10, r=10, t=30, b=10),
                       legend=dict(orientation="h", yanchor="top", y=-0.15, x=0),
                       xaxis=dict(tickprefix="$", tickformat=",.0f", gridcolor=GRID,
                                  range=[0, float(totals.max() or 1) * 1.45]),
                       yaxis=dict(autorange="reversed"))
    st.plotly_chart(fig2, height=380, key="burn_chart")

# --------------------------------------------------------------------------------------
# Per-product risk table
# --------------------------------------------------------------------------------------
st.subheader("Product runway & risk")


def _flags(p) -> str:
    out = []
    if fe.has_flag(p["false_velocity"]):
        out.append("False Velocity")
    if p["context_switching"]:
        out.append(f"Context-Switch ({len(p['context_switching'])})")
    if p["rolloff"]:
        out.append(f"Roll-Off ({len(p['rolloff'])})")
    return " · ".join(out) or "—"


table = pd.DataFrame({
    "Product": P["product_id"] + " — " + P["product_name"],
    "Risk": P["risk_tier"].map(TIER_LABEL),
    "Weekly burn": P["base_weekly_burn"],
    "Base depletion": P["base_depletion_date"],
    "Slip depletion": P["slip_depletion_date"],
    "Base variance (wks)": pd.to_numeric(P["base_variance_weeks"], errors="coerce"),
    "Gap @ period end": P["base_gap_at_period_end"],
    "Contractor share": P["contractor_share_pct"],
    "Carryover %": P["carryover_pct"],
    "Rework %": P["rework_pct"],
    "Net velocity": P["base_net_velocity"],
    "Unfunded pts": P["base_unfunded_points"],
    "Backlog pts": P["remaining_backlog_points"],
    "Flags": P.apply(_flags, axis=1),
})
st.dataframe(
    table, hide_index=True, height=(len(table) + 1) * 35 + 3,
    column_config={
        "Weekly burn": st.column_config.NumberColumn(format="$%,.0f"),
        "Gap @ period end": st.column_config.NumberColumn(format="$%,.0f", help="+ surplus / − shortfall (Base)"),
        "Base depletion": st.column_config.DateColumn(format="YYYY-MM-DD"),
        "Slip depletion": st.column_config.DateColumn(format="YYYY-MM-DD"),
        "Base variance (wks)": st.column_config.NumberColumn(format="%+.1f", help="Depletion vs period_end"),
        "Contractor share": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=100),
        "Carryover %": st.column_config.NumberColumn(format="%.0f%%"),
        "Rework %": st.column_config.NumberColumn(format="%.0f%%"),
        "Net velocity": st.column_config.NumberColumn(format="%.1f", help="pts/sprint after drag (Base)"),
        "Unfunded pts": st.column_config.NumberColumn(format="%.0f"),
        "Backlog pts": st.column_config.NumberColumn(format="%.0f"),
    },
)

st.subheader("Product detail & recommended actions")
names = result.member_burn.drop_duplicates("member_id").set_index("member_id")["name"]
for _, p in P.iterrows():
    with st.expander(f"{TIER_LABEL[p['risk_tier']]} · {p['product_id']} — {p['product_name']}",
                     expanded=p["risk_tier"] in ("Critical", "Warning")):
        st.markdown(f"_{p['risk_reason']}_")
        a, b, c = st.columns(3)
        a.metric("Base depletion", fe._fmt_date(p["base_depletion_date"]))
        b.metric("Slip depletion", fe._fmt_date(p["slip_depletion_date"]))
        c.metric("Scope fit (Base)", f"{fe._fmt_num(p['base_deliverable_points'], 0)} / "
                                     f"{fe._fmt_num(p['remaining_backlog_points'], 0)} pts")
        if fe.has_flag(p["false_velocity"]):
            st.markdown(f"⚠️ **False Velocity:** {p['false_velocity']}")
        if p["context_switching"]:
            items = [f"{names.get(x['member_id'], x['member_id'])} ({x['role']}, {x['allocation_pct']:g}% here; "
                     f"also {', '.join(x['other_products']) or str(int(x['unallocated_pct'])) + '% unallocated'})"
                     for x in p["context_switching"]]
            st.markdown("⚠️ **Context-Switching Risk:** " + "; ".join(items))
        if p["rolloff"]:
            items = [f"{names.get(r['member_id'], r['member_id'])} ({r['role']}) ends "
                     f"{r['contract_end_date']:%b %d}, {r['share_of_burn_pct']:.0f}% of burn" for r in p["rolloff"]]
            st.markdown("🔻 **Contractor Roll-Off / Turnover Risk:** " + "; ".join(items))
        st.markdown("**Recommended actions**")
        st.markdown("\n".join(f"1. {act}" for act in p["actions"]))
        team = result.member_burn[result.member_burn["product_id"] == p["product_id"]][
            ["member_id", "name", "role", "cost_type", "allocation_pct", "blended_hourly_rate", "weekly_cost",
             "sprint_cost", "contract_end_date"]]
        st.dataframe(team, hide_index=True, height=(len(team) + 1) * 35 + 3, column_config={
            "allocation_pct": st.column_config.NumberColumn("Alloc %", format="%.0f%%"),
            "blended_hourly_rate": st.column_config.NumberColumn("Rate", format="$%.0f/h"),
            "weekly_cost": st.column_config.NumberColumn("Weekly cost", format="$%,.0f"),
            "sprint_cost": st.column_config.NumberColumn("Sprint cost", format="$%,.0f"),
            "contract_end_date": st.column_config.DateColumn("Contract end"),
        })

# --------------------------------------------------------------------------------------
# Exports (member_id only)
# --------------------------------------------------------------------------------------
st.subheader("Exports")
if DEMO_MODE:
    st.caption("Exports use member_id only — no personal names. In the demo, reports download to your "
               "computer; nothing is saved on the server.")
    e2, e3 = st.columns(2)
else:
    st.caption("Exports use member_id only — no personal names. Writing to /output also saves the baseline "
               "for the next run's delta summary.")
    e1, e2, e3 = st.columns(3)
    if e1.button("Write reports to /output", type="primary", icon="💾"):
        paths = fe.write_outputs(result, bundle, output_dir)
        st.success(f"Wrote {paths['report'].name} and {paths['forecast'].name} to {output_dir}")
e2.download_button("Download Markdown brief", fe.render_markdown_report(result, bundle),
                   file_name=fe.REPORT_FILENAME, mime="text/markdown", icon="📄")
e3.download_button("Download forecast CSV", fe.forecast_table(result).to_csv(index=False),
                   file_name=fe.FORECAST_FILENAME, mime="text/csv", icon="📊")
