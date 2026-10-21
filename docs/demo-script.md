# 5-minute demo video script

**Before recording:** `make env && make up && make seed-load`; create an admin (`make admin
EMAIL=...`) and enrol MFA; open tabs: Swagger (`localhost:8000/docs`), Grafana (`localhost:3300`,
*Amrutam* folder), Jaeger (`localhost:16686`), the repo in an editor. Have an authenticator app
ready.

| Time | Show | Say (key points) |
|---|---|---|
| **0:00–0:30** | README + `docs/architecture.md` flowchart | "Telemedicine backend for Amrutam: modular monolith in FastAPI, Postgres, Redis, a worker for async jobs; 4 API replicas behind a gateway; Terraform for AWS Mumbai with DR in Hyderabad." |
| **0:30–1:10** | Swagger: register patient → login → register doctor → login shows `mfa_enrollment_required` → MFA setup/enable with the authenticator | "Argon2 passwords, short JWTs with rotating refresh tokens, and MFA mandatory for doctors and admins: without it they can't touch anything privileged." |
| **1:10–1:40** | Doctor `PUT /v1/doctors/me` → admin approves → `POST /doctors/me/slots` with a weekly rule → `GET /v1/doctors?specialty=digestive_health` | "Doctors are verified against their council registration before they're visible. Slots come from a weekly rule in IST; Postgres forbids overlapping slots. Search runs on 5,000 doctors with caching." |
| **1:40–2:40** | `POST /v1/consultations` (show `Idempotency-Key`) → resend the same request → `Idempotent-Replayed: true` → second patient books the same slot → 409 → `POST /payments/mock/{id}/complete` → consultation `confirmed` | "Booking is a saga: hold the slot atomically, pay, confirm by signed webhook. Retries never double-book; a second patient can't take the slot. If payment fails or times out, the slot is released and late payments are refunded automatically." Optional: show `test_only_one_of_many_concurrent_bookings_wins`. |
| **2:40–3:20** | Doctor `start` → prescription with **Agnitundi Vati** → 422 → valid prescription → `GET /prescriptions/signing-key` | "TPG 2020 rules are enforced: List B drugs can't be prescribed online, List A only after video. Prescriptions are Ed25519-signed and immutable; a pharmacy can verify them offline." |
| **3:20–4:00** | `GET /v1/admin/audit-logs` → `/verify` → `GET /v1/admin/analytics/overview` → `GET /v1/users/me/data-export` | "Every action lands in a hash-chained, append-only, partitioned audit log. Analytics come from rollups the worker maintains. DPDP rights: full export, and erasure that keeps legally required medical records." |
| **4:00–4:40** | Grafana *API - SLOs & traffic* during `make load`; click a slow request's trace in Jaeger; Loki error panel; Prometheus `/alerts` | "SLO dashboards, error-budget burn-rate alerts, logs linked to traces. Load testing found two real bottlenecks, a single-core process and a per-row subquery in search, both fixed; search went from 69 ms to 0.4 ms." |
| **4:40–5:00** | GitHub Actions run (lint, tests, Bandit/CodeQL/pip-audit/gitleaks, Checkov, Trivy) + `docs/security.md` | "137 tests against real Postgres, 94% coverage, security scanning on code, dependencies, image and Terraform; threat model and security checklist in the docs. Thanks!" |

**Tips:** record at 1080p, zoom the browser to 125%, hide bookmarks, and keep each request
pre-filled in Swagger so the run stays under five minutes.
