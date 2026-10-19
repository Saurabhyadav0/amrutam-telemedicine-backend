---
name: amrutam-security-review
description: Security review of a diff in the Amrutam telemedicine backend, mapped to this repo's controls (docs/security.md), threat model and Indian compliance rules (TPG 2020, DPDP Act). Use as the security gate before any change is called done, when reviewing a PR, or when the user mentions security, PHI, auth, RBAC, MFA, encryption, webhooks, secrets, compliance or "is this safe".
---

# Amrutam security review

Review the **diff**, not the whole repo. For each touched surface, check the matching rows
below against `docs/security.md` (checklist, STRIDE table, OWASP API mapping). Report
findings as: location → pattern → impact here → concrete fix. Don't change authn/authz,
crypto or validation semantics without confirming with the user.

## Surface → what to verify

| Surface touched | Verify |
|---|---|
| New/changed endpoint | Auth dependency present (`CurrentUser` / `require_roles`); privileged roles still need MFA; object-level ownership check with 404; `StrictModel` input; explicit response model (no ORM passthrough); rate limit if public or abusable; `require_idempotency_key` if duplicates hurt |
| Health data (PHI) | Stored via `FieldCipher` with row-bound AAD; not in logs, audit metadata, outbox payloads, notifications or error bodies; staff access audited; included in the DPDP export, and handled by erasure (anonymised or legally retained) |
| Auth / tokens / MFA | No weakening of lockout, token TTLs, refresh rotation or TOTP replay protection; token endpoints stay out of idempotency replay; no secrets in responses beyond what the flow requires |
| SQL | Parameterised only (SQLAlchemy expressions or `text()` with bound params); dynamic ORDER BY via bound CASE or allow-list, never f-strings; Bandit must stay clean |
| Payments / webhooks | Signature + timestamp verified before parsing trust; dedupe via `payment_events`; amount checked against our record; gateway calls carry an idempotency key |
| Prescriptions | TPG list rules still enforced (List B never, List A video-first); signed document unchanged in shape or the signer/key id updated; immutability trigger intact |
| Config / secrets | New secrets: no default, validated at startup, in `.env.example` (empty), `scripts/generate_env.py`, `infra/terraform/secrets.tf`; nothing secret committed (gitleaks) |
| Dependencies | Locked in `uv.lock`; `pip-audit` clean; justify any new dependency |
| Docker / Terraform / CI | Non-root, read-only container; no public data-tier exposure; KMS encryption; Checkov clean or skip with written reason; CI permissions minimal; deploy still OIDC |
| Audit trail | Security-relevant action recorded in the same transaction; no UPDATE/DELETE paths on `audit_logs` or `prescriptions` |

## Compliance quick checks (`docs/compliance.md`)

- Teleconsultation still requires and records explicit consent (TPG 2020).
- Doctor identity (registration no., council, qualification) still shown to patients and on
  prescriptions; unverified doctors still invisible and unable to publish slots.
- DPDP: purpose-specific consent records, export and erasure keep working; data stays in
  India-region infrastructure.

## Commands

`make security` (Bandit + pip-audit), `make test`, `make tf-check` for infra, and gitleaks
via Docker (`docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:v8.21.2 dir /repo`).

## Output

A short table of findings ranked by exploitability against this deployment, then "no
findings" for each surface you checked and found clean, so the reader knows what was covered.
