# Amrutam Telemedicine Backend: Research & Plan

> Research notes for the Amrutam backend assignment. Covers what the task asks for, the domain, compliance, a design, the stack, and a 5-day plan.

---

## 1. What Amrutam is (context for the design)

- **Amrutam** is an Indian Ayurveda brand (products and wellness). **Amrutam Global** (amrutam.global) is its consultation platform, which connects users with certified **Ayurvedic doctors** (the site currently lists about 38 verified experts).
- It runs two apps: the **Amrutam app** (patients: consultations, health tracking, session reminders, products) and the **Amrutam Doctor / Doc app** (doctors: an **availability toggle**, online consultations, no physical clinic needed).
- So the domain is: patient ↔ Ayurvedic doctor, slot booking, video/audio/chat consultation, an Ayurvedic prescription (herbs and formulations, often Amrutam's own products), and payment.
- **Design hook for bonus points:** prescriptions can link to Amrutam product SKUs (prescription → product recommendation). This would be a "bonus" feature.

## 2. Indian regulations to follow (strong signal in the "Compliance" section)

| Regulation | What it means for the backend |
|---|---|
| **Telemedicine Practice Guidelines 2020 (MoHFW/NMC)** + **AYUSH telemedicine guidelines** (Ayurvedic doctors follow the AYUSH version) | Store the doctor's **registration number and qualification** and verify them at onboarding. **Identify the patient** before advice is given (no anonymous consults). Record **explicit consent** before the first consult (an "I agree" click is valid, so store it with a timestamp, IP and version). Keep consult records. |
| **Prescription rules** | The e-prescription must include the doctor's name, qualification, reg. no., contact, date and patient details, plus a **digital signature or equivalent**. Medicine lists **O / A / B**: List A needs a **video** consult the first time, and List B / Schedule X / NDPS drugs **cannot** be prescribed by tele-consult. → Enforce this server-side: `consultation.mode` × `medicine.category` validation. |
| **DPDP Act 2023** (India's data protection law) | The platform is a **data fiduciary**. Consent must be specific, informed and purpose-limited. Users have rights to access, correction and **erasure** (with a healthcare-record retention exception). **Breach notification** goes to the Data Protection Board. → Build a `consents` table, a data-export endpoint, soft-delete plus anonymisation, and keep **separate records** for medical consent and data-processing consent. |
| **ABDM / ABHA** (Ayushman Bharat Digital Mission) | Optional: an `abha_id` field on profiles, ready for future integration. Mention it in the docs as a roadmap item. |

These go into: `docs/compliance.md`, the `consents` table, the `audit_logs` design, and prescription validation logic.

## 3. Rubric analysis: what gets scored

| Area | Pts | Priority |
|---|---|---|
| Architecture | 20 | Docs and diagrams carry most of the weight |
| Core flows | 20 | Booking must be **concurrency-safe and idempotent** |
| Code quality | 15 | Modular, DI, typed, tested |
| Security | 10 | MFA, RBAC, encryption, threat model |
| Observability | 10 | Metrics, logs and traces, wired up and visible in the demo |
| Scalability | 10 | Partitioning, caching, capacity math |
| Infra/CI | 10 | Docker, IaC, GitHub Actions |
| Bonus | +10 | |

**Automatic FAIL conditions:** a *critical security* gap or *missing idempotency*. So:
- Every POST/PATCH that changes state accepts an `Idempotency-Key` header, stored in the DB with the request hash and the cached response.
- Double-booking a slot must be impossible, even under parallel requests. Prove it with a concurrency test.
- No secrets in the repo (CI runs gitleaks), passwords hashed with argon2, PHI encrypted.

## 4. Capacity math (put this in the architecture doc)

- 100k consultations a day ≈ **1.2 bookings/s on average**. With a 10× peak factor (evenings), that's **~15 writes/s** for bookings.
- Reads (search, slots, profiles) at roughly 50:1 → **~600–1,000 RPS at peak**.
- Storage: about 100k consults × ~5 KB (consult, prescription, audit rows) ≈ **0.5 GB/day → ~180 GB/year**. Audit logs grow fastest, which justifies **monthly partitioning**.
- Conclusion: a single primary Postgres plus read replicas plus Redis handles this easily. Stateless API pods scale horizontally. No need for microservices sprawl: a **modular monolith** plus a separate **worker** process is the right call. (Saying this explicitly shows maturity.)

## 5. Proposed architecture

```
Clients (Patient app / Doctor app / Admin)
        │ HTTPS (TLS 1.3)
   [API Gateway / LB]  ── WAF, rate limit (edge)
        │
   [API service – stateless, N replicas]   ←→  Redis (cache, rate-limit, locks, idempotency fast-path)
     modules: auth · users · doctors · availability · booking
              consultations · prescriptions · payments · search · admin · audit
        │                         │
   PostgreSQL primary  ──WAL──►  read replica(s)
        │  (transactional outbox table)
   [Worker service] ── arq/Redis queues: notifications, PDF Rx generation,
                       payment reconciliation, analytics rollups, slot expiry
        │
   Observability: OpenTelemetry SDK → OTel Collector → Prometheus (metrics) + Tempo/Jaeger (traces) + Loki (logs) → Grafana
```

### Key design decisions

1. **Booking without double-booking**
   - `availability_slots(id, doctor_id, start_at, end_at, status: OPEN|HELD|BOOKED, version)`
   - Booking runs as an **atomic conditional update**: `UPDATE slots SET status='HELD', held_by=$u, hold_expires=now()+10m WHERE id=$1 AND status='OPEN' RETURNING *`. Zero rows means 409 Conflict.
   - Backed by a **unique partial index** on `consultations(slot_id) WHERE status NOT IN ('CANCELLED')`, so the DB itself guarantees one booking per slot.
   - Hold expiry: a worker job releases HELD slots that were never paid for.
2. **Idempotency**: an `idempotency_keys(key, user_id, request_hash, response_code, response_body, created_at, expires_at)` table. It's checked inside the same transaction, and a different body sent with the same key returns 422. TTL is 24h.
3. **Saga for Book → Pay → Confirm** (orchestrated):
   `HOLD slot → create consultation(PENDING_PAYMENT) → create payment intent → [webhook] payment success → CONFIRM consultation + BOOK slot → notify`
   Compensations: payment failed or timed out → cancel consultation and release the slot. Paid but confirm failed → refund job.
   Every step emits events through a **transactional outbox** (written in the same DB transaction, then relayed to the queue), so no event is lost.
4. **Consultation state machine**: `PENDING_PAYMENT → CONFIRMED → IN_PROGRESS → COMPLETED` (or `CANCELLED / NO_SHOW / REFUNDED`). Transitions are guarded in code and recorded in the audit log.
5. **Retry & backoff**: exponential backoff with **full jitter** (`base·2^n`, capped, randomised), max N attempts, then a **dead-letter queue**. Only idempotent operations get retried. A **circuit breaker** wraps external calls (payment gateway, SMS/email).
6. **Caching**: cache-aside in Redis for doctor profiles and search results (TTL 60–300s), with **event-driven invalidation** when availability changes. Slots are never served from cache for the booking decision; the DB is the source of truth. ETags on GET responses.
7. **Data partitioning**: `audit_logs` and `consultations` use **range partitions by month** (pg_partman). Search on doctors gets a GIN index (specialty, language, `tsvector` on name/bio) plus a btree index on `(doctor_id, start_at)` for slots. Future step: shard by region/tenant using a consistent-hash key on `doctor_id`.
8. **Audit trail**: an append-only `audit_logs` table (who, what, when, ip, before/after diff). It's **hash-chained** (`hash = sha256(prev_hash + row)`) so tampering is detectable. A DB role revokes UPDATE/DELETE on it.
9. **Backup & DR**: Postgres PITR (WAL archiving to S3), daily snapshots, and a cross-region replica. Targets: **RPO ≤ 5 min, RTO ≤ 30 min** to support 99.95% (≈ 22 min of downtime a month). Run quarterly restore drills (document the runbook).

## 6. Data model (core tables + additions)

Required: `users, profiles, doctors, availability_slots, consultations, prescriptions, payments, audit_logs`

Additions I'd make: `roles/user_roles` (RBAC), `mfa_secrets`, `refresh_tokens`, `consents`, `idempotency_keys`, `outbox_events`, `prescription_items`, `medicines (category O/A/B)`, `doctor_specialties`.

Encrypted (application-level AES-256-GCM, **envelope encryption** with a KMS-held KEK and a `key_version` column for rotation): phone, DOB, symptoms/notes, prescription content.

## 7. Security plan

- **Auth**: email/phone + password (argon2id), **TOTP MFA** (required for doctors and admins), short-lived JWT access tokens (15 min) plus **rotating refresh tokens** with reuse detection.
- **RBAC**: roles `patient, doctor, admin, support`, plus **ownership checks** (a patient sees only their own consults; a doctor sees only theirs). This is the main fix for OWASP API #1, BOLA/IDOR.
- **OWASP Top 10 / API Top 10 mapping** in `docs/security.md`, covering input validation (Pydantic), parameterised SQL, security headers (HSTS, CSP, X-Content-Type-Options), CORS allowlist, rate limiting (per-IP + per-user, Redis sliding window), and request body size limits.
- **Threat model**: STRIDE per component, with an attack surface table (public API, webhooks with signature verification, admin panel, DB, queues) and data classification (Public / Internal / Confidential / **Restricted-PHI**).
- **Key rotation**: JWT signing keys published via JWKS with `kid` (rotate without downtime), DEK re-wrap jobs.
- **Supply chain**: `npm audit`, Dependabot, **Trivy** image scan, **gitleaks**, CodeQL in CI.

## 8. Recommended stack

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.12** (in Docker; libs are most stable there) | Allowed by the brief |
| Framework | **FastAPI** (my choice; the brief names no framework) | Async, fast, **OpenAPI generated automatically**, built-in DI via `Depends()`. For a cleaner service/repository layer, add `dependency-injector` containers |
| DB | PostgreSQL 16 | Required |
| Query layer | **SQLAlchemy 2.0 (async) + asyncpg**, **Alembic** migrations | Full control for `SELECT … FOR UPDATE`, partitions and partial indexes (raw SQL in migrations where needed) |
| Cache/queue | Redis 7 + **arq** (async jobs) or Celery | Async jobs, rate limiting (`slowapi` or a custom Redis sliding window) |
| API | REST + **OpenAPI 3.1** (FastAPI generates it; export to `docs/openapi.yaml`) | Required deliverable |
| Validation | **Pydantic v2** | Strict schemas, `extra="forbid"` |
| Auth libs | `argon2-cffi`, `pyotp` (TOTP MFA), `PyJWT`, `cryptography` (AES-GCM envelope encryption) | |
| Observability | OpenTelemetry (FastAPI/SQLAlchemy/Redis auto-instrumentation), `structlog` JSON logs with PII redaction, `prometheus-fastapi-instrumentator`, Grafana, Tempo/Jaeger, Loki | |
| Tests | **pytest + pytest-asyncio + testcontainers** (real Postgres/Redis), httpx `AsyncClient`, **k6** or Locust load test (proves p95 targets) | |
| Tooling | `uv` (deps), `ruff` (lint/format), `mypy` (types), `bandit` + `pip-audit` (security) | |
| Infra | Docker, docker-compose (local), **Terraform** (AWS: ECS Fargate, RDS Multi-AZ, ElastiCache, KMS, Secrets Manager) or a Helm chart | |
| CI/CD | GitHub Actions: lint → typecheck → unit → integration → security scans → build image → push GHCR → deploy | |

*(Language chosen: Python. The design above is language-independent.)*

## 9. Repo structure

```
amrutam-telemedicine-backend/
├── app/
│   ├── main.py          # FastAPI app factory
│   ├── core/            # config (env vars), DI container, security, crypto, observability
│   ├── modules/         # auth, users, doctors, availability, booking, consultations,
│   │                    # prescriptions, payments, search, admin, audit
│   │   └── <module>/    # router.py · schemas.py · service.py · repository.py · models.py
│   ├── middleware/      # idempotency, rate limit, request-id, audit
│   └── worker/          # arq jobs + outbox relay
├── alembic/             # migrations
├── pyproject.toml
├── infra/terraform/     # AWS IaC
├── infra/docker/        # compose: pg, redis, otel, prometheus, grafana, tempo, loki
├── docs/
│   ├── architecture.md  # 2–4 pages + diagrams (Mermaid)
│   ├── security.md      # checklist + STRIDE threat model
│   ├── compliance.md    # TPG 2020, AYUSH, DPDP
│   ├── runbooks/dr.md
│   └── openapi.yaml
├── tests/{unit,integration,load}
├── .github/workflows/ci.yml
└── README.md
```

## 10. Five-day plan

| Day | Work |
|---|---|
| **1 ✅** | Scaffold (FastAPI, uv, compose, Alembic, CI skeleton). Auth: register/login, argon2, JWT and refresh, TOTP MFA, RBAC guards. Audit + idempotency middleware. |
| **2 ✅** | Doctors (onboarding with reg. no. verification status), availability slots (bulk create, recurring), search/filter (specialty, language, price, rating, next-available) with caching. |
| **3 ✅** | Booking saga (hold → pay (mock gateway + signed webhook) → confirm), outbox + worker, retries/DLQ, cancellation/refund. Consultation state machine, prescriptions with O/A/B validation, PDF job. |
| **4 ✅** | Admin analytics (daily consults, revenue, doctor utilisation, from materialised views or rollup jobs). Observability stack plus Grafana dashboard. Tests: concurrency double-booking test, idempotency test, k6 load test. Trivy/gitleaks/CodeQL. |
| **5 ✅** | Terraform, docs (architecture, sequence/ER Mermaid diagrams, security, DR), OpenAPI polish, README, **5-min demo video**. |

### Demo video (5 min) script
1. `docker compose up` brings up the whole stack (30s)
2. Swagger: register patient + doctor, enable MFA (60s)
3. Doctor creates slots, patient searches and books with an Idempotency-Key, and a retry returns the same response (60s)
4. Two parallel bookings on the same slot: one gets 201, the other 409 (30s)
5. Payment webhook → confirmed → prescription → audit log entries (60s)
6. Grafana: p95 latency, a trace across API → DB → worker; k6 results (45s)
7. CI pipeline green (15s)

## 11. Bonus ideas (+10)
- Prescription → Amrutam product recommendation (SKU linking)
- Hash-chained tamper-evident audit log
- k6 results showing p95 targets are met
- DPDP data export / erasure endpoints
- Video room token issuance (e.g., a LiveKit/Agora token endpoint, stubbed)
- Feature flags and graceful shutdown/readiness probes

## Sources
- Amrutam Global: https://www.amrutam.global/
- Amrutam Home & Doctor apps launch: https://amrutam.co.in/blogs/daily-reads/launch-alert-amrutam-home-and-doctors-apps
- Telemedicine Practice Guidelines 2020 (PMC review): https://pmc.ncbi.nlm.nih.gov/articles/PMC8106416/
- CMS legal guide, India telemedicine: https://cms.law/en/int/expert-guides/cms-expert-guide-to-digital-health-apps-and-telemedicine/india
- Telemedicine + DPDP summary: https://docover.in/resources/hospital/telemedicine_guidelines.html
- Platform regulatory issues: https://www.khuranaandkhurana.com/regulatory-issues-in-telemedicine-platform-models
