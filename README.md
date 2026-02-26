# Project Health Dashboard

A browser-based project portfolio health monitoring tool built with Python and Flask. The dashboard ingests weekly project data from a CSV file and produces per-project **Health Scores** and **Confidence Scores** using weighted, normalized metrics grounded in established project management methodologies — including Earned Value Management (EVM), schedule performance analysis, quality indicators, and resource stability measures. All scoring is fully transparent: every number on screen can be traced back to its source data and formula.

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running the Application](#running-the-application)
- [Input Data Format](#input-data-format)
- [Scoring Methodology](#scoring-methodology)
  - [Health Score](#health-score)
  - [Metric Families and Normalization](#metric-families-and-normalization)
  - [Time-Proximity Weight Adjustment](#time-proximity-weight-adjustment)
  - [Confidence Score](#confidence-score)
  - [Trend Delta](#trend-delta)
  - [Biggest Drag Identification](#biggest-drag-identification)
- [Threshold Configuration](#threshold-configuration)
- [API Reference](#api-reference)
- [Project Structure](#project-structure)
- [Known Limitations and Future Improvements](#known-limitations-and-future-improvements)

---

## Features

- **Portfolio overview cards** — one card per project showing Health Score, Confidence Score, week-over-week trend delta (▲/▼), and key at-a-glance metrics
- **Per-project detail panel** — activated by clicking any card, showing:
  - Auto-generated plain-language health narrative
  - KPI row with CPI, SPI, Cost Variance, Milestone Rate, Open Risks, Forecast Slip, and Forecast CoV
  - Health & Confidence trend line chart over all available weeks
  - Score composition doughnut chart for the latest week
  - Stacked bar chart showing how each metric contributes to the health score week-over-week
  - Full metric breakdown table sorted by gap (biggest drag first), with raw values, normalization formulas, normalized scores, weights, actual vs. max contribution, and points lost
  - Confidence score derivation table showing each penalty component
- **Threshold configuration panel** — all 12 normalization thresholds are adjustable via a settings UI; changes immediately rescore all projects
- **Interactive tooltips** — hovering over CPI, SPI, and Forecast CoV KPI cards shows step-by-step derivation with actual EV, PV, and AC values
- **Export** — full dashboard and per-project detail exportable as PNG or print-to-PDF

---

## Requirements

- Python 3.8 or higher
- pip

Python package dependencies:

```
flask>=2.0
pandas>=1.3
numpy>=1.21
```

---

## Installation

**1. Clone or download the project**

```bash
git clone <your-repo-url>
cd project-health-dashboard
```

Or simply place `app.py`, `data.csv`, and the `templates/` folder in the same directory.

**2. Install dependencies**

```bash
pip install flask pandas numpy
```

Or using a virtual environment (recommended):

```bash
python -m venv venv
source venv/bin/activate        # macOS/Linux
venv\Scripts\activate           # Windows
pip install flask pandas numpy
```

---

## Running the Application

```bash
python app.py
```

The server starts on port 5050 by default. Open your browser to:

```
http://localhost:5050
```

To use a different port, change the last line of `app.py`:

```python
app.run(debug=True, port=YOUR_PORT)
```

For production use, run behind a WSGI server such as Gunicorn:

```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:5050 app:app
```

---

## Input Data Format

The application reads a file named `data.csv` from the same directory as `app.py`. Each row represents one project's data for one week. Multiple rows per project (one per week) are expected and used to compute trends and rolling statistics.

### Required Columns

| Column | Type | Description |
|---|---|---|
| `project_id` | string | Unique project identifier (e.g. `P-1001`) |
| `project_name` | string | Human-readable project name |
| `week_ending` | date (YYYY-MM-DD) | Saturday or Friday of the reporting week |
| `planned_end_date` | date (YYYY-MM-DD) | Original planned completion date — should not change week to week |
| `forecast_end_date` | date (YYYY-MM-DD) | Current forecast completion date — updated each week |
| `planned_percent_complete` | decimal (0–1) | What % complete the project *should* be this week per the baseline plan |
| `actual_percent_complete` | decimal (0–1) | What % complete the project actually is |
| `backlog_items_added_last_4w` | integer | New backlog items added in the past 4 weeks |
| `backlog_items_closed_last_4w` | integer | Backlog items closed/completed in the past 4 weeks |
| `requirements_changed_last_4w` | integer | Number of requirement changes in the past 4 weeks |
| `defects_open` | integer | Total open defects |
| `defects_open_critical` | integer | Open defects classified as critical or blocker severity |
| `defect_escape_rate_last_4w` | decimal (0–1) | Ratio of defects found post-release vs. total defects found |
| `blocked_days_last_2w` | integer | Team-days lost to blockers or impediments in the past 2 weeks |
| `dependency_count` | integer | Number of active external dependencies |
| `team_size` | integer | Current number of people on the team |
| `team_churn_last_4w` | integer | Number of team members who departed in the past 4 weeks |
| `unplanned_work_ratio_last_4w` | decimal (0–1) | Fraction of total work that was unplanned (e.g. 0.30 = 30% unplanned) |

### Optional Columns (enable EVM and milestone scoring)

| Column | Type | Description |
|---|---|---|
| `planned_cost_to_date` | number | Cumulative planned spend through this week (Planned Value / PV) |
| `actual_cost_to_date` | number | Cumulative actual spend through this week (Actual Cost / AC) |
| `milestones_planned_to_date` | integer | Number of milestones that should have been achieved by this week |
| `milestones_hit` | integer | Number of milestones actually achieved to date |
| `risks_open` | integer | Total number of open risks in the risk register |
| `risks_high` | integer | Number of open risks rated high severity |

If the optional EVM columns are absent or zero, CPI and SPI are excluded from scoring and their weights are redistributed across the remaining metrics automatically. Milestone and risk columns are also optional and degrade gracefully if missing.

### Sample Data

A sample file `data.csv` is included with five projects across nine weeks, covering a range of health states and trajectory patterns:

| Project | Planned End | State | Health | Confidence | Story |
|---|---|---|---|---|---|
| Mobile App Reliability | May 15, 2026 | Healthy | ~100 | ~97 | Consistently ahead of schedule; forecast 3 days early |
| Cloud Infrastructure Uplift | Sep 30, 2026 | Healthy | ~94 | ~81 | Ahead of plan throughout; minor complexity uptick late |
| Payments Modernization | Jun 30, 2026 | Healthy (recovering) | ~93 | ~95 | Struggled early, steadily recovered; now 3d slip |
| Customer Portal Redesign | Jul 31, 2026 | At Risk | ~52 | ~81 | Scope explosion mid-flight, PM intervention stabilizing |
| Data Platform Migration | Aug 15, 2026 | Critical | ~16 | ~43 | Continued deterioration; 89d slip, CPI critical |

---

## Scoring Methodology

### Health Score

The Health Score is a single number from 0 to 100 representing overall project health for a given week. It is computed as a **weighted sum of normalized metrics**:

```
Health Score = Σ ( normalized_metric_i × adjusted_weight_i ) × 100
```

All adjusted weights sum to 1.0, and each normalized metric is clamped to [0.0, 1.0] where 1.0 is fully healthy and 0.0 is worst case.

---

### Metric Families and Normalization

Metrics are organized into families. Each is independently normalized using a linear formula with configurable thresholds. A score of 1.0 means the metric is at or better than the ideal; 0.0 means it has reached the threshold for maximum concern.

#### Family 1 — Schedule Performance (base weight ~22%)

| Metric | Normalization Formula | Threshold Key |
|---|---|---|
| Schedule Variance | `clamp((actual% − planned% + lag_max) / lag_max, 0, 1)` | `sched_lag_max` (default 0.20) |
| Forecast Slip | `clamp(1 − slip_days / slip_days_max, 0, 1)` | `slip_days_max` (default 140) |

Schedule Variance measures how far ahead or behind the team is against the baseline plan. A variance of 0% maps to 1.0; a lag of `sched_lag_max` or worse maps to 0.0. Forecast Slip measures how many calendar days the current forecast end date has drifted beyond the original planned end.

#### Family 2 — Scope Stability (base weight ~18%)

| Metric | Normalization Formula | Threshold Key |
|---|---|---|
| Backlog Growth | `clamp(1 − max(0, added−closed) / net_backlog_max, 0, 1)` | `net_backlog_max` (default 50) |
| Req. Churn | `clamp(1 − changes_4w / req_churn_max, 0, 1)` | `req_churn_max` (default 15) |

Net backlog growth captures whether scope is expanding faster than it is being delivered. Requirements churn captures mid-flight scope instability, which is a leading indicator of rework and schedule risk.

#### Family 3 — Quality (base weight ~20%)

| Metric | Normalization Formula | Threshold Key |
|---|---|---|
| Defect Escape Rate | `clamp(1 − escape_rate / defect_escape_max, 0, 1)` | `defect_escape_max` (default 0.15) |
| Critical Defects | `clamp(1 − (critical / team_size) / crit_defect_ratio, 0, 1)` | `crit_defect_ratio` (default 2.0) |

Defect escape rate (defects found in production / total defects found) penalizes defects that slipped through testing. Critical defects are normalized per team member to prevent large teams from being unfairly penalized for having more total defects in absolute terms.

#### Family 4 — Resource Stability (base weight ~16%)

| Metric | Normalization Formula | Threshold Key |
|---|---|---|
| Team Churn | `clamp(1 − churn_4w / team_size, 0, 1)` | *(uses team_size as natural denominator)* |
| Blocked Days | `clamp(1 − blocked_2w / blocked_days_max, 0, 1)` | `blocked_days_max` (default 10) |

Team churn is normalized as a proportion of team size so that losing 2 people from a team of 5 is treated very differently from losing 2 from a team of 20. Blocked days captures execution impediments over a rolling 2-week window.

#### Family 5 — Execution (base weight ~16%)

| Metric | Normalization Formula | Threshold Key |
|---|---|---|
| Unplanned Work | `clamp(1 − unplanned_ratio / unplanned_max, 0, 1)` | `unplanned_max` (default 0.60) |
| Dependencies | `clamp(1 − dep_count / dep_count_max, 0, 1)` | `dep_count_max` (default 15) |

#### Family 6 — EVM / Cost Performance (base weight ~16%, optional)

Present only when `planned_cost_to_date` and `actual_cost_to_date` columns are populated.

| Metric | Normalization Formula | Threshold Key |
|---|---|---|
| CPI (Cost) | `clamp((CPI − cpi_floor) / (1 − cpi_floor), 0, 1)` | `cpi_floor` (default 0.70) |
| SPI (Schedule) | `clamp((SPI − spi_floor) / (1 − spi_floor), 0, 1)` | `spi_floor` (default 0.70) |

**Earned Value (EV)** is computed as:
```
EV = actual_percent_complete × planned_cost_to_date / planned_percent_complete
```

Then:
```
CPI = EV / AC     (>1.0 = under budget, <1.0 = over budget)
SPI = EV / PV     (>1.0 = ahead of schedule, <1.0 = behind)
```

The floor-based normalization means a CPI at or below the floor (default 0.70) maps to 0.0, and a CPI of 1.0 maps to 1.0. CPI values above 1.0 are clamped at 1.0 (no bonus for being under budget beyond the score ceiling). Research on EVM stability suggests CPI is a strong predictor of final cost performance once a project reaches 20% completion — a CPI of 0.83 at that point implies a final cost of approximately `Budget ÷ 0.83`.

#### Family 7 — Milestone Performance (base weight ~7%, optional)

Present only when `milestones_planned_to_date` and `milestones_hit` columns are populated.

| Metric | Normalization Formula | Threshold Key |
|---|---|---|
| Milestone Rate | `clamp((hit_rate − milestone_floor) / (1 − milestone_floor), 0, 1)` | `milestone_floor` (default 0.50) |

---

### Time-Proximity Weight Adjustment

Base weights are dynamically adjusted based on how far through its lifecycle the project is. This reflects the PM principle that certain risks matter more as a project approaches completion — late-stage slippage is harder to recover from, defects cost significantly more to fix near launch, and new scope has diminishing but still real impact.

The **proximity factor** is:

```
proximity = clamp((actual_percent_complete − 0.30) / 0.70, 0, 1)
```

This produces 0 when the project is at or below 30% complete, rising linearly to 1.0 at 100% complete.

The proximity factor scales specific weights:

| Metric | Weight at 30% complete | Weight at 100% complete | Direction |
|---|---|---|---|
| Schedule Variance | 10% | 17% | ↑ increases |
| Forecast Slip | 8% | 14% | ↑ increases |
| Defect Escape Rate | 8% | 12% | ↑ increases |
| Critical Defects | 7% | 10% | ↑ increases |
| Unplanned Work | 7% | 4% | ↓ decreases |
| Dependencies | 5% | 3% | ↓ decreases |

After adjustment, **all weights are renormalized to sum to 1.0**, ensuring the Health Score always reflects a true 0–100 scale regardless of which optional metric families are present or what the proximity factor is.

---

### Confidence Score

The Confidence Score (0–100) answers a different question from the Health Score: *how much can you trust that the current forecast will hold?* A project can be healthy yet have low forecast confidence (the end date has been moving around), or be in poor health but have a stable, believable forecast.

The score is computed using a penalty-based approach starting from 100:

```
Confidence = clamp(100 − CoV_penalty − churn_penalty − backlog_penalty − slip_penalty, 0, 100)
```

#### CoV Penalty — up to 40 points

The primary driver is a **directional, delta-based Coefficient of Variation** computed over a rolling 4-week window. Crucially, the CoV is computed on the **week-over-week changes** (deltas) in forecast slip — not the raw slip values themselves. This separates two distinct signals:

- **Erraticism** — how chaotic is the forecast movement? A forecast moving consistently in one direction scores low erraticism. A forecast flip-flopping week to week scores high.
- **Direction** — is the average change worsening or improving? A worsening trend amplifies the penalty; an improving trend reduces it.

This design prevents a common failure mode of naive CoV: a project whose forecast end date is steadily improving each week would have been incorrectly penalized under a raw-value approach because the small denominator inflated the ratio. Under the delta approach it is correctly rewarded.

**Step 1 — compute deltas:**
```
deltas = diff(slip_history)   # e.g. [10,15,20,18] → [+5, +5, −2]
```

**Step 2 — delta CoV (erraticism):**
```
reference  = max(|mean(deltas)|, 10)   # 10-day floor prevents inflation near zero
delta_cov  = clamp(std(deltas) / reference, 0, 2.0)
base_penalty = clamp(delta_cov / 0.5) × 30   # up to 30 pts
```

**Step 3 — directional multiplier:**
```
dir_factor     = tanh(mean(deltas) / 7)        # smoothly bounded −1 to +1
dir_multiplier = 1.0 + 0.4 × dir_factor        # range: ~0.6 (improving) to ~1.4 (worsening)
```

**Step 4 — directional floor** (worsening forecasts earn a minimum penalty even when movement is orderly):
```
directional_floor = clamp(dir_factor, 0, 1) × 8   # up to 8 pts; 0 for improving forecasts
```

**Step 5 — final penalty:**
```
CoV_penalty = clamp(max(base_penalty × dir_multiplier, directional_floor), 0, 40)
```

| delta_cov Range | Interpretation |
|---|---|
| 0.0 – 0.1 | Forecast movement is consistent and predictable |
| 0.1 – 0.5 | Moderate erraticism; some week-to-week variation |
| > 0.5 | High erraticism; forecast is unreliable |

The full derivation chain — slip history, deltas, mean delta, delta CoV, directional multiplier, floor, and final penalty — is visible in the Forecast CoV KPI tooltip and the Confidence Score derivation table in the detail panel.

#### Additional Penalties

| Signal | Formula | Rationale |
|---|---|---|
| Requirements Churn | `req_changes_4w × 1.0` | High churn predicts future forecast instability |
| Backlog Net Growth | `max(0, added−closed) × 0.5` | Expanding scope threatens the forecast |
| Forecast Slip | `max(0, slip_days) × 0.25` | Current slip magnitude is a direct confidence deduction |

---

### Trend Delta

The week-over-week Health Score change is displayed on each portfolio card as a ▲/▼ badge:

```
trend_delta = health_score(this_week) − health_score(last_week)
```

This is distinct from the absolute Health Score and is particularly useful for spotting projects that are technically above threshold but deteriorating rapidly, or low-scoring projects that are recovering.

---

### Biggest Drag Identification

The auto-generated narrative identifies the metric contributing most to health score underperformance using the **gap** — the difference between what a metric *could* contribute at a perfect normalized score of 1.0 and what it *actually* contributes:

```
gap_i = max_contribution_i − actual_contribution_i
      = (adjusted_weight_i × 100) × (1 − normalized_score_i)
```

The metric with the largest gap is the biggest drag. The metrics breakdown table is sorted by gap descending so the most impactful issues appear at the top.

This is more meaningful than identifying the lowest-contributing metric in absolute terms, which conflates intentionally low weight (by design) with poor performance. A Dependencies metric scoring 3.2/3.7 possible points is not a drag — it is nearly perfect. A CPI metric scoring 0/9.6 possible points is.

---

## Threshold Configuration

All normalization thresholds are configurable via the ⚙ Thresholds button in the header. Changes take effect immediately and rescore all projects without restarting the server.

Thresholds can also be passed as query parameters to the API for programmatic use:

```
GET /api/data?sched_lag_max=0.15&slip_days_max=90&cpi_floor=0.80
```

### Default Thresholds Reference

| Key | Default | Meaning |
|---|---|---|
| `sched_lag_max` | 0.20 | A 20% schedule lag scores 0.0 on Schedule Variance |
| `slip_days_max` | 140 | 140 days of forecast slip scores 0.0 on Forecast Slip |
| `net_backlog_max` | 50 | 50 net new backlog items per 4 weeks scores 0.0 |
| `req_churn_max` | 15 | 15 requirement changes per 4 weeks scores 0.0 |
| `defect_escape_max` | 0.15 | 15% defect escape rate scores 0.0 |
| `crit_defect_ratio` | 2.0 | 2 critical defects per team member scores 0.0 |
| `blocked_days_max` | 10 | 10 blocked days in a 2-week window scores 0.0 |
| `unplanned_max` | 0.60 | 60% unplanned work ratio scores 0.0 |
| `dep_count_max` | 15 | 15 active dependencies scores 0.0 |
| `cpi_floor` | 0.70 | CPI at or below 0.70 scores 0.0 |
| `spi_floor` | 0.70 | SPI at or below 0.70 scores 0.0 |
| `milestone_floor` | 0.50 | Milestone hit rate at or below 50% scores 0.0 |

Thresholds should be calibrated to your organization's risk tolerance and project type. A 12-month infrastructure program warrants different slip thresholds than a 6-week feature release.

---

## API Reference

The application exposes a lightweight REST API consumed by the frontend. It can also be called programmatically.

### `GET /`

Returns the dashboard HTML page.

---

### `GET /api/data`

Returns all project scores, contributions, raw values, series data, and narratives.

**Query parameters:** Any threshold key (see above) overrides the default for this request.

**Response shape (abbreviated):**

```json
{
  "summaries": [
    {
      "project_id": "P-1001",
      "project_name": "Payments Modernization",
      "health_score": 71.0,
      "confidence_score": 67.4,
      "trend_delta": 10.6,
      "contributions": {
        "Schedule Variance": 8.23,
        "Forecast Slip": 8.27,
        "CPI (Cost)": 3.79
      },
      "max_contributions": {
        "Schedule Variance": 11.75,
        "Forecast Slip": 11.26,
        "CPI (Cost)": 9.28
      },
      "narrative": "Payments Modernization is at moderate risk (71.0/100)...",
      "raw": {
        "planned_end_date": "2026-06-30",
        "forecast_end_date": "2026-07-03",
        "pct_complete": 74.0,
        "planned_pct": 75.0,
        "sched_var_pct": -1.0,
        "slip_days": 3,
        "cpi": 0.993,
        "spi": 0.987,
        "ev": 596400,
        "pv": 600000,
        "ac": 604000,
        "slip_history": [18, 14, 10, 7, 5, 3],
        "cov_deltas": [-4.0, -4.0, -3.0, -2.0, -2.0],
        "cov_mean_delta": -3.0,
        "cov_std_delta": 0.89,
        "cov_delta_cov": 0.089,
        "cov_dir_factor": -0.394,
        "cov_dir_mult": 0.842,
        "cov_base_penalty": 5.4,
        "cov_dir_floor": 0.0,
        "cov_penalty": 4.5,
        "churn_penalty": 1.0,
        "backlog_penalty": 0.0,
        "slip_penalty": 0.75,
        "risks_high": 1,
        "milestones_hit": 11,
        "milestones_planned": 12
      }
    }
  ],
  "series": [
    {
      "project_id": "P-1001",
      "project_name": "Payments Modernization",
      "weeks": ["2026-02-07", "2026-02-14", "2026-02-21", "2026-02-28"],
      "health": [60.4, 63.1, 67.8, 71.0],
      "confidence": [72.0, 69.5, 68.2, 67.4],
      "trend_deltas": [0, 2.7, 4.7, 3.2],
      "contributions_by_week": [{}, {}, {}, {}],
      "max_contributions_by_week": [{}, {}, {}, {}],
      "raw_by_week": [{}, {}, {}, {}]
    }
  ],
  "thresholds": {
    "sched_lag_max": 0.20,
    "slip_days_max": 140
  }
}
```

---

### `GET /api/thresholds/defaults`

Returns the default threshold values as a flat JSON object. Useful for populating a settings UI or building tooling on top of the API.

---

## Project Structure

```
project-health-dashboard/
│
├── app.py                  # Flask application, scoring engine, narrative generator, API routes
├── data.csv                # Weekly project input data (replace with your own)
├── README.md               # This file
│
└── templates/
    └── index.html          # Single-page dashboard UI (HTML + CSS + JS, no build step)
```

All frontend logic is self-contained in `templates/index.html`. There are no compiled assets or build steps. Chart.js (v4.4.1) and html2canvas (v1.4.1) are loaded from the Cloudflare CDN.

---

## Known Limitations and Future Improvements

**CSV-only input, no persistence** — Data must be provided as a flat CSV file updated manually each week. A natural next step is a lightweight SQLite backend that supports week-by-week data entry through the UI, annotations per project per week, and full history without file management.

**No authentication** — The application has no login or access control. It is intended for local or trusted-network use only. For broader deployment, add authentication before exposing it externally.

**SPI convergence near project end** — SPI naturally converges toward 1.0 as a project nears completion because PV stops accumulating while the team works to finish. This is a known limitation of the traditional SPI formula and can make late-project schedule performance look better than it is. The time-proximity weight adjustments partially compensate, but a more rigorous fix would switch to SPI(t) — a time-based variant — in later project phases.

**Confidence score with fewer than 3 data points** — The directional CoV requires at least 2 weeks of data to compute deltas, and the erraticism component (standard deviation of deltas) requires at least 3 weeks. For a project's first two weeks, the CoV penalty is zero regardless of trend, meaning confidence is driven solely by the churn, backlog, and slip penalties. This resolves naturally once sufficient history accumulates.

**Single portfolio, single file** — All projects must reside in one CSV file. For multi-portfolio environments, consider adding a `portfolio` column and a filter control in the UI, or a file-picker to load different portfolio files.

**Export rendering fidelity** — The PNG and PDF export features use html2canvas, which has known limitations with certain CSS effects. Very long detail panels or complex gradient backgrounds may not render with full fidelity in exported images. For high-quality stakeholder-facing exports, a server-side PDF generation approach (e.g. WeasyPrint or headless Chrome via Playwright) would produce better results.
