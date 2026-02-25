# Project Health Dashboard

## Setup

```bash
pip install flask pandas numpy
python app.py
```

Then open http://localhost:5050 in your browser.

## Input
Place your `data.csv` in the same directory as `app.py`.  
Required columns match `sample_project_timeseries.csv`.

## Scoring Methodology

### Health Score (0–100)
Weighted, normalized aggregate of 10 metrics across 5 families:

| Family | Metrics | Base Weight |
|---|---|---|
| Schedule Performance | % Complete Variance, Forecast Slip | 22% |
| Scope Stability | Backlog Net Growth, Req. Churn | 18% |
| Quality | Defect Escape Rate, Critical Defects | 20% |
| Resource | Team Churn, Blocked Days | 16% |
| Execution | Unplanned Work Ratio, Dependencies | 16% |

**Time-Proximity Adjustment:** As `actual_percent_complete` rises above 30%, Schedule and Quality weights increase (up to +8% each) while Execution/Dependency weights taper. This reflects the PM principle that late-stage slippage is harder to recover and defects cost more to fix near launch. All weights are renormalized to sum to 1.0 after adjustment.

Each metric is normalized to [0, 1] using domain-appropriate thresholds (e.g., a 20% schedule lag = 0.0, on/ahead = 1.0). The Health Score = Σ(normalized_metric × adjusted_weight) × 100.

### Confidence Score (0–100)
Penalty-based score derived from forecast reliability signals:
- `100 - (slip_days × 0.4) - (req_changes × 1.5) - (net_backlog × 0.8)`
- Clamped to [0, 100]

High slip + high churn = low confidence the forecast is trustworthy.
