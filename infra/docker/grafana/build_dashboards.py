"""Generates the provisioned Grafana dashboards. Run after editing: python build_dashboards.py"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}


def panel(
    title, exprs, *, x, y, w=12, h=8, unit="short", kind="timeseries", thresholds=None, ds=PROM
):
    targets = [
        {"datasource": ds, "expr": e, "legendFormat": legend, "refId": chr(65 + i)}
        for i, (e, legend) in enumerate(exprs)
    ]
    steps = [{"color": "green", "value": None}] + [
        {"color": c, "value": v} for v, c in (thresholds or [])
    ]
    p = {
        "type": kind,
        "title": title,
        "datasource": ds,
        "targets": targets,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {
            "defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}},
            "overrides": [],
        },
        "options": {},
    }
    if kind == "timeseries" and thresholds:
        p["fieldConfig"]["defaults"]["custom"] = {"thresholdsStyle": {"mode": "line+area"}}
    return p


def dashboard(uid, title, panels):
    return {
        "uid": uid,
        "title": title,
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "time": {"from": "now-1h", "to": "now"},
        "refresh": "10s",
        "tags": ["amrutam"],
        "panels": [dict(p, id=i + 1) for i, p in enumerate(panels)],
    }


api = dashboard(
    "amrutam-api",
    "Amrutam API - SLOs & traffic",
    [
        panel(
            "Availability (30d) - SLO 99.95%",
            [
                (
                    '1 - (sum(increase(http_requests_total{status=~"5.."}[30d])) / sum(increase(http_requests_total[30d])))',
                    "availability",
                )
            ],
            x=0,
            y=0,
            w=6,
            h=5,
            unit="percentunit",
            kind="stat",
            thresholds=[(0, "red"), (0.9995, "green")],
        ),
        panel(
            "Read p95 (SLO 200ms)",
            [("slo:read_latency_p95:5m", "GET p95")],
            x=6,
            y=0,
            w=6,
            h=5,
            unit="s",
            kind="stat",
            thresholds=[(0.15, "orange"), (0.2, "red")],
        ),
        panel(
            "Write p95 (SLO 500ms)",
            [("slo:write_latency_p95:5m", "write p95")],
            x=12,
            y=0,
            w=6,
            h=5,
            unit="s",
            kind="stat",
            thresholds=[(0.4, "orange"), (0.5, "red")],
        ),
        panel(
            "5xx ratio (5m)",
            [("slo:http_error_ratio:rate5m", "5xx")],
            x=18,
            y=0,
            w=6,
            h=5,
            unit="percentunit",
            kind="stat",
            thresholds=[(0.0005, "orange"), (0.007, "red")],
        ),
        panel(
            "Requests/s by route",
            [("sum by (handler) (rate(http_requests_total[1m]))", "{{handler}}")],
            x=0,
            y=5,
            unit="reqps",
        ),
        panel(
            "Responses/s by status",
            [("sum by (status) (rate(http_requests_total[1m]))", "{{status}}")],
            x=12,
            y=5,
            unit="reqps",
        ),
        panel(
            "p95 latency - reads by route",
            [
                (
                    'histogram_quantile(0.95, sum by (le, handler) (rate(http_request_duration_seconds_bucket{method="GET"}[5m])))',
                    "{{handler}}",
                )
            ],
            x=0,
            y=13,
            unit="s",
            thresholds=[(0.2, "red")],
        ),
        panel(
            "p95 latency - writes by route",
            [
                (
                    'histogram_quantile(0.95, sum by (le, handler) (rate(http_request_duration_seconds_bucket{method=~"POST|PUT|PATCH|DELETE"}[5m])))',
                    "{{handler}}",
                )
            ],
            x=12,
            y=13,
            unit="s",
            thresholds=[(0.5, "red")],
        ),
        panel(
            "Rate limited (429) & auth failures (401)",
            [
                (
                    'sum by (handler) (rate(http_requests_total{status="429"}[5m]))',
                    "429 {{handler}}",
                ),
                (
                    'sum by (handler) (rate(http_requests_total{status="401"}[5m]))',
                    "401 {{handler}}",
                ),
            ],
            x=0,
            y=21,
            unit="reqps",
        ),
        panel(
            "Doctor search cache hit ratio",
            [
                (
                    'sum(rate(cache_requests_total{result="hit"}[5m])) / sum(rate(cache_requests_total[5m]))',
                    "hit ratio",
                )
            ],
            x=12,
            y=21,
            unit="percentunit",
        ),
        panel(
            "Error logs (API + worker)",
            [('{service=~"api|worker"} | json | level=~"error|warning"', "")],
            x=0,
            y=29,
            w=24,
            h=9,
            kind="logs",
            ds=LOKI,
        ),
    ],
)

business = dashboard(
    "amrutam-business",
    "Amrutam - bookings, payments & jobs",
    [
        panel(
            "Bookings by outcome",
            [("sum by (outcome) (rate(bookings_total[5m]) * 60)", "{{outcome}}")],
            x=0,
            y=0,
            unit="short",
        ),
        panel(
            "Payment webhooks by outcome",
            [("sum by (outcome) (rate(payment_webhooks_total[5m]) * 60)", "{{outcome}}")],
            x=12,
            y=0,
            unit="short",
        ),
        panel(
            "Outbox deliveries",
            [("sum by (result) (rate(outbox_events_total[5m]) * 60)", "{{result}}")],
            x=0,
            y=8,
            unit="short",
        ),
        panel(
            "Dead-lettered events (1h)",
            [('sum(increase(outbox_events_total{result="dead"}[1h]))', "dead")],
            x=12,
            y=8,
            w=6,
            unit="short",
            kind="stat",
            thresholds=[(1, "red")],
        ),
        panel(
            "Payment circuit breaker",
            [("max(circuit_breaker_open)", "open")],
            x=18,
            y=8,
            w=6,
            unit="short",
            kind="stat",
            thresholds=[(1, "red")],
        ),
        panel(
            "Unpaid holds released / min",
            [("rate(slot_holds_released_total[5m]) * 60", "released")],
            x=0,
            y=16,
            unit="short",
        ),
        panel(
            "Cancellations by actor / min",
            [("sum by (actor) (rate(cancellations_total[5m]) * 60)", "{{actor}}")],
            x=12,
            y=16,
            unit="short",
        ),
        panel(
            "Notifications sent / min",
            [("sum by (template) (rate(notifications_sent_total[5m]) * 60)", "{{template}}")],
            x=0,
            y=24,
            unit="short",
        ),
        panel(
            "External call retries / min",
            [("sum by (dependency) (rate(dependency_retries_total[5m]) * 60)", "{{dependency}}")],
            x=12,
            y=24,
            unit="short",
        ),
    ],
)

OUT.mkdir(exist_ok=True)
for d in (api, business):
    (OUT / f"{d['uid']}.json").write_text(json.dumps(d, indent=2) + "\n")
    print("wrote", d["uid"])
