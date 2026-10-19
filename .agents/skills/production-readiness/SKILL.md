---
name: production-readiness
description: Make code production-grade before it ships — error handling, observability, configuration, resilience, performance, and rollout safety. Use before merging or deploying a feature, when the user says "production", "ship it", "is this ready", "deploy", "harden", "make it robust", or when reviewing code that will run unattended against real traffic or real data.
---

> Adapted from [devnouiq/engineering-standards-skills](https://github.com/devnouiq/engineering-standards-skills) (MIT, © 2026 Ujjwal Garg). See `.agents/skills/THIRD_PARTY_NOTICES.md`.

# Production Readiness

Prototype code assumes the happy path. Production code assumes the network is down, the
payload is malformed, the disk is full, and someone is watching the dashboard at 3am.

Run this as a gate before a feature is considered done.

## 1. Error handling

- **Fail loudly at boundaries, gracefully in the middle.** Validate input at the edge and
  reject with a precise error; do not let bad data propagate inward.
- No bare `except:` / `catch (e) {}` that swallows. Catch the *specific* exception you can
  actually handle.
- Never log-and-continue on a failure that leaves state inconsistent — roll back or abort.
- Error messages: actionable for the operator, non-leaky for the user. No stack traces,
  SQL, or internal paths in a client response.
- Distinguish **retryable** (timeout, 503, deadlock) from **terminal** (400, validation)
  failures and treat them differently.
- Clean up resources deterministically — context managers, `defer`, `finally`.

## 2. Observability

You cannot operate what you cannot see.

- **Structured logs**, not string concatenation. Include a correlation/request id on every
  line so one request can be traced end to end.
- Log levels used correctly: `ERROR` = someone must act; `WARN` = degraded but handled;
  `INFO` = business milestone; `DEBUG` = off in prod.
- **Never log secrets or PII.** Tokens, passwords, keys, card numbers, full request bodies
  — redact at the logger, not at the call site.
- Emit metrics for the things you would page on: request rate, error rate, latency
  percentiles (p50/p95/p99), queue depth, job success/failure counts.
- Trace across service hops if the request crosses a boundary.
- A **health/readiness endpoint** that actually checks dependencies, not one that returns
  `200` unconditionally.

## 3. Configuration

- Config comes from the environment; **nothing environment-specific is hardcoded**.
- Secrets come from a secret store or injected env — never from source, never from a
  committed `.env`.
- Validate config **at startup** and fail fast with a clear message. Do not discover a
  missing variable on the first request at midnight.
- Sensible defaults for optional settings; no defaults for secrets.
- Feature flags for anything risky, so rollback does not require a deploy.

## 4. Resilience

- **Timeouts on every** network call, DB query, and lock acquisition. An unbounded wait is
  an outage.
- Retries with exponential backoff **and jitter**, with a maximum attempt count. Only
  retry idempotent operations.
- Circuit breaker or bulkhead around a dependency that can go down without taking you
  with it.
- Graceful degradation: if the recommendation service is down, serve the page without
  recommendations — do not 500.
- Graceful shutdown: drain in-flight work on SIGTERM, stop accepting new work, then exit.
- Concurrency safety: no data races, no unguarded shared mutable state, explicit
  transaction boundaries and isolation levels.

## 5. Data

- Migrations are **backward compatible** — deploy the schema change before the code that
  needs it (expand → migrate → contract).
- Every migration has a tested rollback path, or is provably additive.
- No destructive operation without an explicit, reviewed step.
- Backups and retention verified for anything you cannot recompute.

## 6. Performance

- Know the expected volume before choosing the algorithm. Do not optimise blind; do not
  ship an O(n²) loop over an unbounded collection either.
- Bound every query; paginate every list endpoint.
- Eliminate N+1 access patterns at the boundary.
- Cache with an explicit TTL and an invalidation story. A cache without invalidation is a
  bug with a delay.
- Move work the user is not waiting for off the request path.

## 7. Rollout safety

- The change is behind a flag, or is small enough to revert cleanly.
- Deploy is reversible: previous version can run against the current schema.
- Alerting exists for the new failure modes this change introduces.
- Runbook note for anything an on-call engineer would not guess.

## 8. Tests that matter

- Cover the failure paths, not just the happy path: timeout, malformed input, empty
  result, permission denied, duplicate submission.
- At least one test proves the retry/idempotency behaviour if the code claims it.
- Tests are deterministic — no reliance on wall-clock timing, network, or ordering.

## Gate

Do not call the feature done until each item below is true or explicitly waived with a
reason:

- [ ] Failure paths handled; no silent swallowing
- [ ] Structured logs with correlation id; no secrets or PII logged
- [ ] Metrics/alerts exist for the new failure modes
- [ ] Config validated at startup; secrets injected, not committed
- [ ] Timeouts + bounded retries on every external call
- [ ] Queries bounded and indexed; no N+1
- [ ] Migration is backward compatible and reversible
- [ ] Graceful shutdown and idempotent retries where applicable
- [ ] Rollback path is real and has been reasoned through
