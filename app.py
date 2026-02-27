from flask import Flask, render_template, jsonify, request
import pandas as pd
import numpy as np
import os

app = Flask(__name__)

DEFAULT_THRESHOLDS = {
    "sched_lag_max":      0.20,   # 20% behind = 0.0 on schedule variance
    "slip_days_max":      140,    # 140 days slip = 0.0
    "net_backlog_max":    50,     # 50 net new items = 0.0
    "req_churn_max":      15,     # 15 req changes/4w = 0.0
    "defect_escape_max":  0.15,   # 15% escape rate = 0.0
    "crit_defect_ratio":  2.0,    # critical defects per team member ceiling
    "blocked_days_max":   10,     # 10 blocked days in 2w = 0.0
    "unplanned_max":      0.60,   # 60% unplanned = 0.0
    "dep_count_max":      15,     # 15 dependencies = 0.0
    "cpi_floor":          0.70,   # CPI below 0.70 = 0.0
    "spi_floor":          0.70,   # SPI below 0.70 = 0.0
    "milestone_floor":    0.50,   # below 50% milestone hit rate = 0.0
    # Kanban-specific thresholds
    "cycle_time_max":     30,     # 30-day avg cycle time = score 0.0
    "throughput_floor":   0.40,   # below 40% of rolling avg = score 0.0
    "wip_overage_max":    0.50,   # 50% over WIP limit = score 0.0
    "aging_wip_max":      5,      # 5 aging items = score 0.0
}

def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))

def directional_cov_penalty(slip_history):
    """
    Directional-aware forecast volatility penalty for the Confidence Score.

    Operates on week-over-week DELTAS of forecast slip, not raw slip values.
    This separates two distinct signals:

      1. Erraticism  — how chaotic is the forecast movement? (delta CoV)
         A forecast moving consistently in one direction = low erraticism.
         A forecast flip-flopping week to week = high erraticism.

      2. Direction   — is the forecast getting better or worse on average?
         Worsening (mean_delta > 0) amplifies the erraticism penalty.
         Improving (mean_delta < 0) reduces it.

    This correctly rewards a project whose forecast is steadily improving
    and penalizes one that is erratic or consistently worsening, solving
    the directional blindness of the original CoV-on-raw-values approach.

    Returns: (penalty_pts: float, breakdown: dict)
    """
    arr = np.array([float(v) for v in slip_history])

    if len(arr) < 2:
        return 0.0, {
            "deltas": [], "mean_delta": 0.0, "std_delta": 0.0,
            "delta_cov": 0.0, "dir_factor": 0.0, "dir_multiplier": 1.0,
            "base_penalty": 0.0, "directional_floor": 0.0, "final_penalty": 0.0,
            "note": "< 2 weeks of data — no penalty applied"
        }

    deltas     = np.diff(arr)
    mean_delta = float(np.mean(deltas))
    std_delta  = float(np.std(deltas, ddof=1)) if len(deltas) >= 2 else 0.0

    # Use max(|mean|, 10) as reference to prevent tiny mean from inflating CoV
    # when the forecast barely moves (10-day floor = ~2-week sprint reference)
    reference = max(abs(mean_delta), 10.0)
    delta_cov = clamp(std_delta / reference, 0, 2.0)

    # Base erraticism penalty: delta_cov 0→0 pts, ≥0.5→30 pts
    base_penalty = clamp(delta_cov / 0.5) * 30

    # Directional multiplier via tanh(mean_delta / 7):
    #   mean +14 d/wk => tanh(+2) ≈ +0.96 => multiplier ≈ 1.38  (amplify)
    #   mean  +7 d/wk => tanh(+1) ≈ +0.76 => multiplier ≈ 1.30
    #   mean   0 d/wk => tanh(0)  =  0    => multiplier = 1.00  (neutral)
    #   mean  -7 d/wk => tanh(-1) ≈ -0.76 => multiplier ≈ 0.70
    #   mean -14 d/wk => tanh(-2) ≈ -0.96 => multiplier ≈ 0.62  (reduce)
    dir_factor     = float(np.tanh(mean_delta / 7.0))
    dir_multiplier = 1.0 + 0.4 * dir_factor   # range: ~0.6 to ~1.4

    # Directional floor: a consistently worsening forecast earns a minimum
    # penalty even with zero erraticism. Max floor = 8 pts at ≥+14 d/wk avg.
    # Improving forecasts get no floor (improving is always rewarded).
    directional_floor = clamp(dir_factor, 0.0, 1.0) * 8.0

    raw_penalty   = base_penalty * dir_multiplier
    final_penalty = clamp(max(raw_penalty, directional_floor), 0, 40)

    return final_penalty, {
        "deltas":            [round(float(d), 1) for d in deltas],
        "mean_delta":        round(mean_delta, 2),
        "std_delta":         round(std_delta, 2),
        "reference":         round(reference, 1),
        "delta_cov":         round(delta_cov, 3),
        "dir_factor":        round(dir_factor, 3),
        "dir_multiplier":    round(dir_multiplier, 3),
        "base_penalty":      round(base_penalty, 1),
        "directional_floor": round(directional_floor, 1),
        "final_penalty":     round(final_penalty, 1),
    }

def throughput_cov_penalty(throughput_history):
    """
    Directional-aware throughput volatility penalty for Kanban Confidence Score.

    Mirror of directional_cov_penalty but with INVERTED direction sense:
      - Rising throughput (mean_delta > 0) = improving → penalty REDUCED
      - Falling throughput (mean_delta < 0) = worsening → penalty AMPLIFIED

    Returns: (penalty_pts: float, breakdown: dict)
    """
    arr = np.array([float(v) for v in throughput_history])

    if len(arr) < 2:
        return 0.0, {
            "tp_deltas": [], "tp_mean_delta": 0.0, "tp_std_delta": 0.0,
            "tp_delta_cov": 0.0, "tp_dir_factor": 0.0, "tp_dir_multiplier": 1.0,
            "tp_base_penalty": 0.0, "tp_directional_floor": 0.0, "tp_final_penalty": 0.0,
            "note": "< 2 weeks of data — no penalty applied"
        }

    deltas     = np.diff(arr)
    mean_delta = float(np.mean(deltas))
    std_delta  = float(np.std(deltas, ddof=1)) if len(deltas) >= 2 else 0.0

    # Use max(|mean|, 1) as reference — throughput numbers are smaller than slip days
    reference = max(abs(mean_delta), 1.0)
    delta_cov = clamp(std_delta / reference, 0, 2.0)

    # Base erraticism penalty: delta_cov 0→0 pts, ≥0.5→30 pts
    base_penalty = clamp(delta_cov / 0.5) * 30

    # Directional multiplier — INVERTED vs slip version:
    #   rising throughput (mean_delta > 0) → tanh > 0 → dir_factor > 0
    #   but rising = IMPROVING, so we negate dir_factor for the multiplier
    #   mean +5 items/wk => tanh(+5/3) ≈ +0.99 → dir_factor ≈ +0.99 → multiplier ≈ 0.60 (reduce)
    #   mean  0           => tanh(0)   =  0    → multiplier = 1.00 (neutral)
    #   mean -5 items/wk => tanh(-5/3) ≈ -0.99 → dir_factor ≈ -0.99 → multiplier ≈ 1.40 (amplify)
    dir_factor     = float(np.tanh(mean_delta / 3.0))
    dir_multiplier = 1.0 - 0.4 * dir_factor   # inverted sign vs slip version

    # Directional floor: consistently FALLING throughput earns a minimum penalty.
    # dir_factor > 0 = improving = no floor. dir_factor < 0 = worsening = floor up to 8 pts.
    directional_floor = clamp(-dir_factor, 0.0, 1.0) * 8.0

    raw_penalty   = base_penalty * dir_multiplier
    final_penalty = clamp(max(raw_penalty, directional_floor), 0, 40)

    return final_penalty, {
        "tp_deltas":            [round(float(d), 1) for d in deltas],
        "tp_mean_delta":        round(mean_delta, 2),
        "tp_std_delta":         round(std_delta, 2),
        "tp_reference":         round(reference, 1),
        "tp_delta_cov":         round(delta_cov, 3),
        "tp_dir_factor":        round(dir_factor, 3),
        "tp_dir_multiplier":    round(dir_multiplier, 3),
        "tp_base_penalty":      round(base_penalty, 1),
        "tp_directional_floor": round(directional_floor, 1),
        "tp_final_penalty":     round(final_penalty, 1),
    }


def generate_narrative(summary, trend_delta, conf_drivers):
    """Plain-language health narrative for the latest week."""
    name   = summary["project_name"]
    health = summary["health_score"]
    conf   = summary["confidence_score"]
    raw    = summary["raw"]
    contribs     = summary["contributions"]
    max_contribs = summary["max_contributions"]
    delivery_fw  = raw.get("delivery_framework", "planned")

    # Top detractor = metric losing the most points (gap = max possible - actual)
    gaps = {k: max_contribs[k] - contribs[k] for k in contribs}
    sorted_gaps   = sorted(gaps.items(), key=lambda x: x[1], reverse=True)
    top_detractor = sorted_gaps[0][0]
    top_gap_pts   = sorted_gaps[0][1]

    trend_str = ""
    if abs(trend_delta) >= 1.0:
        direction = "improving" if trend_delta > 0 else "declining"
        trend_str = f" The score is **{direction}** ({trend_delta:+.1f} pts week-over-week)."

    # Status opener
    if health >= 75:
        opener = f"**{name}** is in good health ({health}/100)."
    elif health >= 50:
        opener = f"**{name}** is at moderate risk ({health}/100) and warrants attention."
    else:
        opener = f"**{name}** is in a critical state ({health}/100) and requires immediate intervention."

    # Kanban-specific narrative
    if delivery_fw == "kanban":
        tp       = raw.get("throughput_last_4w")
        tp_avg   = raw.get("throughput_avg_4w")
        ct       = raw.get("avg_cycle_time")
        wip_cur  = raw.get("wip_current")
        wip_lim  = raw.get("wip_limit")

        flow_str = ""
        if tp is not None and tp_avg is not None and tp_avg > 0:
            tp_ratio = tp / tp_avg
            if tp_ratio >= 1.0:
                flow_str = f" Throughput is **healthy** ({tp} items/4w, {tp_ratio:.0%} of rolling avg)."
            elif tp_ratio >= 0.7:
                flow_str = f" Throughput is **moderate** ({tp} items/4w, {tp_ratio:.0%} of rolling avg) — monitor for trend."
            else:
                flow_str = f" Throughput is **below average** ({tp} items/4w, only {tp_ratio:.0%} of rolling avg) — flow may be impaired."

        ct_str = ""
        if ct is not None:
            if ct <= 7:
                ct_str = f" Average cycle time is fast at **{ct:.0f} days**."
            elif ct <= 14:
                ct_str = f" Average cycle time of **{ct:.0f} days** is acceptable."
            else:
                ct_str = f" Average cycle time of **{ct:.0f} days** is elevated — consider reducing WIP."

        wip_str = ""
        if wip_cur is not None and wip_lim is not None and wip_lim > 0:
            overage = (wip_cur - wip_lim) / wip_lim
            if overage > 0.2:
                wip_str = f" WIP is **{wip_cur}/{wip_lim}** — significantly over limit, risking queue buildup."
            elif overage > 0:
                wip_str = f" WIP is **{wip_cur}/{wip_lim}** — slightly over limit."
            else:
                wip_str = f" WIP is **{wip_cur}/{wip_lim}** — within limit."

        cpi = raw.get("cpi")
        evm_str = ""
        if cpi is not None:
            if cpi < 0.9:
                evm_str = f" Cost performance is concerning (CPI={cpi:.2f})."
            elif cpi > 1.05:
                evm_str = f" Cost performance is favorable (CPI={cpi:.2f})."

        conf_str = ""
        if conf < 50:
            conf_str = f" Flow confidence is low ({conf}/100) — driven primarily by {conf_drivers}."
        elif conf < 75:
            conf_str = f" Flow confidence is moderate ({conf}/100)."

        driver_str = f" The biggest drag on health is **{top_detractor}** (costing ~{top_gap_pts:.1f} pts vs its potential)."

        return (opener + trend_str + flow_str + ct_str + wip_str + evm_str + conf_str + driver_str)

    # ── Planned / Waterfall / Scrum narrative ─────────────────────────────────
    # Schedule narrative
    sched_str = ""
    if raw["sched_var_pct"] < -5:
        sched_str = f" The project is **{abs(raw['sched_var_pct']):.0f}%** behind schedule"
        if raw["slip_days"] > 0:
            sched_str += f" with a forecast slip of **{raw['slip_days']} days**."
        else:
            sched_str += "."
    elif raw["sched_var_pct"] >= 0:
        sched_str = f" Schedule is on track or ahead by {raw['sched_var_pct']:.0f}%."

    # Cost narrative
    cpi = raw.get("cpi", 1.0)
    evm_str = ""
    if cpi < 0.9:
        evm_str = f" Cost performance is concerning (CPI={cpi:.2f}): spending more than planned for work delivered."
    elif cpi > 1.05:
        evm_str = f" Cost performance is favorable (CPI={cpi:.2f})."

    # Risk narrative
    risks_high = raw.get("risks_high", 0)
    risk_str   = ""
    if risks_high >= 5:
        risk_str = f" There are **{risks_high} high-severity risks** open — escalation may be warranted."
    elif risks_high > 0:
        risk_str = f" {risks_high} high-severity risk(s) are open and should be monitored."

    # Milestone narrative
    ms_hit  = raw.get("milestones_hit", 0)
    ms_plan = raw.get("milestones_planned", 0)
    ms_str  = ""
    if ms_plan > 0:
        hit_rate = ms_hit / ms_plan
        if hit_rate < 0.7:
            ms_str = f" Only **{ms_hit}/{ms_plan}** planned milestones have been achieved ({hit_rate*100:.0f}% hit rate)."

    # Confidence narrative
    conf_str = ""
    if conf < 50:
        conf_str = f" Confidence in the forecast is low ({conf}/100) — driven primarily by {conf_drivers}."
    elif conf < 75:
        conf_str = f" Forecast confidence is moderate ({conf}/100)."

    # Top driver callout
    driver_str = f" The biggest drag on health is **{top_detractor}** (costing ~{top_gap_pts:.1f} pts vs its potential)."

    return (opener + trend_str + sched_str + evm_str + risk_str + ms_str +
            conf_str + driver_str)


def compute_scores(df_raw, thresholds=None):
    if thresholds is None:
        thresholds = DEFAULT_THRESHOLDS

    T = thresholds
    results = []

    for pid, grp in df_raw.groupby("project_id"):
        grp = grp.sort_values("week_ending").copy()
        grp["week_ending"]      = pd.to_datetime(grp["week_ending"])
        grp["planned_end_date"] = pd.to_datetime(grp["planned_end_date"], errors="coerce")
        grp["forecast_end_date"]= pd.to_datetime(grp["forecast_end_date"], errors="coerce")

        rows = []
        forecast_days_history  = []  # for planned CoV-based confidence
        throughput_history     = []  # for Kanban CoV-based confidence

        for idx, (_, r) in enumerate(grp.iterrows()):
            week_dt   = r["week_ending"]
            plan_end  = r["planned_end_date"]
            pct_comp  = r["actual_percent_complete"]
            proj_name = r["project_name"]

            delivery_fw = str(r.get("delivery_framework", "planned") or "planned").strip().lower()
            is_kanban   = (delivery_fw == "kanban")

            # ── SHARED METRIC NORMALIZATIONS ──────────────────────────

            # Backlog net growth (shared)
            net_backlog = r["backlog_items_added_last_4w"] - r["backlog_items_closed_last_4w"]
            m_backlog   = clamp(1.0 - max(0, net_backlog) / T["net_backlog_max"])

            # Requirements churn (shared)
            m_req_churn = clamp(1.0 - r["requirements_changed_last_4w"] / T["req_churn_max"])

            # Defect escape rate (shared)
            m_defect_escape = clamp(1.0 - r["defect_escape_rate_last_4w"] / T["defect_escape_max"])

            # Critical defects per team member (shared)
            crit_ratio = r["defects_open_critical"] / max(1, r["team_size"])
            m_critical = clamp(1.0 - crit_ratio / T["crit_defect_ratio"])

            # Team churn (shared)
            m_churn  = clamp(1.0 - r["team_churn_last_4w"] / r["team_size"])

            # Blocked days (shared)
            m_blocked = clamp(1.0 - r["blocked_days_last_2w"] / T["blocked_days_max"])

            # Dependencies (shared)
            m_deps = clamp(1.0 - r["dependency_count"] / T["dep_count_max"])

            # EVM: CPI and SPI (shared — SPI excluded for Kanban below)
            has_evm = ("planned_cost_to_date" in r.index and "actual_cost_to_date" in r.index and
                       pd.notna(r.get("actual_cost_to_date")) and r.get("actual_cost_to_date", 0) > 0)
            if has_evm:
                planned_pct_safe = max(r["planned_percent_complete"], 0.01)
                ev  = r["actual_percent_complete"] * r["planned_cost_to_date"] / planned_pct_safe
                ac  = r["actual_cost_to_date"]
                pv  = r["planned_cost_to_date"]
                cpi = ev / ac if ac > 0 else 1.0
                spi = ev / pv if pv > 0 else 1.0
                m_cpi = clamp((cpi - T["cpi_floor"]) / (1.0 - T["cpi_floor"]))
                m_spi = clamp((spi - T["spi_floor"]) / (1.0 - T["spi_floor"]))
            else:
                cpi, spi, m_cpi, m_spi = 1.0, 1.0, 1.0, 1.0
                ev, ac, pv = None, None, None

            # Milestone hit rate (shared)
            has_ms = ("milestones_planned_to_date" in r.index and
                      pd.notna(r.get("milestones_planned_to_date")) and
                      r.get("milestones_planned_to_date", 0) > 0)
            if has_ms:
                ms_rate     = r["milestones_hit"] / r["milestones_planned_to_date"]
                m_milestone = clamp((ms_rate - T["milestone_floor"]) / (1.0 - T["milestone_floor"]))
            else:
                ms_rate, m_milestone = 1.0, 1.0

            # ── KANBAN-SPECIFIC METRICS ────────────────────────────────
            has_kanban_cols = (
                is_kanban and
                "throughput_last_4w" in r.index and
                pd.notna(r.get("throughput_last_4w"))
            )

            if has_kanban_cols:
                tp_current = float(r["throughput_last_4w"])
                throughput_history.append(tp_current)
                tp_window  = throughput_history[-4:]
                tp_avg     = float(np.mean(tp_window)) if tp_window else tp_current
                tp_floor   = T["throughput_floor"]
                m_throughput = clamp((tp_current / max(tp_avg, 0.01) - tp_floor) / (1.0 - tp_floor))

                avg_ct   = float(r.get("avg_cycle_time_last_4w", 0) or 0)
                m_cycle_time = clamp(1.0 - avg_ct / T["cycle_time_max"])

                wip_cur  = float(r.get("wip_current", 0) or 0)
                wip_lim  = float(r.get("wip_limit", 1) or 1)
                wip_overage = max(0, wip_cur / max(wip_lim, 1) - 1.0)
                m_wip_adherence = clamp(1.0 - wip_overage / T["wip_overage_max"])

                aging_items = float(r.get("aging_wip_items", 0) or 0)
                m_aging_wip = clamp(1.0 - aging_items / T["aging_wip_max"])
            else:
                tp_current, tp_avg, avg_ct, wip_cur, wip_lim, aging_items = None, None, None, None, None, None
                m_throughput = m_cycle_time = m_wip_adherence = m_aging_wip = 1.0
                if is_kanban:
                    throughput_history.append(0)

            # Kanban target-date / forecast slip (optional)
            has_kanban_target = (
                is_kanban and
                pd.notna(plan_end) and
                pd.notna(r["forecast_end_date"])
            )
            if not is_kanban:
                # Planned projects always compute slip
                slip_days       = (r["forecast_end_date"] - plan_end).days
                m_forecast_slip = clamp(1.0 - max(0, slip_days) / T["slip_days_max"])
                forecast_days_history.append(slip_days)
            elif has_kanban_target:
                slip_days       = (r["forecast_end_date"] - plan_end).days
                m_forecast_slip = clamp(1.0 - max(0, slip_days) / T["slip_days_max"])
            else:
                slip_days       = 0
                m_forecast_slip = 1.0

            # Planned-only metrics
            if not is_kanban:
                proximity   = clamp((pct_comp - 0.3) / 0.7)
                sched_var   = r["actual_percent_complete"] - r["planned_percent_complete"]
                m_sched_var = clamp((sched_var + T["sched_lag_max"]) / T["sched_lag_max"])
                m_unplanned = clamp(1.0 - r["unplanned_work_ratio_last_4w"] / T["unplanned_max"])
            else:
                proximity   = 0.0
                sched_var   = 0.0
                m_sched_var = 1.0
                m_unplanned = 1.0

            # ── WEIGHTS ────────────────────────────────────────────────
            if is_kanban:
                # Fixed Kanban weights (no proximity)
                w_throughput    = 0.14 if has_kanban_cols else 0.0
                w_cycle_time    = 0.12 if has_kanban_cols else 0.0
                w_wip_adherence = 0.10 if has_kanban_cols else 0.0
                w_aging_wip     = 0.10 if has_kanban_cols else 0.0
                w_backlog       = 0.09
                w_req_churn     = 0.09
                w_defect_escape = 0.09
                w_critical      = 0.09
                w_churn         = 0.09
                w_blocked       = 0.09
                w_cpi           = 0.08 if has_evm else 0.0
                w_milestone     = 0.07 if has_ms  else 0.0
                w_forecast_slip = 0.08 if has_kanban_target else 0.0

                total_w = (w_throughput + w_cycle_time + w_wip_adherence + w_aging_wip +
                           w_backlog + w_req_churn + w_defect_escape + w_critical +
                           w_churn + w_blocked + w_cpi + w_milestone + w_forecast_slip)

                def wn(w): return w / total_w

                contributions = {}
                max_contributions = {}

                if has_kanban_cols:
                    contributions["Throughput Rate"]  = wn(w_throughput)    * m_throughput    * 100
                    contributions["Cycle Time"]        = wn(w_cycle_time)    * m_cycle_time    * 100
                    contributions["WIP Adherence"]     = wn(w_wip_adherence) * m_wip_adherence * 100
                    contributions["Aging WIP"]         = wn(w_aging_wip)    * m_aging_wip     * 100
                    max_contributions["Throughput Rate"]  = wn(w_throughput)    * 100
                    max_contributions["Cycle Time"]        = wn(w_cycle_time)    * 100
                    max_contributions["WIP Adherence"]     = wn(w_wip_adherence) * 100
                    max_contributions["Aging WIP"]         = wn(w_aging_wip)    * 100

                contributions["Backlog Growth"]     = wn(w_backlog)       * m_backlog      * 100
                contributions["Req. Churn"]         = wn(w_req_churn)     * m_req_churn    * 100
                contributions["Defect Escape Rate"] = wn(w_defect_escape) * m_defect_escape* 100
                contributions["Critical Defects"]   = wn(w_critical)      * m_critical     * 100
                contributions["Team Churn"]         = wn(w_churn)         * m_churn        * 100
                contributions["Blocked Days"]       = wn(w_blocked)       * m_blocked      * 100
                max_contributions["Backlog Growth"]     = wn(w_backlog)       * 100
                max_contributions["Req. Churn"]         = wn(w_req_churn)     * 100
                max_contributions["Defect Escape Rate"] = wn(w_defect_escape) * 100
                max_contributions["Critical Defects"]   = wn(w_critical)      * 100
                max_contributions["Team Churn"]         = wn(w_churn)         * 100
                max_contributions["Blocked Days"]       = wn(w_blocked)       * 100

                if has_evm:
                    contributions["CPI (Cost)"]       = wn(w_cpi) * m_cpi * 100
                    max_contributions["CPI (Cost)"]   = wn(w_cpi) * 100
                if has_ms:
                    contributions["Milestone Rate"]     = wn(w_milestone) * m_milestone * 100
                    max_contributions["Milestone Rate"] = wn(w_milestone) * 100
                if has_kanban_target:
                    contributions["Forecast Slip"]     = wn(w_forecast_slip) * m_forecast_slip * 100
                    max_contributions["Forecast Slip"] = wn(w_forecast_slip) * 100

            else:
                # ── Planned / Scrum weights (existing logic) ───────────
                w_sched_var     = 0.10 + 0.07 * proximity
                w_forecast_slip = 0.08 + 0.06 * proximity
                w_backlog       = 0.08
                w_req_churn     = 0.06
                w_defect_escape = 0.08 + 0.04 * proximity
                w_critical      = 0.07 + 0.03 * proximity
                w_churn         = 0.06
                w_blocked       = 0.06
                w_unplanned     = 0.07 - 0.03 * proximity
                w_deps          = 0.05 - 0.02 * proximity
                w_cpi           = 0.09 if has_evm else 0.0
                w_spi           = 0.07 if has_evm else 0.0
                w_milestone     = 0.07 if has_ms  else 0.0

                total_w = (w_sched_var + w_forecast_slip + w_backlog + w_req_churn +
                           w_defect_escape + w_critical + w_churn + w_blocked +
                           w_unplanned + w_deps + w_cpi + w_spi + w_milestone)

                def wn(w): return w / total_w

                contributions = {
                    "Schedule Variance":   wn(w_sched_var)     * m_sched_var     * 100,
                    "Forecast Slip":       wn(w_forecast_slip) * m_forecast_slip * 100,
                    "Backlog Growth":      wn(w_backlog)        * m_backlog       * 100,
                    "Req. Churn":          wn(w_req_churn)      * m_req_churn     * 100,
                    "Defect Escape Rate":  wn(w_defect_escape)  * m_defect_escape * 100,
                    "Critical Defects":    wn(w_critical)       * m_critical      * 100,
                    "Team Churn":          wn(w_churn)          * m_churn         * 100,
                    "Blocked Days":        wn(w_blocked)        * m_blocked       * 100,
                    "Unplanned Work":      wn(w_unplanned)      * m_unplanned     * 100,
                    "Dependencies":        wn(w_deps)           * m_deps          * 100,
                }
                max_contributions = {
                    "Schedule Variance":   wn(w_sched_var)     * 100,
                    "Forecast Slip":       wn(w_forecast_slip) * 100,
                    "Backlog Growth":      wn(w_backlog)        * 100,
                    "Req. Churn":          wn(w_req_churn)      * 100,
                    "Defect Escape Rate":  wn(w_defect_escape)  * 100,
                    "Critical Defects":    wn(w_critical)       * 100,
                    "Team Churn":          wn(w_churn)          * 100,
                    "Blocked Days":        wn(w_blocked)        * 100,
                    "Unplanned Work":      wn(w_unplanned)      * 100,
                    "Dependencies":        wn(w_deps)           * 100,
                }
                if has_evm:
                    contributions["CPI (Cost)"]         = wn(w_cpi) * m_cpi * 100
                    contributions["SPI (Schedule)"]     = wn(w_spi) * m_spi * 100
                    max_contributions["CPI (Cost)"]     = wn(w_cpi) * 100
                    max_contributions["SPI (Schedule)"] = wn(w_spi) * 100
                if has_ms:
                    contributions["Milestone Rate"]     = wn(w_milestone) * m_milestone * 100
                    max_contributions["Milestone Rate"] = wn(w_milestone) * 100

            health_score = sum(contributions.values())

            # ── CONFIDENCE SCORE ───────────────────────────────────────
            churn_penalty   = r["requirements_changed_last_4w"] * 1.0
            backlog_penalty = max(0, net_backlog) * 0.5

            if is_kanban:
                tp_window_conf = throughput_history[-4:]
                tp_cov_penalty, tp_cov_breakdown = throughput_cov_penalty(tp_window_conf)
                slip_penalty = max(0, slip_days) * 0.15 if has_kanban_target else 0.0
                confidence_score = clamp(
                    100 - tp_cov_penalty - churn_penalty - backlog_penalty - slip_penalty,
                    0, 100
                )
                cov_penalty    = 0.0
                cov_breakdown  = {
                    "deltas": [], "mean_delta": 0.0, "std_delta": 0.0,
                    "delta_cov": 0.0, "dir_factor": 0.0, "dir_multiplier": 1.0,
                    "base_penalty": 0.0, "directional_floor": 0.0, "final_penalty": 0.0,
                }
            else:
                window = forecast_days_history[-4:]
                cov_penalty, cov_breakdown = directional_cov_penalty(window)
                tp_cov_penalty   = 0.0
                tp_cov_breakdown = {
                    "tp_deltas": [], "tp_mean_delta": 0.0, "tp_std_delta": 0.0,
                    "tp_delta_cov": 0.0, "tp_dir_factor": 0.0, "tp_dir_multiplier": 1.0,
                    "tp_base_penalty": 0.0, "tp_directional_floor": 0.0, "tp_final_penalty": 0.0,
                }
                slip_penalty = max(0, slip_days) * 0.25
                confidence_score = clamp(
                    100 - cov_penalty - churn_penalty - backlog_penalty - slip_penalty,
                    0, 100
                )

            # ── TREND (week-over-week delta) ───────────────────────────
            prev_health = rows[-1]["health_score"] if rows else health_score
            trend_delta = round(health_score - prev_health, 1)

            raw_dict = {
                "delivery_framework": delivery_fw,
                "planned_end_date":   plan_end.strftime("%Y-%m-%d") if pd.notna(plan_end) else None,
                "forecast_end_date":  r["forecast_end_date"].strftime("%Y-%m-%d") if pd.notna(r["forecast_end_date"]) else None,
                "pct_complete":       round(pct_comp * 100, 1),
                "planned_pct":        round(r["planned_percent_complete"] * 100, 1),
                "sched_var_pct":      round(sched_var * 100, 1),
                "slip_days":          int(slip_days),
                "net_backlog":        int(net_backlog),
                "req_churn":          int(r["requirements_changed_last_4w"]),
                "defect_escape":      round(r["defect_escape_rate_last_4w"] * 100, 1),
                "critical_defects":   int(r["defects_open_critical"]),
                "team_churn":         int(r["team_churn_last_4w"]),
                "blocked_days":       int(r["blocked_days_last_2w"]),
                "unplanned_pct":      round(r["unplanned_work_ratio_last_4w"] * 100, 1),
                "dependencies":       int(r["dependency_count"]),
                "proximity_pct":      round(proximity * 100, 1),
                "cpi":                round(cpi, 3) if has_evm else None,
                "spi":                round(spi, 3) if (has_evm and not is_kanban) else None,
                "ev":                 round(ev, 0) if has_evm else None,
                "pv":                 round(pv, 0) if has_evm else None,
                "ac":                 round(ac, 0) if has_evm else None,
                "planned_cost":       int(r["planned_cost_to_date"]) if has_evm else None,
                "actual_cost":        int(r["actual_cost_to_date"]) if has_evm else None,
                "slip_history":       list(forecast_days_history[-4:]) if not is_kanban else [],
                "milestones_planned": int(r["milestones_planned_to_date"]) if has_ms else None,
                "milestones_hit":     int(r["milestones_hit"]) if has_ms else None,
                "risks_open":         int(r["risks_open"]) if "risks_open" in r.index and pd.notna(r.get("risks_open")) else 0,
                "risks_high":         int(r["risks_high"]) if "risks_high" in r.index and pd.notna(r.get("risks_high")) else 0,
                # Planned confidence breakdown
                "cov_deltas":         cov_breakdown.get("deltas", []),
                "cov_mean_delta":     cov_breakdown.get("mean_delta", 0.0),
                "cov_std_delta":      cov_breakdown.get("std_delta", 0.0),
                "cov_delta_cov":      cov_breakdown.get("delta_cov", 0.0),
                "cov_dir_factor":     cov_breakdown.get("dir_factor", 0.0),
                "cov_dir_mult":       cov_breakdown.get("dir_multiplier", 1.0),
                "cov_base_penalty":   cov_breakdown.get("base_penalty", 0.0),
                "cov_dir_floor":      cov_breakdown.get("directional_floor", 0.0),
                "cov_penalty":        round(cov_penalty, 1),
                "churn_penalty":      round(churn_penalty, 1),
                "backlog_penalty":    round(backlog_penalty, 1),
                "slip_penalty":       round(slip_penalty, 1),
                "cov":                cov_breakdown.get("delta_cov", 0.0),
                # Kanban fields
                "throughput_last_4w": tp_current,
                "throughput_avg_4w":  round(tp_avg, 1) if tp_avg is not None else None,
                "avg_cycle_time":     avg_ct,
                "wip_current":        wip_cur,
                "wip_limit":          wip_lim,
                "aging_wip_items":    aging_items,
                # Kanban confidence breakdown
                "tp_cov_penalty":        round(tp_cov_penalty, 1),
                "tp_cov_deltas":         tp_cov_breakdown.get("tp_deltas", []),
                "tp_cov_mean_delta":     tp_cov_breakdown.get("tp_mean_delta", 0.0),
                "tp_cov_std_delta":      tp_cov_breakdown.get("tp_std_delta", 0.0),
                "tp_cov_delta_cov":      tp_cov_breakdown.get("tp_delta_cov", 0.0),
                "tp_cov_dir_factor":     tp_cov_breakdown.get("tp_dir_factor", 0.0),
                "tp_cov_dir_mult":       tp_cov_breakdown.get("tp_dir_multiplier", 1.0),
                "tp_cov_base_penalty":   tp_cov_breakdown.get("tp_base_penalty", 0.0),
                "tp_cov_dir_floor":      tp_cov_breakdown.get("tp_directional_floor", 0.0),
            }

            rows.append({
                "project_id":        pid,
                "project_name":      proj_name,
                "week_ending":       week_dt.strftime("%Y-%m-%d"),
                "health_score":      round(health_score, 1),
                "confidence_score":  round(confidence_score, 1),
                "trend_delta":       trend_delta,
                "contributions":     contributions,
                "max_contributions": max_contributions,
                "raw":               raw_dict,
            })

        results.append(rows)

    return results


def load_data():
    csv_path = os.path.join(os.path.dirname(__file__), "data.csv")
    df = pd.read_csv(csv_path)
    df = df.dropna(how="all")
    return df


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    # Accept optional threshold overrides from query params
    thresholds = dict(DEFAULT_THRESHOLDS)
    for key in thresholds:
        val = request.args.get(key)
        if val is not None:
            try:
                thresholds[key] = float(val)
            except ValueError:
                pass

    df = load_data()
    project_series = compute_scores(df, thresholds)

    summaries, all_series = [], []
    for series in project_series:
        latest = series[-1]

        # Build confidence driver string for narrative
        raw = latest["raw"]
        is_kanban = raw.get("delivery_framework") == "kanban"
        drivers = []
        if is_kanban:
            if raw.get("tp_cov_penalty", 0) > 10:
                drivers.append("high throughput volatility")
        else:
            if raw["cov_penalty"] > 10:
                drivers.append("high forecast volatility")
        if raw["churn_penalty"] > 10:
            drivers.append("requirement churn")
        if raw["backlog_penalty"] > 10:
            drivers.append("backlog growth")
        conf_drivers = ", ".join(drivers) if drivers else "multiple signals"

        summ = {
            "project_id":        latest["project_id"],
            "project_name":      latest["project_name"],
            "health_score":      latest["health_score"],
            "confidence_score":  latest["confidence_score"],
            "trend_delta":       latest["trend_delta"],
            "contributions":     latest["contributions"],
            "max_contributions": latest["max_contributions"],
            "raw":               latest["raw"],
            "delivery_framework": raw.get("delivery_framework", "planned"),
        }
        summ["narrative"] = generate_narrative(summ, latest["trend_delta"], conf_drivers)
        summaries.append(summ)

        all_series.append({
            "project_id":                series[0]["project_id"],
            "project_name":              series[0]["project_name"],
            "delivery_framework":        series[0]["raw"].get("delivery_framework", "planned"),
            "weeks":                     [r["week_ending"] for r in series],
            "health":                    [r["health_score"] for r in series],
            "confidence":                [r["confidence_score"] for r in series],
            "trend_deltas":              [r["trend_delta"] for r in series],
            "contributions_by_week":     [r["contributions"] for r in series],
            "max_contributions_by_week": [r["max_contributions"] for r in series],
            "raw_by_week":               [r["raw"] for r in series],
        })

    return jsonify({
        "summaries":  summaries,
        "series":     all_series,
        "thresholds": thresholds,
    })


@app.route("/api/thresholds/defaults")
def default_thresholds():
    return jsonify(DEFAULT_THRESHOLDS)


if __name__ == "__main__":
    app.run(debug=True, port=5050)
