---
name: amrutam-backend
description: Conventions for changing the Amrutam telemedicine backend — module layering, dependency injection, transactions with audit + outbox, idempotency, the booking saga, PHI encryption, migrations and observability. Use before adding or changing any endpoint, service, model, migration or worker job in this repo, or when the user mentions booking, slots, payments, prescriptions, consultations, audit, outbox, idempotency or encryption.
---

# Amrutam backend conventions

Read `AGENTS.md` first, then `docs/architecture.md` for the why. This skill is the how.

## 1. Where code goes

Each feature is a module under `app/modules/<name>/`:

| File | Owns | Must not |
|---|---|---|
| `router.py` | HTTP shape, auth dependencies, status codes, DI wiring (`get_*_service`) | contain business rules or SQL |
| `schemas.py` | Pydantic request/response models (`StrictModel` = `extra="forbid"`) | return ORM objects directly |
| `service.py` | business rules, transactions, audit, outbox | import FastAPI or parse requests |
| `repository.py` | query construction | commit transactions |
| `models.py` | SQLAlchemy tables (register new models in `app/models.py`) | hold behaviour beyond tiny properties |

Cross-cutting code lives in `app/core/` (config, db, crypto, security, cache, resilience,
errors, logging, observability, deps) and `app/middleware/`. Modules talk to each other
through services/repositories, never through routers.

**Dependency injection:** services take collaborators in `__init__` (session, cipher,
gateway, cache, signer, settings). Routers build them with `Depends(...)`. Never import a
global client inside a service; that is what makes services testable with a real DB and a
fake gateway.

## 2. Writes: one transaction = change + audit + outbox

Every state change follows this shape:

```python
row = await session.scalar(select(X).where(...).with_for_update())  # lock what you change
...mutate...
emit(session, "x.changed", row.id, {"x_id": str(row.id)})          # ids only, never PHI
await AuditService(session).record("x.changed", ctx=ctx, actor_id=..., resource_type="x",
                                   resource_id=row.id, metadata={...})  # PHI-free metadata
await session.commit()
```

- Audit **last** before commit: it locks the chain head until commit.
- Side effects (notifications, refunds, external calls that can wait) go through the
  outbox (`app/modules/outbox/service.py::emit`) and a handler in `app/worker/jobs.py`.
  Handlers must be idempotent: delivery is at-least-once.
- External calls that must happen inline go through an adapter with
  `retry_async` + `CircuitBreaker` (`app/core/resilience.py`) and an idempotency key.

## 3. Concurrency: let Postgres enforce invariants

Prefer a constraint or an atomic conditional statement over "check then write":

- compare-and-set: `UPDATE … WHERE status = 'open' RETURNING …` (slot holds)
- partial unique indexes (one live consultation per slot)
- `EXCLUDE USING gist` for time overlaps (doctor slots, patient consultations)
- `INSERT … ON CONFLICT` for dedupe (idempotency keys, webhook inbox, slot publishing)
- `FOR UPDATE SKIP LOCKED` + lease for anything a worker claims

Map `IntegrityError` from these constraints to a precise 409 `code` (see
`ConsultationService._flush_booking`). Add a concurrency test that fires parallel requests.

## 4. Idempotency

- Every POST/PUT/PATCH/DELETE honours `Idempotency-Key` automatically (middleware).
- Add `Depends(require_idempotency_key)` when a duplicate would be harmful (money, bookings,
  prescriptions, anything user-visible that must happen once).
- Token-issuing endpoints are excluded from response replay (`IDEMPOTENCY_EXCLUDED` in
  `app/main.py`): never cache credentials.

## 5. The booking saga (`app/modules/consultations/saga.py`)

Hold → intent → webhook confirm, with compensations (hold expiry sweeper, refund on
late/mismatched payment). Reuse the saga helpers (`transition`, `confirm_booking`,
`cancel_booking`, `request_refund`, `close_unpaid`) instead of mutating statuses directly.
Consultation statuses only move along `TRANSITIONS`; an illegal move is a 409.

## 6. Data protection

- PHI/PII columns are `*_enc` and go through `FieldCipher.encrypt/decrypt` with an AAD of
  `"<table>.<column>:<row id>"`. Lookups on encrypted values use a blind index (`*_bidx`).
- Never log, audit, emit or return PHI you don't need. Logs are redacted by key name
  (`app/core/logging.py::SENSITIVE_KEYS`): add new sensitive field names there.
- Object-level authorization on every load: non-participants get **404**, staff access to
  PHI is audited (`*.viewed_by_staff`).

## 7. Migrations (`alembic/versions/`)

- Hand-written, numbered (`0006_<slug>.py`), additive first (expand → migrate → contract).
- asyncpg runs **one statement per `op.execute`**: split raw DDL into a list.
- New constraint/index for every new query path; partial indexes for hot subsets.
- Big tables: think about partitioning (see `audit_logs`) and a backfill plan.

## 8. Errors, config, observability

- Raise `AppError` subclasses (`BadRequest`, `NotFound`, `Conflict`, `Forbidden`, …) with a
  stable `code`; the handler renders RFC 9457 problem+json with the request id.
- New settings go in `app/core/config.py` (validated at startup) and `.env.example`;
  secrets have no defaults and are also added to `scripts/generate_env.py` and
  `infra/terraform/secrets.tf`.
- New failure modes get a Prometheus counter and, if someone should act, an alert in
  `infra/docker/prometheus/alerts.yml`.
- After any route/schema change run `make openapi` and commit `docs/openapi.yaml`.

## Done checklist for a backend change

- [ ] Layering respected; services injected, no FastAPI imports in services
- [ ] Change + audit + outbox in one transaction; handlers idempotent
- [ ] Invariant enforced by a DB constraint or atomic statement, with a concurrency test
- [ ] Idempotency-Key required where duplicates hurt
- [ ] PHI encrypted, not logged, not in events; object-level authz + 404
- [ ] Migration additive and split per statement; indexes for new queries
- [ ] Metrics/alerts for new failure modes; `docs/openapi.yaml` regenerated
- [ ] `amrutam-testing` and `amrutam-security-review` gates passed
