# Security: checklist and threat model

Each control names the file or test that implements or proves it.

## 1. Security checklist

| # | Control | Status | Evidence |
|---|---|---|---|
| **Identity and access** ||||
| 1 | Passwords hashed with Argon2id, off the event loop; NIST-style length policy + blocklist | ✅ | `app/core/security.py`, `app/modules/auth/schemas.py` |
| 2 | Unknown-email and wrong-password logins take equal time and return the same error | ✅ | `PasswordService.verify_dummy`, `test_wrong_password_and_unknown_email_look_the_same` |
| 3 | Account lockout (5 failures → 15 min) + per-IP and per-account rate limits | ✅ | `AuthService._register_failure`, `test_account_locks_after_repeated_failures` |
| 4 | **TOTP MFA**, replay-protected (time step can't be reused), single-use hashed recovery codes | ✅ | `AuthService._verify_totp`, `test_mfa_login_flow_and_code_replay_blocked` |
| 5 | **MFA mandatory** for doctor, admin and support before any privileged action | ✅ | `require_roles`, `test_admin_without_mfa_is_blocked` |
| 6 | **RBAC** with the role read from the DB on every request (revocation is immediate) | ✅ | `app/modules/auth/dependencies.py`, `test_role_change_takes_effect_immediately` |
| 7 | **Object-level authorization (BOLA)**: non-participants get 404, never another user's data | ✅ | `ConsultationService._load`, `test_other_users_cannot_see_or_touch_consultation` |
| 8 | Short-lived JWTs (15 min, `kid` key ring) + opaque rotating refresh tokens with reuse detection | ✅ | `TokenService`, `test_refresh_rotation_and_reuse_detection` |
| 9 | Step-up re-authentication for destructive account actions (erasure) | ✅ | `PrivacyService.erase` |
| **Data protection** ||||
| 10 | TLS 1.2+/1.3 at the ALB, HTTP→HTTPS redirect, HSTS; `rds.force_ssl`; Redis in-transit encryption | ✅ | `infra/terraform/edge.tf`, `data.tf`, `SecurityHeadersMiddleware` |
| 11 | Encryption at rest with KMS customer-managed keys (RDS, snapshots, Redis, secrets, logs, backups) | ✅ | `infra/terraform/kms.tf`, `data.tf` |
| 12 | **Field-level AES-256-GCM** for PHI (phone, DOB, complaints, notes, prescriptions, TOTP secrets) with per-row AAD | ✅ | `app/core/crypto.py`, `test_ciphertext_bound_to_row`, `test_profile_update_encrypts_phi_at_rest` |
| 13 | Blind index (HMAC) for uniqueness/lookup on encrypted phone numbers | ✅ | `FieldCipher.blind_index` |
| 14 | No PHI in logs, audit metadata, outbox payloads, notifications or error bodies | ✅ | `redact_processor`, `test_validation_errors_never_echo_input`, `test_log_redaction_is_recursive` |
| 15 | `Cache-Control: no-store` on all API responses | ✅ | `SecurityHeadersMiddleware` |
| **Integrity and non-repudiation** ||||
| 16 | Append-only, **hash-chained**, monthly-partitioned audit log; DB trigger blocks UPDATE/DELETE | ✅ | migration 0001, `test_audit_log_is_append_only`, `/v1/admin/audit-logs/verify` |
| 17 | Staff access to consultations/prescriptions is always audited | ✅ | `consultation.viewed_by_staff`, `prescription.viewed_by_staff` |
| 18 | E-prescriptions signed with **Ed25519**, immutable in the DB, verifiable offline | ✅ | `app/modules/prescriptions/signing.py`, `test_signed_prescription_round_trip` |
| 19 | Payment webhooks: HMAC-SHA256 + timestamp (5 min replay window) + event dedupe | ✅ | `verify_webhook`, `test_webhook_security_and_dedup` |
| **Input, abuse and API hygiene** ||||
| 20 | Strict Pydantic schemas (`extra="forbid"`), parameterised SQL only, controlled vocabularies | ✅ | all `schemas.py`; Bandit clean; `test_hostile_search_input_is_harmless` |
| 21 | Body size limit (1 MB), rate limits (edge WAF + app), pagination caps | ✅ | `BodySizeLimitMiddleware`, `rate_limit`, `edge.tf` WAF |
| 22 | **Idempotency** on all writes; required where duplicates harm | ✅ | `app/middleware/idempotency.py`, `test_idempotency.py`, `test_retries_never_double_book` |
| 23 | Security headers (HSTS, CSP, nosniff, frame-deny, CORP), CORS allowlist | ✅ | `SecurityHeadersMiddleware`, `test_security_headers_and_request_id` |
| 24 | `/metrics`, `/docs` and the mock payment endpoint unreachable from the internet | ✅ | ALB listener rule in `edge.tf`; mock router not mounted in production |
| **Platform and supply chain** ||||
| 25 | Secrets only from env / Secrets Manager; none in code, images or Terraform state | ✅ | `Settings` (no defaults for secrets), `secrets.tf`, gitleaks in CI |
| 26 | Non-root container (uid 10001), read-only root FS, minimal slim image | ✅ | `Dockerfile`, `compute.tf` |
| 27 | SAST (Bandit, CodeQL), dependency CVEs (pip-audit), secret scan (gitleaks), image scan (Trivy), IaC scan (Checkov) | ✅ | `.github/workflows/ci.yml` |
| 28 | Private subnets for tasks and data; security groups chain ALB → app → data; VPC flow logs | ✅ | `infra/terraform/network.tf` |
| 29 | WAF with AWS managed OWASP / bad-input / SQLi / IP-reputation rules + per-IP rate rule | ✅ | `edge.tf` |
| 30 | Least-privilege IAM: task role empty, execution role reads only this app's secrets | ✅ | `iam.tf` |
| 31 | Immutable, locked backups (compliance mode) | ✅ | `aws_backup_vault_lock_configuration` |
| 32 | Security alerts: webhook signature failures, login-failure spikes, circuit open, dead letters | ✅ | `infra/docker/prometheus/alerts.yml` |

## 2. Data classification

| Class | Examples | Storage protection | Exposure rules |
|---|---|---|---|
| **Restricted (PHI)** | chief complaint, doctor notes, prescriptions, DOB | KMS at rest **+ AES-GCM field encryption** | Only the patient, their doctor, or audited staff; never in logs or events |
| **Confidential (PII / credentials)** | phone, email, password hashes, TOTP secrets, refresh-token hashes, payment ids | KMS at rest; phone and TOTP field-encrypted; passwords Argon2id; tokens SHA-256 | Owner + admins; redacted in logs |
| **Internal** | slot schedules, audit metadata, outbox, rollups | KMS at rest | Staff tooling only |
| **Public** | verified doctor name, qualification, registration no., fee, rating | KMS at rest | Public discovery API (required by TPG 2020) |

## 3. Threat model (STRIDE)

**Trust boundaries:** internet → WAF/ALB; ALB → API tasks; API/worker → Postgres/Redis
(private subnets); API/worker ↔ payment gateway (internet); admins → admin API.
**Key assets:** patient health data, prescriptions (legal documents), credentials and keys,
money flows, the audit trail.

| Threat | Component | Scenario | Mitigations |
|---|---|---|---|
| **S**poofing | Auth | Credential stuffing / brute force | Argon2id, lockout, per-IP + per-account limits, WAF rate rule, MFA for privileged roles, `LoginFailureSpike` alert |
| **S**poofing | Payment webhook | Attacker forges `payment.succeeded` to get a free consult | HMAC-SHA256 signature, 5-minute timestamp window, amount check against our record, dedupe inbox, `WebhookSignatureFailures` alert |
| **S**poofing | Doctor onboarding | Fake practitioner | Admin verification against the NCISM / State Board register before visibility; unique (council, registration no.); re-verification when credentials change |
| **T**ampering | Audit log | Insider edits or deletes evidence | DB trigger blocks UPDATE/DELETE; hash chain detects any edit; verify endpoint; app DB role without TRUNCATE (ops); locked backups |
| **T**ampering | Prescription | Altered dosage or medicine after issue | Ed25519 signature over the canonical document; table immutable via trigger; public key published |
| **T**ampering | Field ciphertext | Swap an encrypted value between rows | AES-GCM with AAD = `table.column:row_id` |
| **R**epudiation | Clinical actions | "I never prescribed that" / "I never cancelled" | Audit entry per action with actor, role, IP, request id, chained hash; signed prescriptions |
| **I**nformation disclosure | Consultations API | IDOR / BOLA enumeration | Ownership check on every load; 404 for non-participants; UUIDv4 ids |
| **I**nformation disclosure | Logs / errors / telemetry | PHI leaks into log storage or traces | Structured redaction, no input echo, ids-only events and notifications, Authorization header redacted in WAF logs |
| **I**nformation disclosure | Database / backups | Stolen snapshot or disk | KMS encryption; field encryption makes PHI unreadable without app keys (separate KMS key) |
| **D**enial of service | API | Flood / expensive queries | WAF, app rate limits, body limit, page caps, query windows ≤ 31 days, indexed partial queries, autoscaling, readiness checks |
| **D**enial of service | Booking | Holding every slot without paying | 10-minute hold, sweeper, per-user booking rate limit, patient-overlap constraint (one hold per time window), `CheckoutAbandonmentHigh` alert |
| **E**levation of privilege | Registration / profile | Self-assigning admin, or mass assignment of `role` | Signup role limited to patient/doctor; `extra="forbid"` rejects unknown fields; role changes admin-only, audited, never self |
| **E**levation of privilege | Prescribing | Patient or another doctor writes a prescription | `require_roles(DOCTOR)` + consulting-doctor check + state check; TPG List B blocked, List A gated |

### Attack surface

| Entry point | Exposure | Auth | Notes |
|---|---|---|---|
| `/v1/auth/*` | Internet | none / token | Tightest rate limits; responses never cached by idempotency |
| `/v1/doctors*` (discovery) | Internet | none | Read-only, cached, rate limited, verified doctors only |
| `/v1/consultations*`, `/v1/users/me*`, prescriptions | Internet | JWT (+ MFA for staff) | Object-level checks, idempotency |
| `/v1/admin/*` | Internet (could be VPN-only) | Admin + MFA | Every mutation audited |
| `/v1/payments/webhooks/*` | Internet (gateway) | HMAC | Optionally allowlist the gateway's IP ranges in WAF |
| `/metrics`, `/docs`, `/v1/payments/mock/*` | Blocked at ALB / not mounted in prod | n/a | |
| Postgres, Redis | Private subnets | SG + TLS + password / IAM | No public endpoints |

## 4. OWASP API Security Top 10 (2023) mapping

| Risk | Mitigation |
|---|---|
| API1 Broken object level authorization | `_load()` ownership checks, 404 on mismatch, tests per resource |
| API2 Broken authentication | Argon2id, MFA, lockout, short JWTs, refresh rotation + reuse detection |
| API3 Broken object property level authorization | Explicit response models (no ORM passthrough), `extra="forbid"` on input |
| API4 Unrestricted resource consumption | Rate limits, body/page/window caps, WAF, autoscaling |
| API5 Broken function level authorization | `require_roles` on every privileged route, MFA gate, admin-only routers |
| API6 Unrestricted access to sensitive business flows | Booking holds expire, per-user limits, patient-overlap constraint, idempotency |
| API7 SSRF | The service makes no user-controlled outbound requests |
| API8 Security misconfiguration | Security headers, docs disabled in prod, Checkov-clean IaC, non-root read-only containers |
| API9 Improper inventory management | Generated OpenAPI checked in CI; `/v1` versioning; internal endpoints blocked at the edge |
| API10 Unsafe consumption of APIs | Gateway responses typed and validated; webhooks verified, deduped, amount-checked |

The OWASP Top 10 (2021) web risks are covered by the same controls: A01 (#6–7), A02 (#10–13),
A03 (#20), A05 (#23–26), A06 (#27), A07 (#1–5, #8), A08 (#18–19, #27), A09 (§3 alerts,
#16–17) and A10 (API7).

## 5. Encryption and key rotation

| Key | Where | Rotation procedure | Downtime |
|---|---|---|---|
| JWT signing keys | `JWT_KEYS` ring + `JWT_ACTIVE_KID` | Add `k2`, deploy; switch `JWT_ACTIVE_KID=k2`, deploy; after 15 min (access-token TTL) remove `k1` | None (`test_key_rotation_keeps_old_tokens_valid`) |
| Field-encryption keys | `DATA_ENCRYPTION_KEYS` ring + `DATA_ACTIVE_KEY_VERSION` | Add `v2` and make it active (new writes use it; ciphertexts carry their version, so old ones still decrypt); re-encrypt job over rows where `needs_reencryption()`; then retire `v1` | None (`test_rotation_keeps_old_ciphertexts_readable`) |
| Blind-index key | `BLIND_INDEX_KEY` | Compute the new index column alongside the old one, backfill, switch lookups, drop the old one | None |
| Prescription signing key | `PRESCRIPTION_SIGNING_KEY` + `_KEY_ID` | New key + id; prescriptions store `signing_key_id`; publish old public keys for verifying historic prescriptions | None |
| Webhook secret | `PAYMENT_WEBHOOK_SECRET` | Gateways support two active secrets: add new, then verify against both, then drop old | None |
| DB master password | RDS-managed secret | Automatic rotation by RDS / Secrets Manager | None |
| KMS CMKs | AWS KMS | Automatic yearly key-material rotation | None |

## 6. Audit logging

Every security-relevant event is audited **in the same transaction** as the change:
registration, logins (success, failure, lockout), MFA setup/enable, refresh-token reuse,
logout, role changes, doctor verification decisions, slot publishing/removal, bookings,
cancellations, starts, completions, no-shows, reviews, prescriptions, staff views of PHI,
payment outcomes and refunds, outbox retries, analytics refreshes, data exports and erasures.
Each entry stores actor, role, action, resource, IP, user agent, request id, PHI-free metadata,
`prev_hash` and `hash`. Partitions are monthly; retention is enforced by detaching partitions,
never by `DELETE`.

## 7. Dependency and supply-chain scanning

CI (`.github/workflows/ci.yml`) runs on every push/PR: **Bandit** (SAST), **CodeQL**,
**pip-audit** against the locked runtime dependencies, **gitleaks** over git history, **Trivy**
on the built image (fails on fixable HIGH/CRITICAL), and **Checkov** on Terraform. `uv.lock`
pins every transitive dependency; ECR scans on push and tags are immutable;
**Dependabot** (`.github/dependabot.yml`) opens weekly update PRs for Python packages, the
base image, GitHub Actions and Terraform providers, and each PR goes through the full CI.

## 8. Residual risks and follow-ups

* The audit hash chain detects tampering only when verified: schedule `verify` daily and
  anchor the chain head externally (e.g. write it to the locked backup vault).
* An insider with database-owner rights could drop the trigger: run the app with a non-owner
  role (no DDL, no TRUNCATE on `audit_logs`) and alert on DDL via RDS logs.
* Field-encryption keys live in the app's memory. For stronger isolation, unwrap per-tenant
  data keys via KMS (envelope encryption) and cache them briefly.
* Formal penetration test and DPDP data-protection impact assessment before launch.
