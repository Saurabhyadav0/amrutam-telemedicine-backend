# Amrutam Telemedicine Backend

Production-grade backend for Amrutam's Ayurvedic teleconsultation platform: identity & RBAC,
doctor availability and booking, consultations, prescriptions, payments, audit and analytics.

**Stack:** Python 3.12 · FastAPI · PostgreSQL 16 · Redis 7 · SQLAlchemy 2 (async) · Alembic ·
OpenTelemetry · Prometheus · Grafana · Jaeger · Docker · GitHub Actions

## Deliverables

| Brief item | Where |
|---|---|
| 1. Code + infrastructure as code | `app/`, `alembic/`, `infra/terraform/` (AWS: VPC, ECS Fargate, RDS Multi-AZ + replica, ElastiCache, ALB + WAF, KMS, Secrets Manager, AWS Backup), `docker-compose.yml` |
| 2. README with setup | this file |
| 3. API schema | [`docs/openapi.yaml`](docs/openapi.yaml) (OpenAPI 3.1, generated, drift-checked in CI) |
| 4. Architecture doc | [`docs/architecture.md`](docs/architecture.md): architecture + data flow, booking sequence, ER diagram, retries, partitioning, caching/concurrency, sagas, backup/DR, capacity |
| 5. Tests + CI pipeline | `tests/` (110 tests, real Postgres/Redis, 94% coverage), `tests/load/` (k6), `.github/workflows/ci.yml`, CD in `.github/workflows/deploy.yml` |
| 6. Observability | Prometheus metrics + 17 alert rules, Grafana dashboards, Loki logs, OpenTelemetry traces (`infra/docker/`) |
| 7. Security checklist + threat model | [`docs/security.md`](docs/security.md), [`docs/compliance.md`](docs/compliance.md) (TPG 2020, DPDP) |
| Runbooks / demo | [`docs/runbooks/`](docs/runbooks/) (DR, high error rate), [`docs/demo-script.md`](docs/demo-script.md) |
| Agent guide + skills | [`AGENTS.md`](AGENTS.md) and [`.agents/skills/`](.agents/skills/): engineering workflow plus repo-specific backend, testing and security-review skills |
| Planning notes | [`docs/planning-notes.md`](docs/planning-notes.md): domain research, compliance findings, initial design and plan |

## Quick start

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/).

```bash
make env        # writes .env with freshly generated secrets (never committed)
make up         # postgres, redis, migrations, api, jaeger, prometheus, grafana
curl localhost:8000/health/ready
make smoke      # 42 end-to-end checks: auth/MFA, booking saga, Rx, worker, DPDP, observability
```

The stack runs two app processes from one image: `api` (FastAPI, 4 replicas behind an nginx
`gateway`, like pods behind a cloud load balancer) and `worker` (arq: outbox delivery, hold
expiry, refunds, analytics rollups, housekeeping).

| Service | URL |
|---|---|
| API + Swagger UI (via gateway) | http://localhost:8000/docs |
| OpenAPI schema | http://localhost:8000/openapi.json |
| Prometheus metrics | http://localhost:8000/metrics |
| Jaeger (traces) | http://localhost:16686 |
| Prometheus | http://localhost:9090 |
| Grafana (dashboards: *Amrutam* folder) | http://localhost:3300 |
| Loki (logs, via Grafana Explore) | http://localhost:3100 |
| Prometheus alerts | http://localhost:9090/alerts |

Create the first admin (admins can't self-register):

```bash
docker compose exec api python -m app.cli create-admin --email admin@example.com
```

### Develop on the host

```bash
uv sync
docker compose up -d postgres redis
make migrate
make run        # uvicorn with reload on :8000
```

### Load testing

```bash
make seed-load   # once: 5,000 doctors / 300,000 slots into the compose database
make load        # k6 peak-hour mix: 150 reads/s + 15 bookings/s (each = book + pay), 60s
READ_RPS=60 BOOK_RPS=6 DURATION=60s make load
```

The k6 script (`tests/load/booking_mix.js`) enforces the SLOs as thresholds (p95 reads
< 200 ms, writes < 500 ms, < 0.5% errors) and counts 409 (slot taken) as correct behaviour.

**Results on the development laptop** (Apple M3 Pro; k6, 4 API replicas, Postgres, Redis,
Jaeger, Loki and Grafana all sharing one Docker VM):

| Load | p50 read / write | p95 read / write | Errors |
|---|---|---|---|
| 60 reads/s + 6 bookings/s, cool machine | 7 ms / 24 ms | **94 ms / 187 ms** (pass) | 0% |
| Same load, after sustained runs (thermal throttling) | 15 ms / 62 ms | 467 ms / 664 ms (fail) | 0% |
| 150 reads/s + 15 bookings/s (target peak) | 7–12 ms / 30–50 ms | 0.36–1 s / 0.65–2 s (fail) | 0% |

Medians stay low; the tail is CPU scheduling on a shared, throttled laptop. During the
target-peak runs macOS `kernel_task` (thermal management) used 140% CPU, and even a trivial
`/health/live` showed a 40–90 ms p95 against a 1 ms median. **These numbers do not certify the
SLOs**; run `make load` in a staging environment with dedicated nodes to do that. What the
load tests did find and fix:

1. A single API process saturated one core (105% CPU, 3,000+ dropped iterations): the stack
   now runs 4 replicas behind a gateway, with Prometheus discovering each replica.
2. Search computed "next available slot" with a correlated subquery for every candidate doctor
   before the top-N sort (**69 ms** per query at 5k doctors): now a denormalised, indexed
   column kept fresh by the worker (**0.4 ms**).
3. The default per-route latency buckets (0.1/0.5/1 s) couldn't measure a 200 ms SLO: added
   SLO-aligned buckets.
4. No metric showed event-loop saturation: added `event_loop_lag_seconds`.
5. Fixed-size DB pools (no overflow) avoid paying connect cost during bursts.

### Checks

```bash
make test       # pytest with real Postgres + Redis (testcontainers), coverage gate 80%
make lint       # ruff lint + format check
make typecheck  # mypy
make security   # bandit (SAST) + pip-audit (dependency CVEs)
make openapi    # regenerate docs/openapi.yaml (CI fails if it's stale)
make tf-check   # terraform fmt/validate + checkov, in Docker
```

Tests start throwaway Postgres/Redis containers automatically. To use existing ones, set
`TEST_DATABASE_URL` and `TEST_REDIS_URL` (CI does this with service containers).

## What's implemented

### Auth, MFA, RBAC
- Register (`patient` / `doctor` only; admin/support are assigned by an admin) with explicit
  DPDP data-processing consent stored in an append-only `consents` ledger.
- Passwords: Argon2id, hashed off the event loop; timing-equalised for unknown emails;
  account lockout after 5 failures.
- JWT access tokens (15 min) signed with a **key ring** (`kid` header) so keys rotate without
  logging anyone out. Opaque **rotating refresh tokens** (hash-only storage); reusing a rotated
  token revokes the whole token family.
- **TOTP MFA** with replay protection (a code's time-step can't be reused) and single-use
  recovery codes. **Mandatory for doctors, admins and support**: until enrolled, their tokens
  can only reach MFA enrolment endpoints.
- RBAC guard reads the role from the DB on every request, so role changes and account
  disabling apply immediately.

### Data protection
- PHI/PII (phone, date of birth, TOTP secrets) encrypted at the application layer with
  **AES-256-GCM**, versioned keys for rotation, and associated data binding each ciphertext
  to its row. Phone uniqueness/lookup via an HMAC **blind index**.
- Logs redact credentials and personal fields; validation errors never echo input.

### Idempotency
- Every POST/PUT/PATCH/DELETE honours `Idempotency-Key`; routes where duplicates are harmful
  **require** it. Same key + same request replays the stored response
  (`Idempotent-Replayed: true`); same key + different body returns 422; concurrent duplicates
  run once (409 for the loser). Keys are scoped per user.

### Audit trail
- `audit_logs` is **range-partitioned by month**, **append-only** (a DB trigger blocks
  UPDATE/DELETE) and **hash-chained**. `GET /v1/admin/audit-logs/verify` re-checks the chain.
- Audit rows are written in the same transaction as the change they describe.

### Doctors & availability
- Doctors submit a practice profile (registration number + council, qualification,
  specialties, languages, fee). Admins verify it against the NCISM / State Board register;
  only **verified** doctors can publish slots or appear in search. Editing credentials on a
  verified profile sends it back for re-verification. Registration numbers are unique.
- Patients see the registration number and qualification, as the Telemedicine Practice
  Guidelines 2020 require. An availability toggle hides a doctor from search instantly.
- Slots are published as explicit windows or a weekly rule in the doctor's timezone
  (DST-safe), up to 90 days ahead, 500 per request. A **Postgres exclusion constraint** makes
  overlapping slots for one doctor impossible, even under concurrent requests; overlaps in a
  batch are skipped, so re-publishing a week is a no-op.
- Only `open` slots can be deleted; held/booked slots must go through cancellation.

### Search & caching
- Filters: prefix full-text on name/specialty/qualification/bio, specialty, language, fee
  range, rating, availability window; sorts by relevance, rating, fee, experience, next
  available. Served from the read replica.
- Partial GIN/btree indexes cover only bookable doctors; "next available" is an index-only
  scan on a partial index of open slots.
- Redis cache-aside with **generation-based invalidation** (one `INCR` invalidates the
  namespace; no key scans). Search results live 30s, profiles 60s; open-slot lists and booking
  always read the database. Hit/miss counters exported to Prometheus.
- Measured locally with 5,000 doctors / 300,000 slots (`scripts/seed_search_load.sql`):
  search **p95 36 ms uncached, 8.6 ms cached** (target: p95 < 200 ms).

### Booking saga & payments
```
book ─► HOLD slot (atomic compare-and-set) + consultation PENDING_PAYMENT + payment   [1 tx]
     ─► gateway.create_intent  (retry w/ full-jitter backoff + circuit breaker)      [external]
webhook payment.succeeded ─► CONFIRM: consultation CONFIRMED, slot BOOKED            [1 tx]
           └─ hold already expired ─► compensate: cancel + refund                     [1 tx]
webhook payment.failed / hold expiry (worker) ─► cancel, expire payment, reopen slot [1 tx]
refund requested ─► worker: gateway.refund (idempotent, retried, dead-lettered)       [async]
```
- **Double booking is impossible at three levels:** the slot hold is a conditional `UPDATE`
  (one winner under concurrency, tested with 8 parallel patients), a partial unique index
  allows one live consultation per slot, and an exclusion constraint stops a patient holding
  two overlapping consultations.
- Booking needs `Idempotency-Key`; a retry with a new key for the same slot **resumes** the
  existing booking instead of failing.
- Webhooks are HMAC-SHA256 signed with a timestamp (5-minute replay window) and deduplicated
  through a `payment_events` inbox; redeliveries are no-ops.
- A payment that lands after the hold expired, or with the wrong amount, is refunded
  automatically. Cancellation policy: patients get a full refund up to 2h before the start;
  doctor/admin cancellations are always refunded. Free consultations confirm immediately.
- Explicit **teleconsultation consent** (TPG 2020) is recorded on first booking.

### Consultations
- State machine: `pending_payment → confirmed → in_progress → completed`, plus `cancelled`
  and `no_show`; illegal transitions return 409. The doctor starts within 15 minutes of the
  slot; both participants get short-lived per-user room tokens for the video/chat provider.
- Object-level authorization: non-participants get 404 (no existence leak); admin/support
  access to consultations and prescriptions is always audited.
- Chief complaint and doctor notes are encrypted. Reviews update the doctor's rating with one
  atomic `UPDATE` (no lost votes) and refresh search.

### Prescriptions
- TPG 2020 medicine lists enforced server-side: **List B** (e.g. Schedule E(1) drugs) is never
  allowed via telemedicine; **List A** needs a video consult the first time, then refills may
  be audio/chat; **List O** is always allowed. Starter Ayurvedic catalogue is seeded.
- Each prescription is signed with **Ed25519** over a canonical JSON document carrying the
  TPG-required fields (doctor registration, patient, date, Rx, advice). The public key is at
  `GET /v1/prescriptions/signing-key` so pharmacies can verify offline. The signed document is
  stored encrypted and the table is append-only at the DB level.
- Items that map to Amrutam products come back as `product_recommendations`.

### Async jobs (worker)
- **Transactional outbox:** events are written in the same transaction as the change and
  delivered at-least-once. Workers claim rows with `FOR UPDATE SKIP LOCKED` + a lease, so
  replicas never double-process. Failures back off exponentially with jitter; after 8 attempts
  an event is **dead-lettered** (`GET /v1/admin/outbox`, retry with
  `POST /v1/admin/outbox/{id}/retry`).
- Jobs: notifications, refunds, hold-expiry sweeper (every 30s), idempotency-key purge,
  monthly audit partition creation. Worker metrics on `:9100`.

### Admin analytics
- `GET /v1/admin/analytics/overview|daily|doctors|specialties` (admin + MFA): bookings,
  completion/cancellation/no-show rates, gross/net revenue and refunds, new patients/doctors,
  average rating, per-doctor utilisation (booked/published slots), demand by specialty.
- Served from small **daily rollup tables** (IST business days), not by scanning bookings.
  The worker recomputes today/yesterday every 10 minutes with idempotent set-based upserts;
  `POST /v1/admin/analytics/refresh` backfills any range asynchronously via the outbox.
  Responses include `as_of`. Only aggregates are exposed: no PHI.

### Observability
- **Metrics (Prometheus):** per-route latency histograms with bucket edges at the SLO
  thresholds, request/error rates, business counters (`bookings_total`,
  `payment_webhooks_total`, `outbox_events_total`, `slot_holds_released_total`,
  `cancellations_total`, `notifications_sent_total`), `cache_requests_total`,
  `circuit_breaker_open`, `dependency_retries_total`, and `event_loop_lag_seconds`
  (saturation signal for async Python). Worker metrics on `:9100`.
- **Alerts** (`infra/docker/prometheus/alerts.yml`, 17 rules): multi-window **error-budget
  burn rates** for 99.95% availability, p95 read/write latency SLOs, API/worker down,
  dead-lettered events, payment circuit open, webhook signature failures and login-failure
  spikes (security), checkout abandonment.
- **Dashboards (Grafana, provisioned):** *API - SLOs & traffic* and *Bookings, payments &
  jobs*. Generated from `infra/docker/grafana/build_dashboards.py`.
- **Logs (Loki via Grafana Alloy):** JSON logs from every container, labelled by service
  and level. Clicking a `trace_id` in a log line opens the trace in Jaeger, and Jaeger links
  back to the trace's logs.
- **Traces (OpenTelemetry → Jaeger):** HTTP → SQL → Redis spans; 10% parent-based sampling.

### Privacy (DPDP Act 2023)
- `GET /v1/users/me/data-export` returns everything held about the caller (decrypted profile,
  consents, consultations, signed prescriptions).
- `POST /v1/users/me/erasure` (password step-up, explicit confirmation) anonymises identity
  and contact data, revokes every session and records consent withdrawal, while **retaining
  clinical records** as medical-records rules require. Blocked while consultations are
  active; practitioner and staff accounts close through support. Both actions are audited.

### Infrastructure (Terraform, `infra/terraform/`)
- AWS **ap-south-1 (Mumbai)** with DR copies in **ap-south-2 (Hyderabad)**, keeping data in
  India. VPC across 3 AZs with public / private / isolated data subnets and flow logs.
- **ECS Fargate (Graviton)**: API service autoscaled on CPU and requests per target, with a
  deployment circuit breaker that rolls back; worker service; one-off migration task.
  Non-root, read-only containers; immutable ECR tags with scan-on-push.
- **RDS Postgres 16 Multi-AZ** + read replica, KMS encryption, forced TLS, IAM auth, 30-day
  PITR, cross-region backup replication; **ElastiCache Redis** Multi-AZ with TLS + AUTH.
- **ALB** (TLS 1.3 policy, HTTP→HTTPS, internal paths blocked) behind **WAF** (AWS managed
  OWASP/SQLi/bad-input/IP-reputation rules + rate rule, logged with Authorization redacted).
- Per-domain **KMS CMKs** with yearly rotation; **Secrets Manager** for app secrets;
  **AWS Backup** with a compliance-mode vault lock (daily ×35 d, monthly ×7 y).
- `make tf-check` runs `fmt`, `validate` and **Checkov**: 205 passed, 0 failed, and each of
  the 20 skips has its reason written inline.
- CD (`.github/workflows/deploy.yml`): GitHub OIDC → AWS (no stored keys), build an immutable
  arm64 image, run migrations as a one-off task, roll out, smoke test. Staging is automatic
  after CI; production is the same image, promoted with required reviewers.

### Working with AI coding agents
[`AGENTS.md`](AGENTS.md) gives any coding agent the commands, hard rules and domain
glossary for this repo. [`.agents/skills/`](.agents/skills/) holds task skills that agents
load on demand: three written for this codebase (`amrutam-backend`, `amrutam-testing`,
`amrutam-security-review`) and general engineering skills (`engineering-workflow`,
`solid-engineering`, `secure-by-default`, `production-readiness`, `subagent-first`,
`diagnose`, `zoom-out`) adapted from MIT-licensed open-source collections (attribution in
`.agents/skills/THIRD_PARTY_NOTICES.md`).

### Platform
- RFC 9457 `application/problem+json` errors with `request_id` everywhere.
- Security headers (HSTS, CSP, nosniff, `Cache-Control: no-store`), CORS allowlist, request
  body limit, per-IP and per-account rate limiting (Redis).
- Liveness/readiness probes, structured JSON logs carrying `request_id` + `trace_id`,
  OpenTelemetry traces (HTTP → SQL → Redis), Prometheus latency histograms.
- Read-replica-ready (`DATABASE_READ_URL`) and pooled connections.

## API

| Method | Path | Auth |
|---|---|---|
| POST | `/v1/auth/register` | public, `Idempotency-Key` required |
| POST | `/v1/auth/login` | public |
| POST | `/v1/auth/mfa/verify` | MFA challenge token |
| POST | `/v1/auth/mfa/setup`, `/v1/auth/mfa/enable` | bearer |
| POST | `/v1/auth/refresh`, `/v1/auth/logout` | refresh token |
| GET | `/v1/users/me` | bearer |
| GET | `/v1/users/me/data-export` | bearer (DPDP right of access) |
| POST | `/v1/users/me/erasure` | bearer + password (DPDP right to erasure) |
| PATCH | `/v1/users/me/profile` | bearer |
| GET | `/v1/admin/users` | admin + MFA |
| PATCH | `/v1/admin/users/{id}/role` | admin + MFA |
| GET | `/v1/admin/audit-logs`, `/v1/admin/audit-logs/verify` | admin/support + MFA |
| GET/PUT | `/v1/doctors/me` | doctor + MFA |
| PATCH | `/v1/doctors/me/accepting` | doctor + MFA |
| POST/GET | `/v1/doctors/me/slots` | doctor + MFA (POST needs `Idempotency-Key`) |
| DELETE | `/v1/doctors/me/slots/{id}` | doctor + MFA |
| GET | `/v1/doctors`, `/v1/doctors/specialties` | public, rate limited |
| GET | `/v1/doctors/{id}`, `/v1/doctors/{id}/slots` | public, rate limited |
| GET | `/v1/admin/doctors` | admin + MFA |
| POST | `/v1/admin/doctors/{id}/verification` | admin + MFA |
| POST | `/v1/consultations` | patient, `Idempotency-Key` required |
| GET | `/v1/consultations`, `/v1/consultations/{id}` | participants (staff: audited) |
| POST | `/v1/consultations/{id}/cancel` | participants/admin, `Idempotency-Key` |
| POST | `/v1/consultations/{id}/start`, `/complete`, `/no-show` | consulting doctor |
| GET | `/v1/consultations/{id}/join` | participants |
| POST | `/v1/consultations/{id}/review` | patient, `Idempotency-Key` |
| POST/GET | `/v1/consultations/{id}/prescription` | doctor (POST) / participants |
| GET | `/v1/medicines` | doctor |
| GET | `/v1/prescriptions/signing-key` | public |
| POST | `/v1/payments/webhooks/mockpay` | gateway (HMAC signed) |
| POST | `/v1/payments/mock/{id}/complete` | patient, **dev only** (simulates checkout) |
| GET/POST | `/v1/admin/outbox`, `/v1/admin/outbox/{id}/retry` | admin + MFA |
| GET | `/v1/admin/analytics/overview`, `/daily`, `/doctors`, `/specialties` | admin + MFA |
| POST | `/v1/admin/analytics/refresh` | admin + MFA (async, 202) |
| GET | `/health/live`, `/health/ready`, `/metrics` | public (internal in prod) |

## Configuration

All configuration is environment variables (see `.env.example`). Secrets have no defaults and
the app refuses to start with missing or too-short keys.

| Variable | Purpose |
|---|---|
| `DATABASE_URL`, `DATABASE_READ_URL` | Postgres primary / optional replica |
| `REDIS_URL` | rate limiting, cache, queues |
| `JWT_KEYS`, `JWT_ACTIVE_KID` | `{kid: secret}` signing key ring |
| `DATA_ENCRYPTION_KEYS`, `DATA_ACTIVE_KEY_VERSION` | `{version: base64 32B}` field encryption keys |
| `BLIND_INDEX_KEY` | HMAC key for searchable encrypted fields |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP/HTTP collector, e.g. `http://jaeger:4318` |
| `PAYMENT_WEBHOOK_SECRET` | HMAC secret shared with the payment gateway |
| `PRESCRIPTION_SIGNING_KEY`, `PRESCRIPTION_SIGNING_KEY_ID` | Ed25519 seed for e-prescriptions |
| `MOCKPAY_FAILURE_RATE` | chaos knob: fraction of gateway calls that fail (0–1) |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | per-process connection pool (compose: 15 / 0) |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | trace sampling (compose: 10% parent-based) |
| `CORS_ORIGINS` | JSON list of allowed origins |

## Layout

```
app/
  core/          config, db, security (passwords/JWT), crypto, errors, logging, observability, DI
  middleware/    request context, security headers, body limit, idempotency
  core/cache.py  Redis cache-aside with generation-based invalidation
  core/resilience.py  retry with full-jitter backoff, circuit breaker
  modules/       auth · users · doctors · availability · consultations · payments ·
                 prescriptions · analytics · privacy · outbox · admin · audit · health
                 (router → service → repository → models)
  worker/        arq worker: outbox dispatcher, sweeper, housekeeping
alembic/         migrations (partitioned audit log, exclusion constraints, triggers, seed)
tests/           unit + integration (real Postgres/Redis)
infra/docker/    nginx gateway, prometheus (+ alert rules), grafana (dashboards), alloy → loki
tests/load/      k6 peak-hour scenario with SLO thresholds
infra/terraform/ AWS infrastructure (+ envs/*.tfvars, backend configs)
docs/            architecture, security, compliance, openapi.yaml, runbooks, demo script, planning notes
AGENTS.md        guide for coding agents; skills in .agents/skills/
.github/         CI: lint, types, tests, bandit, pip-audit, gitleaks, CodeQL, image build + Trivy
```
