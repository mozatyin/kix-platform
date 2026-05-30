# Observability Dashboard — ML Smart Bidding & Network-Effect K-factor

**Status:** Wave-C production telemetry surface
**Scope:** verifies Bible claims (LightGBM Smart Bidding, viral K > 0.3,
attribution) in real traffic; provides alerting + offline validation
hooks for ops + ML engineering.

The dashboard is fed by `app/services/ml_observability.py` and the
endpoints exposed by `app/routers/observability.py`. All data lives in
Redis (sorted sets + hashes + counters) — no new heavy dependencies, no
PG migration, < 1 ms hot-path overhead.

---

## 1. Reference dashboard layout (Grafana JSON spec)

Suggested panel grid for a single Grafana dashboard titled
**"KiX Wave-C — ML + Viral"**. Data source is the JSON API plugin
pointed at the FastAPI app.

```json
{
  "title": "KiX Wave-C — ML + Viral",
  "schemaVersion": 39,
  "panels": [
    {
      "id": 1,
      "type": "stat",
      "title": "Smart Bidding CTR — Accuracy (7d)",
      "targets": [
        {
          "url": "/api/v1/observability/ml/smart_bidding_ctr/metrics?days=7",
          "jsonPath": "$.accuracy"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "percentunit",
          "thresholds": {
            "steps": [
              {"value": 0,    "color": "red"},
              {"value": 0.65, "color": "orange"},
              {"value": 0.75, "color": "green"}
            ]
          }
        }
      }
    },
    {
      "id": 2,
      "type": "stat",
      "title": "Smart Bidding CTR — AUC (7d)",
      "targets": [
        {"url": "/api/v1/observability/ml/smart_bidding_ctr/metrics?days=7", "jsonPath": "$.auc"}
      ],
      "fieldConfig": {"defaults": {"unit": "percentunit"}}
    },
    {
      "id": 3,
      "type": "stat",
      "title": "Calibration MAE (lower is better)",
      "targets": [
        {"url": "/api/v1/observability/ml/smart_bidding_ctr/metrics?days=7", "jsonPath": "$.calibration_mae"}
      ]
    },
    {
      "id": 4,
      "type": "stat",
      "title": "Distribution Drift (vs prior week)",
      "targets": [
        {"url": "/api/v1/observability/ml/smart_bidding_ctr/metrics?days=7", "jsonPath": "$.drift_score"}
      ],
      "fieldConfig": {
        "defaults": {
          "thresholds": {
            "steps": [
              {"value": 0,    "color": "green"},
              {"value": 0.25, "color": "orange"},
              {"value": 0.50, "color": "red"}
            ]
          }
        }
      }
    },
    {
      "id": 10,
      "type": "gauge",
      "title": "K-factor — last 7d (acme)",
      "targets": [
        {"url": "/api/v1/observability/viral/acme/kfactor?window_days=7", "jsonPath": "$.kfactor"}
      ],
      "fieldConfig": {
        "defaults": {
          "min": 0, "max": 2.0,
          "thresholds": {
            "steps": [
              {"value": 0,   "color": "red"},
              {"value": 0.3, "color": "orange"},
              {"value": 0.6, "color": "green"},
              {"value": 1.5, "color": "red"}
            ]
          }
        }
      }
    },
    {
      "id": 11,
      "type": "table",
      "title": "Recent anomalies",
      "targets": [
        {"url": "/api/v1/observability/anomalies?limit=20", "jsonPath": "$.anomalies"}
      ]
    },
    {
      "id": 20,
      "type": "graph",
      "title": "K-factor trailing (1d / 7d / 30d)",
      "targets": [
        {"url": "/api/v1/observability/viral/acme/kfactor", "jsonPath": "$.trailing"}
      ]
    }
  ]
}
```

> The full panel JSON is too large to inline — keep this stub in
> source control and import it into Grafana via `Dashboards → Import →
> Paste JSON`. The data-source UID slot needs to be substituted at
> import time.

### Network-graph panel (spec, not implemented)

Renders the invite tree as a force-directed graph (inviter → invitee
edges). Backed by a future read endpoint —
`/api/v1/observability/viral/{brand_id}/tree?root={user_id}&depth=5`.
**Not in scope for Wave-C; documented here so the ops team knows what
slot to leave open in the layout.**

---

## 2. Per-metric thresholds + alerts

| Metric                         | Source endpoint                                        | Warn       | Critical   | Runbook                                  |
| ------------------------------ | ------------------------------------------------------ | ---------- | ---------- | ---------------------------------------- |
| Smart Bidding accuracy (7d)    | `ml/{name}/metrics`                                    | < 0.65     | < 0.55     | `runbooks/ml-accuracy-regression.md`     |
| Smart Bidding AUC (7d)         | `ml/{name}/metrics`                                    | < 0.65     | < 0.55     | `runbooks/ml-accuracy-regression.md`     |
| Calibration MAE                | `ml/{name}/metrics`                                    | > 0.10     | > 0.20     | `runbooks/ml-calibration-drift.md`       |
| Drift score (PSI-style)        | `ml/{name}/metrics`                                    | > 0.25     | > 0.50     | `runbooks/ml-drift.md`                   |
| K-factor productive floor      | `viral/{brand}/kfactor`                                | < 0.3      | < 0.1      | `runbooks/viral-cold.md`                 |
| K-factor explosion             | `viral/{brand}/kfactor` (`explosion_warning=true`)     | K > 1.5    | K > 2.0    | `runbooks/viral-runaway.md`              |
| Observability infra Redis      | `health/observability`                                 | latency>50ms | redis=error | `runbooks/observability-redis.md`     |

### Alert plumbing

**Pagerduty** (for `critical`) — fires from Grafana when:

* `accuracy < 0.55` for 30 min, OR
* `kfactor > 2.0` for 5 min, OR
* `health/observability.redis == "error"` for 1 min.

**Slack `#kix-ops`** (for `warn`) — fires from Grafana when any "warn"
threshold above is breached for 15 min. Slack messages include a deep
link to the relevant panel and the recommended runbook.

The system also self-reports anomalies via
`GET /api/v1/observability/anomalies`. Wire that as a 1-minute Slack
poll for low-volume but high-signal callouts.

---

## 3. ML feedback loop

```
auction.predict_ctr(features) ──► track_ml_prediction(..., audit_event_id)
                                         │
                                         ▼
                              ml:predictions:{model}:{date}
                                         │
                  (when outcome known)   ▼
              attach_actual_outcome(prediction_id, actual)
                                         │
                                         ▼
                  (nightly)  run_daily_metric_job([models])
                                         │
                                         ▼
                              ml:baseline:{model}:{metric}
                                         │
                                         ▼
                  detect_anomaly() — z-score on trailing 14 d
```

**Alert if accuracy drops > 10 % from baseline**: the daily job records
yesterday's `accuracy` into the baseline hash; the next morning's run
calls `detect_anomaly("accuracy", today_value, baseline_window_days=14)`
and `severity` ∈ {`warn`, `critical`} triggers the Slack/Pagerduty hook
above.

---

## 4. Audit chain integration

Every `track_ml_prediction(...)` call accepts an `audit_event_id` that
points to a row in the durable PG audit log
(`app/services/audit_log_service.py`). To reconstruct the full chain
post-incident:

```
GET /api/v1/observability/ml/{model_name}/audit/{prediction_id}
```

returns the stored prediction record + the `audit_event_id`. The caller
then resolves the audit row via the existing audit API. This lets an
on-call engineer trace `bad bid → ML prediction → input features hash
→ audit event → caller context` in one path.

---

## 5. Onboarding for a new ops team member

1. **Bookmark** Grafana → "KiX Wave-C — ML + Viral".
2. **Confirm health** at `GET /api/v1/health/observability` — expect
   `redis: "ok"`, latency < 50 ms.
3. **Sanity-poll** `GET /api/v1/observability/dashboard-data` once a
   day; the response shape is contract-tested in
   `tests/test_observability.py::test_dashboard_shape_stable`.
4. **First Slack alert**: open the linked runbook, follow the
   "verify → contain → escalate" template.
5. **For ML regressions**: dump the prediction record for one bad
   auction via `audit/{prediction_id}`, attach to the bug report.
6. **For viral explosion**: cross-check with
   `app/routers/network_effect.py::MAX_INHERITANCE_DEPTH` — the
   inheritance-depth cap should have already throttled the loop; if K
   is still > 1.5 sustained, the cap may be bypassed (regression).

---

## 6. Endpoint cheat sheet

| Method | Path                                                                       | Purpose                                    |
| ------ | -------------------------------------------------------------------------- | ------------------------------------------ |
| GET    | `/api/v1/observability/ml/{model_name}/metrics?days=7`                     | Trailing ML metrics                        |
| POST   | `/api/v1/observability/ml/{model_name}/track`                              | Manual prediction tracking                 |
| POST   | `/api/v1/observability/ml/{model_name}/attach-actual`                      | Late-binding outcome feedback              |
| GET    | `/api/v1/observability/ml/{model_name}/audit/{prediction_id}`              | Reconstruct decision chain                 |
| GET    | `/api/v1/observability/viral/{brand_id}/kfactor?window_days=7`             | K-factor + explosion flag                  |
| POST   | `/api/v1/observability/viral/{brand_id}/event`                             | Manual viral event ingestion               |
| GET    | `/api/v1/observability/anomalies?model_name=_global`                       | Anomaly listing                            |
| GET    | `/api/v1/observability/dashboard-data`                                     | Aggregate snapshot for dashboards          |
| GET    | `/api/v1/health/observability`                                             | Infra health probe                         |
