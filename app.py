from flask import Flask, render_template, jsonify
import pandas as pd
import numpy as np
import json
import os

app = Flask(__name__)

# ─────────────────────────────────────────────
#  SCORING ENGINE
# ─────────────────────────────────────────────

def compute_scores(df_raw):
    """
    For each project, compute per-week Health Score and Confidence Score
    using weighted, normalized metrics.  The weights of schedule-sensitive
    metrics escalate dynamically as the project nears its planned end date.

    Metric Families (base weights sum to 1.0):
      1. Schedule Performance    – 0.25
      2. Scope Stability         – 0.20
      3. Quality                 – 0.25
      4. Resource Stability      – 0.15
      5. Execution               – 0.15

    Each metric is normalized to [0, 1] where 1 = perfectly healthy.
    The Health Score is a weighted average of all normalized metrics.
    The Confidence Score is a separate measure of forecast reliability
    using schedule forecast drift and scope churn volatility.
    """

    results = []

    for pid, grp in df_raw.groupby("project_id"):
        grp = grp.sort_values("week_ending").copy()
        grp["week_ending"] = pd.to_datetime(grp["week_ending"])
        grp["planned_end_date"] = pd.to_datetime(grp["planned_end_date"])
        grp["forecast_end_date"] = pd.to_datetime(grp["forecast_end_date"])

        rows = []
        for _, r in grp.iterrows():
            week_dt   = r["week_ending"]
            plan_end  = r["planned_end_date"]
            proj_name = r["project_name"]
            pct_comp  = r["actual_percent_complete"]

            # ── Time-proximity multiplier ──────────────────────────────
            # As pct_complete rises above 0.5, schedule & quality weights
            # increase (they matter more when you're nearly done).
            proximity = min(1.0, max(0.0, (pct_comp - 0.3) / 0.7))  # 0→1 from 30%→100%

            # ── METRIC NORMALIZATIONS (1 = healthy, 0 = bad) ──────────

            # 1a. Schedule: % complete variance  (planned vs actual)
            sched_var = r["actual_percent_complete"] - r["planned_percent_complete"]
            # >0 = ahead; normalize: 0 at -0.20 lag, 1 at 0 or better
            m_sched_var = min(1.0, max(0.0, (sched_var + 0.20) / 0.20))

            # 1b. Schedule: forecast slip in working days
            slip_days = (r["forecast_end_date"] - plan_end).days
            # 0 slip = 1.0; each 14 days of slip drops 0.1 (max penalty at 140 days)
            m_forecast_slip = min(1.0, max(0.0, 1.0 - (max(0, slip_days) / 140)))

            # 2a. Scope: backlog net growth rate (items added vs closed)
            net_backlog = r["backlog_items_added_last_4w"] - r["backlog_items_closed_last_4w"]
            # ideal = 0 or negative; each net +10 items = -0.1
            m_backlog = min(1.0, max(0.0, 1.0 - (max(0, net_backlog) / 50)))

            # 2b. Scope: requirements churn rate
            # >10 changes/4w = bad; 0 = perfect
            m_req_churn = min(1.0, max(0.0, 1.0 - (r["requirements_changed_last_4w"] / 15)))

            # 3a. Quality: defect escape rate
            # 0 = 1.0, 0.15+ = 0.0
            m_defect_escape = min(1.0, max(0.0, 1.0 - (r["defect_escape_rate_last_4w"] / 0.15)))

            # 3b. Quality: critical defects per team member
            crit_ratio = r["defects_open_critical"] / max(1, r["team_size"])
            m_critical  = min(1.0, max(0.0, 1.0 - (crit_ratio / 2.0)))

            # 4a. Resource: team churn rate
            m_churn = min(1.0, max(0.0, 1.0 - (r["team_churn_last_4w"] / r["team_size"])))

            # 4b. Resource: blocked days
            # 0 blocked days = 1.0; 10 = 0.0
            m_blocked = min(1.0, max(0.0, 1.0 - (r["blocked_days_last_2w"] / 10)))

            # 5a. Execution: unplanned work ratio
            m_unplanned = min(1.0, max(0.0, 1.0 - (r["unplanned_work_ratio_last_4w"] / 0.6)))

            # 5b. Execution: dependency exposure
            # >10 deps = risky; each dep subtracts from score
            m_deps = min(1.0, max(0.0, 1.0 - (r["dependency_count"] / 15)))

            # ── DYNAMIC WEIGHTS ────────────────────────────────────────
            # Base weights (schedule & quality escalate near end)
            w_sched_var     = 0.12 + 0.08 * proximity   # 0.12→0.20
            w_forecast_slip = 0.10 + 0.08 * proximity   # 0.10→0.18
            w_backlog       = 0.10
            w_req_churn     = 0.08
            w_defect_escape = 0.10 + 0.05 * proximity   # 0.10→0.15
            w_critical      = 0.10 + 0.03 * proximity   # 0.10→0.13
            w_churn         = 0.08
            w_blocked       = 0.08
            w_unplanned     = 0.10 - 0.04 * proximity   # 0.10→0.06
            w_deps          = 0.06 - 0.03 * proximity   # 0.06→0.03

            total_w = (w_sched_var + w_forecast_slip + w_backlog + w_req_churn +
                       w_defect_escape + w_critical + w_churn + w_blocked +
                       w_unplanned + w_deps)

            # Normalize weights to sum to 1
            wn = lambda w: w / total_w

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

            health_score = sum(contributions.values())

            # ── CONFIDENCE SCORE ───────────────────────────────────────
            # Based on: forecast drift stability + scope churn + schedule variance
            # Uses a simplified exponential decay from 100 based on risk signals
            conf_slip_penalty    = max(0, slip_days) * 0.4        # each day of slip costs
            conf_churn_penalty   = r["requirements_changed_last_4w"] * 1.5
            conf_backlog_penalty = max(0, net_backlog) * 0.8
            confidence_score     = max(0, min(100, 100 - conf_slip_penalty
                                                       - conf_churn_penalty
                                                       - conf_backlog_penalty))

            rows.append({
                "project_id":    pid,
                "project_name":  proj_name,
                "week_ending":   week_dt.strftime("%Y-%m-%d"),
                "health_score":  round(health_score, 1),
                "confidence_score": round(confidence_score, 1),
                "contributions": contributions,
                "raw": {
                    "pct_complete":     round(pct_comp * 100, 1),
                    "planned_pct":      round(r["planned_percent_complete"] * 100, 1),
                    "sched_var_pct":    round(sched_var * 100, 1),
                    "slip_days":        int(slip_days),
                    "net_backlog":      int(net_backlog),
                    "req_churn":        int(r["requirements_changed_last_4w"]),
                    "defect_escape":    round(r["defect_escape_rate_last_4w"] * 100, 1),
                    "critical_defects": int(r["defects_open_critical"]),
                    "team_churn":       int(r["team_churn_last_4w"]),
                    "blocked_days":     int(r["blocked_days_last_2w"]),
                    "unplanned_pct":    round(r["unplanned_work_ratio_last_4w"] * 100, 1),
                    "dependencies":     int(r["dependency_count"]),
                    "proximity_pct":    round(proximity * 100, 1),
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
    df = load_data()
    project_series = compute_scores(df)

    # Build summary: latest week per project
    summaries = []
    all_series = []
    for series in project_series:
        latest = series[-1]
        summaries.append({
            "project_id":   latest["project_id"],
            "project_name": latest["project_name"],
            "health_score": latest["health_score"],
            "confidence_score": latest["confidence_score"],
            "contributions": latest["contributions"],
            "raw": latest["raw"],
        })
        all_series.append({
            "project_id":   series[0]["project_id"],
            "project_name": series[0]["project_name"],
            "weeks":        [r["week_ending"] for r in series],
            "health":       [r["health_score"] for r in series],
            "confidence":   [r["confidence_score"] for r in series],
            "contributions_by_week": [r["contributions"] for r in series],
            "raw_by_week":  [r["raw"] for r in series],
        })

    return jsonify({"summaries": summaries, "series": all_series})


if __name__ == "__main__":
    app.run(debug=True, port=5050)
