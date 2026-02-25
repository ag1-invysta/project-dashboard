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
}

def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))

def coeff_of_variation(values):
    """Rolling CoV: std/mean. Returns 0 if stable, higher if volatile."""
    arr = np.array([float(v) for v in values])
    if len(arr) < 2 or np.mean(arr) == 0:
        return 0.0
    return float(np.std(arr, ddof=1) / abs(np.mean(arr)))

def generate_narrative(summary, trend_delta, conf_drivers):
    """Plain-language health narrative for the latest week."""
    name   = summary["project_name"]
    health = summary["health_score"]
    conf   = summary["confidence_score"]
    raw    = summary["raw"]
    contribs     = summary["contributions"]
    max_contribs = summary["max_contributions"]

    # Top detractor = metric losing the most points (gap = max possible - actual)
    # This correctly identifies what's actually hurting the score, not just the
    # lowest-weight metric.
    gaps = {k: max_contribs[k] - contribs[k] for k in contribs}
    sorted_gaps   = sorted(gaps.items(), key=lambda x: x[1], reverse=True)
    top_detractor = sorted_gaps[0][0]   # largest gap = biggest drag
    top_gap_pts   = sorted_gaps[0][1]

    # Top performer = metric closest to its max (smallest gap as % of max)
    pct_gaps = {k: gaps[k]/max_contribs[k] if max_contribs[k]>0 else 0 for k in contribs}
    top_performer = min(pct_gaps, key=pct_gaps.get)

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
    spi = raw.get("spi", 1.0)
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

    # Top driver callout — show gap (points being lost)
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
        grp["week_ending"]     = pd.to_datetime(grp["week_ending"])
        grp["planned_end_date"]= pd.to_datetime(grp["planned_end_date"])
        grp["forecast_end_date"]= pd.to_datetime(grp["forecast_end_date"])

        rows = []
        forecast_days_history = []  # for CoV-based confidence

        for idx, (_, r) in enumerate(grp.iterrows()):
            week_dt  = r["week_ending"]
            plan_end = r["planned_end_date"]
            pct_comp = r["actual_percent_complete"]
            proj_name= r["project_name"]

            # ── Time-proximity factor ──────────────────────────────────
            proximity = clamp((pct_comp - 0.3) / 0.7)

            # ── METRIC NORMALIZATIONS ──────────────────────────────────

            # 1a. Schedule variance
            sched_var   = r["actual_percent_complete"] - r["planned_percent_complete"]
            m_sched_var = clamp((sched_var + T["sched_lag_max"]) / T["sched_lag_max"])

            # 1b. Forecast slip
            slip_days       = (r["forecast_end_date"] - plan_end).days
            m_forecast_slip = clamp(1.0 - max(0, slip_days) / T["slip_days_max"])
            forecast_days_history.append(slip_days)

            # 2a. Backlog net growth
            net_backlog = r["backlog_items_added_last_4w"] - r["backlog_items_closed_last_4w"]
            m_backlog   = clamp(1.0 - max(0, net_backlog) / T["net_backlog_max"])

            # 2b. Requirements churn
            m_req_churn = clamp(1.0 - r["requirements_changed_last_4w"] / T["req_churn_max"])

            # 3a. Defect escape rate
            m_defect_escape = clamp(1.0 - r["defect_escape_rate_last_4w"] / T["defect_escape_max"])

            # 3b. Critical defects per team member
            crit_ratio  = r["defects_open_critical"] / max(1, r["team_size"])
            m_critical  = clamp(1.0 - crit_ratio / T["crit_defect_ratio"])

            # 4a. Team churn
            m_churn  = clamp(1.0 - r["team_churn_last_4w"] / r["team_size"])

            # 4b. Blocked days
            m_blocked = clamp(1.0 - r["blocked_days_last_2w"] / T["blocked_days_max"])

            # 5a. Unplanned work
            m_unplanned = clamp(1.0 - r["unplanned_work_ratio_last_4w"] / T["unplanned_max"])

            # 5b. Dependencies
            m_deps = clamp(1.0 - r["dependency_count"] / T["dep_count_max"])

            # 6a. EVM: CPI (Cost Performance Index)
            has_evm = ("planned_cost_to_date" in r and "actual_cost_to_date" in r and
                       r["actual_cost_to_date"] > 0)
            if has_evm:
                ev  = r["actual_percent_complete"] * r["planned_cost_to_date"] / max(r["planned_percent_complete"], 0.01)
                ac  = r["actual_cost_to_date"]
                pv  = r["planned_cost_to_date"]
                cpi = ev / ac if ac > 0 else 1.0
                spi = ev / pv if pv > 0 else 1.0
                m_cpi = clamp((cpi - T["cpi_floor"]) / (1.0 - T["cpi_floor"]))
                m_spi = clamp((spi - T["spi_floor"]) / (1.0 - T["spi_floor"]))
            else:
                cpi, spi, m_cpi, m_spi = 1.0, 1.0, 1.0, 1.0

            # 7. Milestone hit rate
            has_ms = ("milestones_planned_to_date" in r and r["milestones_planned_to_date"] > 0)
            if has_ms:
                ms_rate = r["milestones_hit"] / r["milestones_planned_to_date"]
                m_milestone = clamp((ms_rate - T["milestone_floor"]) / (1.0 - T["milestone_floor"]))
            else:
                ms_rate, m_milestone = 1.0, 1.0

            # ── DYNAMIC WEIGHTS ────────────────────────────────────────
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
            # max_contributions = what each metric would contribute at norm=1.0
            # gap = max - actual = points being lost to this metric's underperformance
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
                contributions["CPI (Cost)"]       = wn(w_cpi) * m_cpi * 100
                contributions["SPI (Schedule)"]   = wn(w_spi) * m_spi * 100
                max_contributions["CPI (Cost)"]   = wn(w_cpi) * 100
                max_contributions["SPI (Schedule)"] = wn(w_spi) * 100
            if has_ms:
                contributions["Milestone Rate"]     = wn(w_milestone) * m_milestone * 100
                max_contributions["Milestone Rate"] = wn(w_milestone) * 100

            health_score = sum(contributions.values())

            # ── CONFIDENCE SCORE (CoV-based) ───────────────────────────
            # Uses rolling coefficient of variation of forecast_end slip
            # over all available weeks up to now. More volatile = lower confidence.
            window = forecast_days_history[-4:]  # up to 4-week rolling window
            cov    = coeff_of_variation(window)
            # CoV of 0 = perfectly stable = 100 confidence
            # CoV of 0.5+ = highly volatile = low confidence
            cov_penalty = clamp(cov / 0.5) * 40   # up to 40pt penalty from volatility

            # Additional penalties for current state
            churn_penalty   = r["requirements_changed_last_4w"] * 1.0
            backlog_penalty = max(0, net_backlog) * 0.5
            slip_penalty    = max(0, slip_days) * 0.25

            confidence_score = clamp(100 - cov_penalty - churn_penalty - backlog_penalty - slip_penalty, 0, 100)

            # ── TREND (week-over-week delta) ───────────────────────────
            prev_health = rows[-1]["health_score"] if rows else health_score
            trend_delta = round(health_score - prev_health, 1)

            rows.append({
                "project_id":        pid,
                "project_name":      proj_name,
                "week_ending":       week_dt.strftime("%Y-%m-%d"),
                "health_score":      round(health_score, 1),
                "confidence_score":  round(confidence_score, 1),
                "trend_delta":       trend_delta,
                "contributions":     contributions,
                "max_contributions": max_contributions,
                "raw": {
                    "pct_complete":      round(pct_comp * 100, 1),
                    "planned_pct":       round(r["planned_percent_complete"] * 100, 1),
                    "sched_var_pct":     round(sched_var * 100, 1),
                    "slip_days":         int(slip_days),
                    "net_backlog":       int(net_backlog),
                    "req_churn":         int(r["requirements_changed_last_4w"]),
                    "defect_escape":     round(r["defect_escape_rate_last_4w"] * 100, 1),
                    "critical_defects":  int(r["defects_open_critical"]),
                    "team_churn":        int(r["team_churn_last_4w"]),
                    "blocked_days":      int(r["blocked_days_last_2w"]),
                    "unplanned_pct":     round(r["unplanned_work_ratio_last_4w"] * 100, 1),
                    "dependencies":      int(r["dependency_count"]),
                    "proximity_pct":     round(proximity * 100, 1),
                    "cpi":               round(cpi, 3) if has_evm else None,
                    "spi":               round(spi, 3) if has_evm else None,
                    "ev":                round(ev, 0) if has_evm else None,
                    "pv":                round(r["planned_cost_to_date"], 0) if has_evm else None,
                    "ac":                round(r["actual_cost_to_date"], 0) if has_evm else None,
                    "planned_cost":      int(r["planned_cost_to_date"]) if has_evm else None,
                    "actual_cost":       int(r["actual_cost_to_date"]) if has_evm else None,
                    "slip_history":      list(forecast_days_history[-4:]),
                    "milestones_planned":int(r["milestones_planned_to_date"]) if has_ms else None,
                    "milestones_hit":    int(r["milestones_hit"]) if has_ms else None,
                    "risks_open":        int(r["risks_open"]) if "risks_open" in r else 0,
                    "risks_high":        int(r["risks_high"]) if "risks_high" in r else 0,
                    "cov":               round(cov, 3),
                    "cov_penalty":       round(cov_penalty, 1),
                    "churn_penalty":     round(churn_penalty, 1),
                    "backlog_penalty":   round(backlog_penalty, 1),
                    "slip_penalty":      round(slip_penalty, 1),
                }
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
        drivers = []
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
        }
        summ["narrative"] = generate_narrative(summ, latest["trend_delta"], conf_drivers)
        summaries.append(summ)

        all_series.append({
            "project_id":                series[0]["project_id"],
            "project_name":              series[0]["project_name"],
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
