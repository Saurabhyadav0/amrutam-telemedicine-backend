# Compliance mapping (India)

How the backend implements the rules that apply to an Ayurvedic teleconsultation platform. This
is an engineering mapping, not legal advice; legal review should confirm it before launch.

## Telemedicine Practice Guidelines 2020 (MoHFW / Board of Governors, MCI)

Ayurvedic practitioners are additionally covered by the AYUSH Ministry's telemedicine guidelines,
which follow the same structure.

| Requirement | Implementation |
|---|---|
| Only registered practitioners may consult | Doctors submit registration number + council; admins verify against the NCISM / State Board register; only `verified` doctors appear in search or can publish slots (`doctors/service.py`) |
| Practitioner identifies themselves to the patient | Registration number, council and qualification on every search result, profile and prescription |
| Patient identification | Authenticated accounts only; no anonymous consults; the prescription carries patient name/age |
| Explicit patient consent before teleconsultation | `consent_to_teleconsultation: true` required on booking; recorded once per consent version in `consents` with IP and user agent |
| Medicine lists O / A / B | `medicines.tpg_list`; **List B** (e.g. Schedule E(1) drugs: Vatsanabha, Kupilu, Bhallataka preparations) always rejected; **List A** only after a video consult, refills allowed; **List O** any mode (`PrescriptionService._enforce_tpg_lists`) |
| Prescription format and signature | Canonical document with practitioner, patient, date, diagnosis, Rx and advice; **Ed25519 digital signature**; immutable |
| Maintain consultation records | Consultations, prescriptions, payments and audit retained (also after patient erasure); backups held up to 7 years |

## Digital Personal Data Protection Act 2023 (and DPDP Rules, 2025)

| Obligation | Implementation |
|---|---|
| Consent: free, specific, informed, purpose-limited | Separate consent records per purpose (`data_processing` at signup, `teleconsultation` at first booking), versioned, append-only |
| Purpose limitation / data minimisation | Only data needed for care and payment is collected; notifications and events carry ids, not health data |
| Reasonable security safeguards | Encryption (TLS, KMS, field-level AES-GCM), MFA, RBAC, audit trail; see `security.md` |
| Right of access | `GET /v1/users/me/data-export`: full decrypted export of profile, consents, consultations, prescriptions |
| Right to correction | `PATCH /v1/users/me/profile` |
| Right to erasure, with legal-retention exception | `POST /v1/users/me/erasure` (password step-up): identity and contact data removed, sessions revoked, consent withdrawal recorded; clinical records retained as medical-records rules require |
| Breach detection and notification | Security alerts + audit trail + logs support timely notification to the Data Protection Board and affected users (incident process in the runbooks) |
| Data localisation preference | Primary in **ap-south-1 (Mumbai)**, DR copies in **ap-south-2 (Hyderabad)**: data stays in India |

## Future: ABDM

`profiles` can gain an encrypted ABHA number, and prescriptions/consult summaries can be shared
as FHIR bundles to ABDM health lockers with patient consent, via the consent manager.
