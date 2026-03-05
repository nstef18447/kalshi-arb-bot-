"""Kalshi Arb Bot — Phase 1 Validation Dashboard.

Run: streamlit run dashboard.py
"""

import os
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import db
import queries

# ── App config ────────────────────────────────────────────────────

st.set_page_config(
    page_title="Kalshi Arb Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Inline CSS — per preferences, no config.toml
st.markdown("""
<style>
    .metric-card {
        background: #1e1e2e;
        border-radius: 8px;
        padding: 16px;
        text-align: center;
        border: 1px solid #333;
    }
    .metric-card h3 { color: #cdd6f4; margin: 0; font-size: 0.85rem; }
    .metric-card p { color: #f5e0dc; margin: 4px 0 0 0; font-size: 1.6rem; font-weight: bold; }
    .stDataFrame { font-size: 0.85rem; }
    div[data-testid="stMetric"] { background: #1e1e2e; padding: 12px; border-radius: 8px; border: 1px solid #333; }
</style>
""", unsafe_allow_html=True)

# Ensure DB exists
db.init_db()

# ── Sidebar: navigation + config ─────────────────────────────────

st.sidebar.title("Kalshi Arb Bot")
page = st.sidebar.radio(
    "Page",
    ["Overview", "Ladder Explorer", "Cross-Strike Matrix", "Trade Log", "Competition Signals", "Paper Trades"],
)

st.sidebar.markdown("---")
st.sidebar.caption(f"DB: `{db.DB_PATH}`")
try:
    db_size = os.path.getsize(db.DB_PATH)
    st.sidebar.caption(f"DB size: {db_size / 1024:.0f} KB")
except OSError:
    st.sidebar.caption("DB size: N/A")

db_info = queries.get_db_info()
st.sidebar.caption(
    f"Rows — scans: {db_info['scans']:,} | snapshots: {db_info['ladder_snapshots']:,} | "
    f"opps: {db_info['opportunities']:,} | trades: {db_info['trades']:,}"
)


def _ts_to_dt(ts):
    """Unix timestamp to datetime."""
    return datetime.utcfromtimestamp(ts)


def _show_refresh():
    """Show last-updated time and refresh button."""
    col1, col2 = st.columns([8, 1])
    with col1:
        st.caption(f"Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    with col2:
        if st.button("🔄", key=f"refresh_{page}"):
            st.rerun()


def _empty_state(message):
    """Friendly empty state."""
    st.info(f"No data yet. {message}")


# ══════════════════════════════════════════════════════════════════
# PAGE 1: OVERVIEW
# ══════════════════════════════════════════════════════════════════

def page_overview():
    st.title("Overview")
    _show_refresh()

    # Auto-refresh every 60s
    if "overview_last_refresh" not in st.session_state:
        st.session_state.overview_last_refresh = time.time()
    if time.time() - st.session_state.overview_last_refresh > 60:
        st.session_state.overview_last_refresh = time.time()
        st.rerun()

    # ── Filters ──
    filter_col1, filter_col2, filter_col3 = st.columns([2, 2, 1])

    with filter_col1:
        date_range = st.date_input(
            "Date range",
            value=(datetime.utcnow().date() - timedelta(days=7), datetime.utcnow().date()),
            key="overview_dates",
        )
    with filter_col2:
        type_options = ["A_monotonicity", "B_probability_gap", "C_hard", "C_soft"]
        selected_types = st.multiselect("Opportunity types", type_options, default=type_options)
    with filter_col3:
        min_spread = st.slider("Min spread (cents)", 0, 20, 0)

    # Convert date range to timestamps
    if isinstance(date_range, tuple) and len(date_range) == 2:
        ts_start = datetime.combine(date_range[0], datetime.min.time()).timestamp()
        ts_end = datetime.combine(date_range[1], datetime.max.time()).timestamp()
    else:
        ts_start, ts_end = None, None

    # ── Metric cards ──
    counts_df = queries.get_opp_counts()
    avg_spread_df = queries.get_avg_hard_arb_spread()

    if counts_df.empty or counts_df.iloc[0]["total"] == 0:
        _empty_state(
            "The bot needs to run and detect opportunities. "
            "Wire `db_logger.log_opportunity()` into scanner.py to start collecting data."
        )
        return

    c = counts_df.iloc[0]
    avg_spread = avg_spread_df.iloc[0]["avg_spread"] if not avg_spread_df.empty else 0
    avg_spread = avg_spread or 0

    # Persistence median
    persist_df = queries.get_opp_persistence()
    median_persist = persist_df["persistence_seconds"].median() if not persist_df.empty else 0

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Total Opps", f"{int(c['total']):,}")
    m2.metric("Last 24h", f"{int(c['last_24h']):,}")
    m3.metric("Type A", f"{int(c['type_a']):,}", delta=f"{int(c['type_a_24h'])} (24h)")
    m4.metric("Type B", f"{int(c['type_b']):,}", delta=f"{int(c['type_b_24h'])} (24h)")
    m5.metric("C Hard / Soft",
              f"{int(c['c_hard']):,} / {int(c['c_soft']):,}",
              delta=f"{int(c['c_hard_24h'])} / {int(c['c_soft_24h'])} (24h)")
    m6.metric("Avg Hard Spread", f"{avg_spread:.1f}¢")

    st.metric("Median Persistence", f"{median_persist:.0f}s")

    st.markdown("---")

    # ── Chart 1: Opportunities per hour (stacked bar) ──
    st.subheader("Opportunities Detected per Hour")
    hourly_df = queries.get_opps_per_hour(ts_start, ts_end, selected_types, min_spread)

    if hourly_df.empty:
        _empty_state("No opportunities match the current filters.")
    else:
        hourly_df["time"] = pd.to_datetime(hourly_df["hour_ts"], unit="s")
        fig_hourly = px.bar(
            hourly_df, x="time", y="count", color="label",
            labels={"count": "Opportunities", "time": "Time (UTC)", "label": "Type"},
            color_discrete_map={
                "A_monotonicity": "#f38ba8",
                "B_probability_gap": "#fab387",
                "C_hard": "#a6e3a1",
                "C_soft": "#89b4fa",
            },
        )
        fig_hourly.update_layout(
            barmode="stack", height=400, margin=dict(t=20, b=40),
            xaxis_title="", yaxis_title="Count",
        )
        st.plotly_chart(fig_hourly, use_container_width=True)

    # ── Chart 2: Spread distribution (histogram) ──
    # ── Chart 3: Spread vs time-to-expiry (scatter) ──
    col_hist, col_scatter = st.columns(2)

    with col_hist:
        st.subheader("Hard Arb Spread Distribution")
        spread_df = queries.get_hard_arb_spread_distribution(ts_start, ts_end)
        if spread_df.empty:
            _empty_state("No hard arb data yet.")
        else:
            fig_hist = px.histogram(
                spread_df, x="spread_cents",
                nbins=max(1, int(spread_df["spread_cents"].max()) - int(spread_df["spread_cents"].min()) + 1),
                labels={"spread_cents": "Spread (cents)", "count": "Count"},
                color_discrete_sequence=["#a6e3a1"],
            )
            fig_hist.update_layout(
                height=350, margin=dict(t=20, b=40),
                xaxis_title="Spread (cents)", yaxis_title="Count",
                bargap=0.1,
            )
            st.plotly_chart(fig_hist, use_container_width=True)

    with col_scatter:
        st.subheader("Spread vs Time to Expiry")
        scatter_df = queries.get_spread_vs_expiry(ts_start, ts_end, selected_types)
        if scatter_df.empty:
            _empty_state("No data with time_to_expiry recorded yet.")
        else:
            fig_scatter = px.scatter(
                scatter_df, x="time_to_expiry_seconds", y="spread_cents", color="label",
                labels={
                    "time_to_expiry_seconds": "Time to Expiry (s)",
                    "spread_cents": "Spread (cents)",
                    "label": "Type",
                },
                color_discrete_map={
                    "A_monotonicity": "#f38ba8",
                    "B_probability_gap": "#fab387",
                    "C_hard": "#a6e3a1",
                    "C_soft": "#89b4fa",
                },
                opacity=0.6,
            )
            fig_scatter.update_layout(height=350, margin=dict(t=20, b=40))
            st.plotly_chart(fig_scatter, use_container_width=True)


# ══════════════════════════════════════════════════════════════════
# PAGE 2: LADDER EXPLORER
# ══════════════════════════════════════════════════════════════════

def page_ladder_explorer():
    st.title("Ladder Explorer")
    _show_refresh()

    windows_df = queries.get_expiry_windows()
    if windows_df.empty:
        _empty_state(
            "No ladder snapshots yet. Wire `db_logger.log_snapshot()` into the bot's scan cycle."
        )
        return

    selected_window = st.selectbox("Expiry Window", windows_df["expiry_window"].tolist())

    timestamps_df = queries.get_snapshot_timestamps(selected_window)
    if timestamps_df.empty:
        _empty_state("No snapshots for this window.")
        return

    ts_list = timestamps_df["timestamp"].tolist()
    ts_labels = [datetime.utcfromtimestamp(t).strftime("%H:%M:%S") for t in ts_list]

    selected_idx = st.selectbox(
        "Snapshot time (UTC)",
        range(len(ts_list)),
        format_func=lambda i: ts_labels[i],
    )
    selected_ts = ts_list[selected_idx]

    # ── Ladder table with conditional formatting ──
    st.subheader("Strike Ladder")
    ladder_df = queries.get_ladder_at_timestamp(selected_window, selected_ts)

    if ladder_df.empty:
        _empty_state("No strikes in this snapshot.")
        return

    def highlight_ladder(row):
        styles = [""] * len(row)
        combined_idx = row.index.get_loc("combined")
        if row["combined"] < 100:
            styles[combined_idx] = "background-color: #f38ba8; color: black; font-weight: bold"
        # Monotonicity check: compare to previous row (handled after)
        return styles

    # Check monotonicity
    ladder_display = ladder_df.copy()
    ladder_display["mono_violation"] = False
    for i in range(1, len(ladder_display)):
        if ladder_display.iloc[i]["yes_ask"] > ladder_display.iloc[i - 1]["yes_ask"]:
            ladder_display.at[ladder_display.index[i], "mono_violation"] = True

    def style_ladder(row):
        styles = [""] * len(row)
        combined_idx = list(row.index).index("combined")
        if row["combined"] < 100:
            styles[combined_idx] = "background-color: #a6e3a1; color: black; font-weight: bold"
        if row.get("mono_violation", False):
            yes_ask_idx = list(row.index).index("yes_ask")
            styles[yes_ask_idx] = "background-color: #fab387; color: black; font-weight: bold"
        return styles

    display_cols = ["strike", "yes_ask", "yes_bid", "no_ask", "no_bid", "yes_depth", "no_depth", "combined"]
    styled = ladder_display[display_cols + ["mono_violation"]].style.apply(style_ladder, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True)

    st.caption("🟢 Green combined = hard arb (< 100¢) | 🟠 Orange yes_ask = monotonicity violation")

    # ── Line chart: Yes prices across strikes ──
    col_line, col_heat = st.columns(2)

    with col_line:
        st.subheader("Yes Prices Across Strikes")
        fig_line = go.Figure()
        fig_line.add_trace(go.Scatter(
            x=ladder_df["strike"], y=ladder_df["yes_ask"],
            mode="lines+markers", name="yes_ask",
            line=dict(color="#89b4fa", width=2),
        ))

        # Highlight monotonicity violations
        violations = ladder_display[ladder_display["mono_violation"]]
        if not violations.empty:
            fig_line.add_trace(go.Scatter(
                x=violations["strike"], y=violations["yes_ask"],
                mode="markers", name="Monotonicity violation",
                marker=dict(color="#f38ba8", size=12, symbol="x"),
            ))

        fig_line.update_layout(
            height=350, margin=dict(t=20, b=40),
            xaxis_title="Strike", yaxis_title="Yes Ask (cents)",
        )
        st.plotly_chart(fig_line, use_container_width=True)

    # ── Heatmap: timestamp × strike → yes_ask ──
    with col_heat:
        st.subheader("Ladder Heatmap (last 60 snapshots)")
        heatmap_df = queries.get_ladder_heatmap_data(selected_window, 60)

        if heatmap_df.empty:
            _empty_state("Need multiple snapshots for heatmap.")
        else:
            pivot = heatmap_df.pivot_table(
                index="timestamp", columns="strike", values="yes_ask", aggfunc="first"
            )
            y_labels = [datetime.utcfromtimestamp(t).strftime("%H:%M:%S") for t in pivot.index]

            fig_heat = go.Figure(data=go.Heatmap(
                z=pivot.values,
                x=[str(c) for c in pivot.columns],
                y=y_labels,
                colorscale="Viridis",
                colorbar=dict(title="Yes Ask"),
            ))
            fig_heat.update_layout(
                height=350, margin=dict(t=20, b=40),
                xaxis_title="Strike", yaxis_title="Time (UTC)",
            )

            # Overlay opportunity markers
            opps_df = queries.get_opps_for_window(selected_window)
            if not opps_df.empty:
                # Map opp timestamps to nearest heatmap row
                for _, opp in opps_df.iterrows():
                    opp_time_label = datetime.utcfromtimestamp(opp["timestamp"]).strftime("%H:%M:%S")
                    fig_heat.add_trace(go.Scatter(
                        x=[str(opp["strike_low"]), str(opp["strike_high"])],
                        y=[opp_time_label, opp_time_label],
                        mode="markers",
                        marker=dict(color="#f38ba8", size=8, symbol="diamond"),
                        name=f"{opp['opp_type']}_{opp['sub_type']}",
                        showlegend=False,
                        hovertext=f"{opp['opp_type']}_{opp['sub_type']}: "
                                  f"{opp['strike_low']}-{opp['strike_high']} "
                                  f"cost={opp['combined_cost']}",
                    ))

            st.plotly_chart(fig_heat, use_container_width=True)


# ══════════════════════════════════════════════════════════════════
# PAGE 3: CROSS-STRIKE MATRIX
# ══════════════════════════════════════════════════════════════════

def page_cross_strike_matrix():
    st.title("Cross-Strike Matrix")
    _show_refresh()

    windows_df = queries.get_expiry_windows()
    if windows_df.empty:
        _empty_state("No ladder snapshots yet.")
        return

    selected_window = st.selectbox("Expiry Window", windows_df["expiry_window"].tolist(),
                                   key="matrix_window")

    timestamps_df = queries.get_snapshot_timestamps(selected_window)
    if timestamps_df.empty:
        _empty_state("No snapshots for this window.")
        return

    ts_list = timestamps_df["timestamp"].tolist()
    ts_labels = [datetime.utcfromtimestamp(t).strftime("%H:%M:%S") for t in ts_list]

    # Slider to scrub through timestamps
    if len(ts_list) > 1:
        slider_idx = st.slider(
            "Scrub through time",
            0, len(ts_list) - 1, 0,
            format_func=lambda i: ts_labels[i],
        )
    else:
        slider_idx = 0

    selected_ts = ts_list[slider_idx]
    st.caption(f"Showing: {ts_labels[slider_idx]} UTC")

    # Build NxN matrix
    matrix_df = queries.get_matrix_data(selected_window, selected_ts)
    if matrix_df.empty or len(matrix_df) < 2:
        _empty_state("Need at least 2 strikes to build a matrix.")
        return

    strikes = matrix_df["strike"].tolist()
    yes_asks = matrix_df["yes_ask"].tolist()
    no_asks = matrix_df["no_ask"].tolist()

    n = len(strikes)
    matrix = np.full((n, n), np.nan)

    for i in range(n):
        for j in range(i + 1, n):
            # Cost = yes_ask(low) + no_ask(high)
            cost = yes_asks[i] + no_asks[j]
            matrix[i][j] = cost

    strike_labels = [f"{s:.0f}" for s in strikes]

    # Custom colorscale: green < 100, yellow 100-105, grey > 105
    fig_matrix = go.Figure(data=go.Heatmap(
        z=matrix,
        x=strike_labels,
        y=strike_labels,
        colorscale=[
            [0.0, "#a6e3a1"],    # green (hard arb)
            [0.5, "#f9e2af"],    # yellow (soft arb zone)
            [1.0, "#585b70"],    # grey (no arb)
        ],
        zmin=85,
        zmax=115,
        colorbar=dict(title="Cost (¢)"),
        hovertemplate="Low: %{y}<br>High: %{x}<br>Cost: %{z}¢<extra></extra>",
    ))

    # Add text annotations for cells with arbs
    annotations = []
    for i in range(n):
        for j in range(i + 1, n):
            val = matrix[i][j]
            if not np.isnan(val):
                color = "black" if val < 105 else "white"
                annotations.append(dict(
                    x=strike_labels[j], y=strike_labels[i],
                    text=f"{val:.0f}",
                    showarrow=False, font=dict(size=10, color=color),
                ))

    fig_matrix.update_layout(
        height=max(400, n * 40),
        margin=dict(t=20, b=40),
        xaxis_title="High Strike",
        yaxis_title="Low Strike",
        annotations=annotations,
    )
    st.plotly_chart(fig_matrix, use_container_width=True)

    # Summary below matrix
    hard_arbs = []
    for i in range(n):
        for j in range(i + 1, n):
            val = matrix[i][j]
            if not np.isnan(val) and val < 100:
                hard_arbs.append({
                    "Low Strike": strikes[i],
                    "High Strike": strikes[j],
                    "Cost": val,
                    "Spread": 100 - val,
                })
    if hard_arbs:
        st.subheader(f"Hard Arbs Found: {len(hard_arbs)}")
        st.dataframe(pd.DataFrame(hard_arbs), use_container_width=True, hide_index=True)
    else:
        st.caption("No hard arbs at this timestamp.")


# ══════════════════════════════════════════════════════════════════
# PAGE 4: TRADE LOG
# ══════════════════════════════════════════════════════════════════

def page_trade_log():
    st.title("Trade Log")
    _show_refresh()

    trades_df = queries.get_all_trades()
    if trades_df.empty:
        _empty_state(
            "No trades yet. Wire `db_logger.log_trade()` into bot.py's execution path. "
            "This page becomes relevant once the bot starts executing."
        )
        return

    # ── Summary stats ──
    summary_df = queries.get_trade_summary()
    s = summary_df.iloc[0]
    total = int(s["total_trades"])
    wins = int(s["wins"])
    orphans = int(s["orphans"])
    win_rate = (wins / total * 100) if total > 0 else 0
    orphan_rate = (orphans / total * 100) if total > 0 else 0

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total Trades", f"{total:,}")
    m2.metric("Win Rate", f"{win_rate:.1f}%")
    m3.metric("Orphan Rate", f"{orphan_rate:.1f}%")
    m4.metric("Total P&L", f"{s['total_pnl']:.0f}¢")
    m5.metric("Total Fees", f"{s['total_fees']:.0f}¢")

    st.markdown("---")

    # ── Equity curve ──
    col_equity, col_orphan = st.columns(2)

    with col_equity:
        st.subheader("Cumulative P&L")
        cum_df = queries.get_cumulative_pnl()
        if not cum_df.empty:
            cum_df["time"] = pd.to_datetime(cum_df["timestamp"], unit="s")
            fig_equity = px.line(
                cum_df, x="time", y="cumulative_pnl",
                labels={"cumulative_pnl": "Cumulative P&L (cents)", "time": ""},
                color_discrete_sequence=["#a6e3a1"],
            )
            fig_equity.update_layout(height=300, margin=dict(t=20, b=40))
            st.plotly_chart(fig_equity, use_container_width=True)

    with col_orphan:
        st.subheader("Rolling Orphan Rate (20-trade window)")
        orphan_df = queries.get_rolling_orphan_rate(20)
        if not orphan_df.empty:
            orphan_df["time"] = pd.to_datetime(orphan_df["timestamp"], unit="s")
            fig_orphan = px.line(
                orphan_df, x="time", y="rolling_orphan_rate",
                labels={"rolling_orphan_rate": "Orphan Rate", "time": ""},
                color_discrete_sequence=["#f38ba8"],
            )
            fig_orphan.add_hline(y=0.25, line_dash="dash", line_color="yellow",
                                 annotation_text="Circuit breaker (25%)")
            fig_orphan.update_layout(height=300, margin=dict(t=20, b=40),
                                     yaxis_tickformat=".0%")
            st.plotly_chart(fig_orphan, use_container_width=True)

    # ── Trade table ──
    st.subheader("All Trades")

    # Filters
    filter_col1, filter_col2 = st.columns(2)
    with filter_col1:
        type_filter = st.multiselect("Filter by type", trades_df["opp_type"].unique().tolist(),
                                     default=trades_df["opp_type"].unique().tolist(),
                                     key="trade_type_filter")
    with filter_col2:
        orphan_filter = st.selectbox("Orphan status", ["All", "Orphaned only", "Non-orphaned only"],
                                     key="trade_orphan_filter")

    filtered = trades_df[trades_df["opp_type"].isin(type_filter)]
    if orphan_filter == "Orphaned only":
        filtered = filtered[filtered["orphaned"] == 1]
    elif orphan_filter == "Non-orphaned only":
        filtered = filtered[filtered["orphaned"] == 0]

    # Format timestamp for display
    display = filtered.copy()
    display["time"] = pd.to_datetime(display["timestamp"], unit="s").dt.strftime("%m/%d %H:%M:%S")
    display_cols = [
        "time", "opp_type", "strike_low", "strike_high",
        "leg1_side", "leg1_price", "leg1_fill_status",
        "leg2_side", "leg2_price", "leg2_fill_status",
        "orphaned", "exit_price", "realized_pnl", "fees",
    ]
    st.dataframe(display[display_cols], use_container_width=True, hide_index=True, height=400)


# ══════════════════════════════════════════════════════════════════
# PAGE 5: COMPETITION SIGNALS
# ══════════════════════════════════════════════════════════════════

def page_competition_signals():
    st.title("Competition Signals")
    _show_refresh()

    # ── Chart 1: Median persistence over time ──
    st.subheader("Opportunity Persistence (rolling hourly median)")
    persist_df = queries.get_persistence_over_time()

    if persist_df.empty:
        _empty_state(
            "Need repeated detections of the same opportunity to measure persistence. "
            "This requires at least a few hours of bot runtime."
        )
    else:
        # Compute rolling median per hour bucket
        persist_df["time"] = pd.to_datetime(persist_df["hour_ts"], unit="s")
        hourly_median = persist_df.groupby("time")["persistence_seconds"].median().reset_index()

        fig_persist = px.line(
            hourly_median, x="time", y="persistence_seconds",
            labels={"persistence_seconds": "Median Persistence (s)", "time": ""},
            color_discrete_sequence=["#cba6f7"],
        )
        fig_persist.update_layout(height=300, margin=dict(t=20, b=40))
        st.plotly_chart(fig_persist, use_container_width=True)

    # ── Chart 2: Average hard arb spread over time ──
    # ── Chart 3: Flash opportunities ──
    col_spread, col_flash = st.columns(2)

    with col_spread:
        st.subheader("Avg Hard Arb Spread Over Time")
        spread_df = queries.get_avg_spread_over_time()
        if spread_df.empty:
            _empty_state("No hard arb data yet.")
        else:
            spread_df["time"] = pd.to_datetime(spread_df["hour_ts"], unit="s")
            fig_spread = px.line(
                spread_df, x="time", y="avg_spread",
                labels={"avg_spread": "Avg Spread (cents)", "time": ""},
                color_discrete_sequence=["#a6e3a1"],
            )
            fig_spread.update_layout(height=300, margin=dict(t=20, b=40))
            st.plotly_chart(fig_spread, use_container_width=True)

    with col_flash:
        st.subheader("Flash Opportunities (gone in 1 scan)")
        flash_df = queries.get_flash_opps()
        if flash_df.empty:
            _empty_state("No flash opportunity data yet.")
        else:
            flash_df["time"] = pd.to_datetime(flash_df["hour_ts"], unit="s")
            fig_flash = px.bar(
                flash_df, x="time", y="flash_count",
                labels={"flash_count": "Count", "time": ""},
                color_discrete_sequence=["#f38ba8"],
            )
            fig_flash.update_layout(height=300, margin=dict(t=20, b=40))
            st.plotly_chart(fig_flash, use_container_width=True)

    # ── Table: Time-of-day breakdown ──
    st.subheader("Best Hours for Opportunities (UTC)")
    tod_df = queries.get_time_of_day_breakdown()
    if tod_df.empty:
        _empty_state("No data yet.")
    else:
        tod_df["hour_label"] = tod_df["hour_utc"].apply(lambda h: f"{int(h):02d}:00")
        tod_df["avg_spread"] = tod_df["avg_spread"].round(1)

        col_table, col_chart = st.columns(2)
        with col_table:
            st.dataframe(
                tod_df[["hour_label", "opp_count", "avg_spread"]].rename(columns={
                    "hour_label": "Hour (UTC)",
                    "opp_count": "Opportunities",
                    "avg_spread": "Avg Spread (¢)",
                }),
                use_container_width=True, hide_index=True,
            )

        with col_chart:
            fig_tod = px.bar(
                tod_df, x="hour_label", y="opp_count",
                labels={"opp_count": "Opportunities", "hour_label": "Hour (UTC)"},
                color="avg_spread",
                color_continuous_scale="Viridis",
            )
            fig_tod.update_layout(height=300, margin=dict(t=20, b=40))
            st.plotly_chart(fig_tod, use_container_width=True)


# ══════════════════════════════════════════════════════════════════
# PAGE 6: PAPER TRADES
# ══════════════════════════════════════════════════════════════════

def page_paper_trades():
    st.title("Paper Trades — Mispricing Scanner")
    _show_refresh()

    # Auto-refresh every 60s
    if "paper_last_refresh" not in st.session_state:
        st.session_state.paper_last_refresh = time.time()
    if time.time() - st.session_state.paper_last_refresh > 60:
        st.session_state.paper_last_refresh = time.time()
        st.rerun()

    summary_df = queries.get_paper_trade_summary()
    if summary_df.empty or summary_df.iloc[0]["total_trades"] == 0:
        _empty_state(
            "No paper trades yet. Run the bot with MODE=mispricing_scanner to start collecting signals. "
            "Each signal is recorded as a hypothetical SELL YES trade."
        )
        return

    s = summary_df.iloc[0]
    total = int(s["total_trades"])
    open_count = int(s["open_trades"])
    resolved = int(s["resolved_trades"])
    wins = int(s["wins"])
    losses = int(s["losses"])
    win_rate = (wins / resolved * 100) if resolved > 0 else 0

    # Days of data
    first_ts = s["first_trade_ts"]
    last_ts = s["last_trade_ts"]
    days_running = max(1, (last_ts - first_ts) / 86400) if first_ts and last_ts else 0

    # ── Top-level metrics ──
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Total Signals", f"{total:,}", delta=f"{open_count} open")
    m2.metric("Resolved", f"{resolved:,}")
    m3.metric("Win / Loss", f"{wins} / {losses}")
    m4.metric("Win Rate", f"{win_rate:.1f}%" if resolved > 0 else "N/A")
    m5.metric("Cumulative P&L", f"{int(s['total_pnl']):+,}c" if resolved > 0 else "N/A")
    m6.metric("Avg Edge at Entry", f"{s['avg_edge_at_entry']:.1f}c")

    if days_running > 0:
        st.caption(f"Tracking for {days_running:.1f} days — need 14 days before going live")
        progress = min(1.0, days_running / 14)
        st.progress(progress, text=f"{days_running:.1f} / 14 days")

    st.markdown("---")

    # ── Breakdown by signal type ──
    col_type, col_cat = st.columns(2)

    with col_type:
        st.subheader("By Signal Type")
        type_df = queries.get_paper_trades_by_signal_type()
        if not type_df.empty:
            display = type_df.copy()
            display["win_rate"] = display.apply(
                lambda r: f"{r['wins'] / r['resolved'] * 100:.0f}%" if r["resolved"] > 0 else "—", axis=1
            )
            display["avg_pnl"] = display["avg_pnl"].apply(lambda x: f"{x:+.1f}c")
            display["avg_edge"] = display["avg_edge"].apply(lambda x: f"{x:.1f}c")
            display["pnl"] = display["pnl"].apply(lambda x: f"{int(x):+,}c")
            st.dataframe(
                display[["signal_type", "total", "resolved", "wins", "losses",
                          "win_rate", "pnl", "avg_pnl", "avg_edge"]].rename(columns={
                    "signal_type": "Type",
                    "total": "Total",
                    "resolved": "Resolved",
                    "wins": "W",
                    "losses": "L",
                    "win_rate": "Win%",
                    "pnl": "P&L",
                    "avg_pnl": "Avg P&L",
                    "avg_edge": "Avg Edge",
                }),
                use_container_width=True, hide_index=True,
            )
        else:
            _empty_state("No signal type data yet.")

    with col_cat:
        st.subheader("By Category")
        cat_df = queries.get_paper_trades_by_category()
        if not cat_df.empty:
            display = cat_df.copy()
            resolved_total = display["wins"] + display["losses"]
            display["win_rate"] = display.apply(
                lambda r: f"{r['wins'] / (r['wins'] + r['losses']) * 100:.0f}%"
                if (r["wins"] + r["losses"]) > 0 else "—", axis=1
            )
            display["pnl"] = display["pnl"].apply(lambda x: f"{int(x):+,}c")
            display["avg_edge"] = display["avg_edge"].apply(lambda x: f"{x:.1f}c")
            st.dataframe(
                display[["category", "total", "wins", "losses", "win_rate", "pnl", "avg_edge"]].rename(columns={
                    "category": "Category",
                    "total": "Total",
                    "wins": "W",
                    "losses": "L",
                    "win_rate": "Win%",
                    "pnl": "P&L",
                    "avg_edge": "Avg Edge",
                }),
                use_container_width=True, hide_index=True,
            )
        else:
            _empty_state("No category data yet.")

    st.markdown("---")

    # ── Equity curve ──
    cum_df = queries.get_paper_cumulative_pnl()
    if not cum_df.empty and len(cum_df) >= 2:
        st.subheader("Cumulative P&L (Resolved Trades)")
        cum_df["time"] = pd.to_datetime(cum_df["timestamp"], unit="s")
        fig_equity = px.line(
            cum_df, x="time", y="cumulative_pnl",
            labels={"cumulative_pnl": "Cumulative P&L (cents)", "time": ""},
            color_discrete_sequence=["#a6e3a1"],
        )
        fig_equity.add_hline(y=0, line_dash="dash", line_color="grey")
        fig_equity.update_layout(height=350, margin=dict(t=20, b=40))
        st.plotly_chart(fig_equity, use_container_width=True)
    elif resolved > 0:
        st.info(f"Only {resolved} resolved trade(s) — equity curve appears after 2+.")

    st.markdown("---")

    # ── Threshold sensitivity (near-miss analysis) ──
    st.subheader("Threshold Sensitivity — Near Misses")
    st.caption(
        "Signals that were close to firing but below threshold. "
        "Use this to evaluate whether loosening or tightening thresholds would improve results."
    )

    nm_summary = queries.get_near_miss_summary()
    nm_dist = queries.get_near_miss_gap_distribution()

    col_nm_table, col_nm_chart = st.columns(2)

    with col_nm_table:
        if not nm_summary.empty:
            display = nm_summary.copy()
            display["avg_gap"] = display["avg_gap"].apply(lambda x: f"{x:.1f}c")
            display["avg_price"] = display["avg_price"].apply(lambda x: f"{x:.0f}c")
            st.dataframe(
                display[["signal_type", "threshold_used", "near_miss_count",
                          "avg_gap", "min_gap", "max_gap", "avg_price"]].rename(columns={
                    "signal_type": "Type",
                    "threshold_used": "Threshold",
                    "near_miss_count": "Count",
                    "avg_gap": "Avg Gap",
                    "min_gap": "Min Gap",
                    "max_gap": "Max Gap",
                    "avg_price": "Avg Price",
                }),
                use_container_width=True, hide_index=True,
            )
        else:
            _empty_state("No near-misses recorded yet.")

    with col_nm_chart:
        if not nm_dist.empty:
            fig_nm = px.histogram(
                nm_dist, x="gap", color="signal_type",
                nbins=20,
                labels={"gap": "Gap (cents below threshold)", "signal_type": "Type"},
                color_discrete_sequence=["#fab387", "#89b4fa", "#a6e3a1"],
                barmode="overlay",
                opacity=0.7,
            )
            fig_nm.update_layout(
                height=300, margin=dict(t=20, b=40),
                xaxis_title="Gap (cents)", yaxis_title="Count",
            )
            st.plotly_chart(fig_nm, use_container_width=True)
        else:
            _empty_state("No near-miss distribution data yet.")

    st.markdown("---")

    # ── Full trade table ──
    st.subheader("All Paper Trades")

    trades_df = queries.get_paper_trades_all()
    if not trades_df.empty:
        # Filters
        filter_col1, filter_col2 = st.columns(2)
        with filter_col1:
            status_filter = st.selectbox("Status", ["All", "Open", "Resolved"], key="paper_status")
        with filter_col2:
            type_options = trades_df["signal_type"].unique().tolist()
            type_filter = st.multiselect("Signal type", type_options, default=type_options, key="paper_type")

        filtered = trades_df.copy()
        if status_filter == "Open":
            filtered = filtered[filtered["status"] == "open"]
        elif status_filter == "Resolved":
            filtered = filtered[filtered["status"] == "resolved"]
        filtered = filtered[filtered["signal_type"].isin(type_filter)]

        display = filtered.copy()
        display["time"] = pd.to_datetime(display["timestamp"], unit="s").dt.strftime("%m/%d %H:%M")
        display["resolved_time"] = display["resolved_at"].apply(
            lambda x: pd.to_datetime(x, unit="s").strftime("%m/%d %H:%M") if pd.notna(x) and x else "—"
        )
        display["pnl_display"] = display["pnl_cents"].apply(
            lambda x: f"{int(x):+d}c" if pd.notna(x) else "—"
        )

        display_cols = [
            "time", "signal_type", "series_ticker", "bucket_label",
            "entry_price", "fair_value_est", "overpricing_gap",
            "status", "resolved_price", "pnl_display", "resolved_time",
        ]
        st.dataframe(
            display[display_cols].rename(columns={
                "time": "Signal Time",
                "signal_type": "Type",
                "series_ticker": "Series",
                "bucket_label": "Bucket",
                "entry_price": "Sell@",
                "fair_value_est": "Fair Val",
                "overpricing_gap": "Gap",
                "status": "Status",
                "resolved_price": "Settled@",
                "pnl_display": "P&L",
                "resolved_time": "Resolved",
            }),
            use_container_width=True, hide_index=True, height=400,
        )

    # ── Recent near-misses ──
    with st.expander("Recent Near-Misses (last 50)"):
        nm_recent = queries.get_near_misses_recent(50)
        if not nm_recent.empty:
            nm_display = nm_recent.copy()
            nm_display["time"] = pd.to_datetime(nm_display["timestamp"], unit="s").dt.strftime("%m/%d %H:%M")
            nm_display_cols = [
                "time", "signal_type", "series_ticker", "bucket_label",
                "yes_price", "fair_value_est", "gap", "threshold_used",
            ]
            st.dataframe(
                nm_display[nm_display_cols].rename(columns={
                    "time": "Time",
                    "signal_type": "Type",
                    "series_ticker": "Series",
                    "bucket_label": "Bucket",
                    "yes_price": "YES Price",
                    "fair_value_est": "Fair Val",
                    "gap": "Gap",
                    "threshold_used": "Threshold",
                }),
                use_container_width=True, hide_index=True, height=300,
            )
        else:
            _empty_state("No near-misses recorded yet.")


# ── Router ────────────────────────────────────────────────────────

PAGES = {
    "Overview": page_overview,
    "Ladder Explorer": page_ladder_explorer,
    "Cross-Strike Matrix": page_cross_strike_matrix,
    "Trade Log": page_trade_log,
    "Competition Signals": page_competition_signals,
    "Paper Trades": page_paper_trades,
}

PAGES[page]()
