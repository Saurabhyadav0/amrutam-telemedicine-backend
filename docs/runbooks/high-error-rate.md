# Runbook: AvailabilityBudgetFastBurn / SlowBurn

**Meaning:** the 5xx ratio is consuming the 99.95% monthly error budget too fast
(fast burn = 2% of the budget per hour).

1. **Scope it.** Grafana → *Amrutam API - SLOs & traffic* → "Responses/s by status" and
   "Requests/s by route". One route or all? Since a deploy (check the image tag)?
2. **Read the errors.** Same dashboard, "Error logs" panel (Loki), or
   `{service="api"} | json | level="error"`. Every line has `request_id` and `trace_id`;
   click the trace id to open the request in Jaeger.
3. **Check dependencies.** `GET /health/ready` per replica; Postgres connections
   (`pg_stat_activity`), Redis latency, `circuit_breaker_open` for the payment gateway.
4. **Mitigate first.** Roll back the last deploy if it correlates; scale API replicas if
   `event_loop_lag_seconds` p99 is high across replicas (saturation); fail over Postgres per
   `docs/runbooks/dr.md` if the primary is unhealthy.
5. **Afterwards:** blameless post-incident note, and a regression test for the cause.
