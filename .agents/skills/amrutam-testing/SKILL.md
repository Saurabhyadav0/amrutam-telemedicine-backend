---
name: amrutam-testing
description: How to test changes in the Amrutam telemedicine backend — unit vs integration tests against real Postgres/Redis, test helpers, concurrency and idempotency tests, worker jobs, the end-to-end smoke test and k6 load tests. Use when writing or fixing tests, before calling a feature done, or when the user says "test", "coverage", "smoke", "load test" or "is this tested".
---

# Testing the Amrutam backend

## Commands

| Command | What it proves | When |
|---|---|---|
| `make test` | 110+ unit + integration tests, coverage gate 80% | every change |
| `make lint typecheck security` | ruff, mypy (strict defs), bandit + pip-audit | every change |
| `uv run python scripts/export_openapi.py --check` | API schema committed matches code | any route/schema change |
| `make up && make smoke` | 42 end-to-end checks through the gateway, worker and observability stack | flows, infra, Docker or config changes |
| `make seed-load && make load` | k6 peak mix with SLO thresholds | query/caching/hot-path changes (run on quiet hardware) |
| `make tf-check` | terraform fmt/validate + Checkov | any `infra/terraform` change |

## Where tests go

- `tests/unit/`: pure logic, no I/O (crypto, tokens, slot expansion, retry/breaker,
  webhook signatures). Fast and deterministic.
- `tests/integration/`: through the real app with `httpx.AsyncClient` against **real
  Postgres + Redis** (testcontainers locally, CI service containers via `TEST_DATABASE_URL`
  / `TEST_REDIS_URL`). Never mock the database: constraints, triggers and partitions are
  part of the behaviour.

## Helpers (`tests/integration/helpers.py`)

`register`, `login`, `enrol_mfa`, `admin_session`, `doctor_session`, `verified_doctor`,
`open_slot`, `book`, `pay`, `confirmed_consultation`, `patient_token`, `idem()`,
`bearer()`, `medicine_id(name)`, `unique_token()`. Use `unique_token()` in names/queries so
tests stay isolated in a shared database; never truncate tables (the audit log is
append-only by design).

The `worker` fixture (`tests/integration/conftest.py`) gives `WorkerDeps` with a
`RecordingNotifier`; call job functions directly (`dispatch_outbox`,
`release_expired_holds`, `refresh_analytics`) and pass `now=` to move time instead of
sleeping.

## What every feature needs

- **Happy path and failure paths:** validation 422, authz 403/404, conflict 409, not found.
- **Object-level authz:** a second user of the same role gets 404.
- **Idempotency:** same key replays (`Idempotent-Replayed: true`); different body → 422.
- **Concurrency** for anything with an invariant: `asyncio.gather` N parallel requests and
  assert exactly one wins *and* the DB holds one row.
- **Audit:** the action appears in `/v1/admin/audit-logs` and the chain still verifies.
- **No PHI leakage:** errors don't echo input; exports/logs don't contain what they shouldn't.
- **Time:** never `sleep`. Use slots a few minutes ahead, `now=` on jobs, or update rows
  directly in the test DB for states far in the future.

## Rules

- Deterministic: no wall-clock races, no network beyond the test containers, no ordering
  dependence between tests.
- A bug fix starts with a failing regression test at the seam where the bug occurs (see
  `diagnose`).
- Don't weaken a threshold or assertion to get green; find out why it fails.
