# Test payloads — try the API by hand

Ready-to-use request bodies for walking the full flow. Two ways to use them:

- **Swagger UI** (easiest): open http://localhost:8000/docs, click an endpoint → *Try it out*,
  paste the JSON, *Execute*. Use the green **Authorize** button to paste a token once.
- **curl**: copy the commands below. Start the stack first with `make up`.

Every write that changes data needs a unique `Idempotency-Key` header — any UUID works, use a
new one per real action.

```bash
BASE=http://localhost:8000
uuid() { python3 -c 'import uuid;print(uuid.uuid4())'; }   # fresh idempotency key
```

---

## 1. Register a patient

`POST /v1/auth/register` — needs an `Idempotency-Key`.

```json
{
  "email": "asha.patient@example.com",
  "password": "Correct-Horse-Battery-9",
  "full_name": "Asha Patient",
  "phone": "+919812345678",
  "role": "patient",
  "accept_terms": true,
  "consent_version": "2026-01"
}
```

```bash
curl -s $BASE/v1/auth/register -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuid)" -d '{
    "email":"asha.patient@example.com","password":"Correct-Horse-Battery-9",
    "full_name":"Asha Patient","role":"patient","accept_terms":true}'
```

> `role` may only be `patient` or `doctor`. Admins are created with
> `docker compose exec api python -m app.cli create-admin --email you@example.com`.

## 2. Log in

`POST /v1/auth/login` (no idempotency key).

```json
{ "email": "asha.patient@example.com", "password": "Correct-Horse-Battery-9" }
```

```bash
TOKEN=$(curl -s $BASE/v1/auth/login -H "Content-Type: application/json" \
  -d '{"email":"asha.patient@example.com","password":"Correct-Horse-Battery-9"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["tokens"]["access_token"])')
echo "$TOKEN"
```

Response is either `{"tokens": {...}}` (no MFA) or `{"mfa_required": true, "mfa_token": "..."}`.
For a patient there's no MFA, so you get tokens directly. Put the `access_token` in the
**Authorize** box in Swagger, or use it as `Authorization: Bearer <token>` in curl.

## 3. See your own profile

`GET /v1/users/me` (auth required).

```bash
curl -s $BASE/v1/users/me -H "Authorization: Bearer $TOKEN"
```

## 4. Update your profile

`PATCH /v1/users/me/profile`

```json
{ "phone": "+919812345678", "date_of_birth": "1992-05-17", "gender": "female", "preferred_language": "hi" }
```

## 5. Search for doctors (public, no auth)

`GET /v1/doctors` with query params. Examples:

```bash
curl -s "$BASE/v1/doctors?specialty=digestive_health&sort=fee_asc&limit=5"
curl -s "$BASE/v1/doctors?q=sharma&language=hi&min_rating=4"
curl -s "$BASE/v1/doctors/specialties"      # the valid specialty values
```

Filters: `q`, `specialty`, `language`, `min_fee`, `max_fee`, `min_rating`,
`available_from`, `available_to`, `sort` (`relevance|rating|fee_asc|fee_desc|experience|next_available`),
`limit`, `offset`. (Run `make seed-load` to get 5,000 sample doctors to search.)

---

## Doctor-side flow (needs a verified doctor + MFA)

### 6. Register + set up a doctor
Register with `"role": "doctor"` (payload like step 1). Log in — the response says
`"mfa_enrollment_required": true`. Enrol MFA:

- `POST /v1/auth/mfa/setup` (Bearer) → returns a `secret` and an `otpauth_uri`. Add the
  secret to an authenticator app (or use the `otpauth_uri` as a QR).
- `POST /v1/auth/mfa/enable` with the current 6-digit code:

```json
{ "code": "123456" }
```

This returns new tokens **and** 10 recovery codes (shown once). Use the new access token for
all doctor actions below.

### 7. Submit the practice profile

`PUT /v1/doctors/me` (doctor + MFA)

```json
{
  "registration_number": "MH-AYU-12345",
  "registration_council": "Maharashtra Council of Indian Medicine",
  "qualification": "BAMS, MD (Kayachikitsa)",
  "years_experience": 12,
  "specialties": ["digestive_health", "stress_sleep"],
  "languages": ["en", "hi", "mr"],
  "fee_paise": 59900,
  "bio": "Classical Ayurveda for gut health and sleep."
}
```

> `fee_paise` is in paise: 59900 = ₹599. After this the profile is `pending`; an **admin**
> approves it with `POST /v1/admin/doctors/{doctor_id}/verification` and body
> `{"decision":"approve"}` before the doctor appears in search or can publish slots.

### 8. Publish availability slots

`POST /v1/doctors/me/slots` (doctor + MFA, needs `Idempotency-Key`). Either a weekly rule…

```json
{
  "mode": "video",
  "rule": {
    "start_date": "2026-10-27",
    "end_date": "2026-10-31",
    "weekdays": [0, 1, 2, 3, 4],
    "day_start": "10:00",
    "day_end": "13:00",
    "slot_minutes": 30,
    "timezone": "Asia/Kolkata"
  }
}
```

…or explicit slots (use future times):

```json
{ "mode": "video", "slots": [
  { "start_at": "2026-10-27T10:00:00+05:30", "end_at": "2026-10-27T10:30:00+05:30" }
] }
```

`mode` is `video`, `audio` or `chat`. See your slots: `GET /v1/doctors/me/slots?from=...&to=...`.

---

## Booking a consultation (patient)

### 9. Book

`POST /v1/consultations` (patient, needs `Idempotency-Key`). Get a `slot_id` from
`GET /v1/doctors/{doctor_id}/slots`.

```json
{
  "slot_id": "PASTE-A-SLOT-UUID",
  "chief_complaint": "Acid reflux and bloating after dinner",
  "consent_to_teleconsultation": true
}
```

Response has the `consultation`, a `payment` (status `created`) and a `checkout_url`. The slot
is held for 10 minutes.

**Test idempotency:** send the exact same request with the **same** `Idempotency-Key` twice —
the second returns the identical response with header `Idempotent-Replayed: true`, and no second
booking is made.

### 10. Pay (dev-only mock gateway)

`POST /v1/payments/mock/{payment_id}/complete` (patient). This simulates the patient paying,
which makes the mock gateway send a signed webhook that confirms the booking.

```json
{ "outcome": "succeeded" }
```

Use `{"outcome":"failed"}` to see the slot released and the consultation cancelled. After
success, `GET /v1/consultations/{id}` shows `status: confirmed`.

### 11. List / view consultations

```bash
curl -s "$BASE/v1/consultations?status=confirmed" -H "Authorization: Bearer $TOKEN"
curl -s "$BASE/v1/consultations/<consultation_id>"  -H "Authorization: Bearer $TOKEN"
```

### 12. Cancel (patient or doctor)

`POST /v1/consultations/{id}/cancel` (needs `Idempotency-Key`)

```json
{ "reason": "plans changed" }
```

Full refund if more than 2h before the start; the slot reopens.

---

## Consultation + prescription (doctor)

With a `confirmed` consultation whose start time is now (within 15 min):

- `POST /v1/consultations/{id}/start` (consulting doctor) → returns a room token.
- `GET  /v1/consultations/{id}/join` (either participant) → room token to join.

### 13. Issue a prescription

`POST /v1/consultations/{id}/prescription` (doctor, needs `Idempotency-Key`). Medicine ids are
fixed for the seeded catalogue — get them from `GET /v1/medicines?q=triphala` (doctor auth).

```json
{
  "diagnosis": "Amlapitta (hyperacidity)",
  "advice": "Avoid spicy food; early, light dinner",
  "items": [
    {
      "medicine_id": "82c59a5b-a4d1-5823-ac75-87aab86d5545",
      "dose": "1 tsp (3 g)",
      "frequency": "twice daily after meals",
      "duration_days": 21,
      "instructions": "With warm water"
    }
  ]
}
```

> `82c59a5b-a4d1-5823-ac75-87aab86d5545` is **Triphala Churna** (List O, allowed any mode).
> To see the TPG rule reject a banned drug, swap in **Agnitundi Vati** (List B):
> `211e4186-5c60-5972-ba13-c30a4cc67250` → you get `422 medicine_not_permitted`.

Response contains the signed `document`, the `signature`, and `product_recommendations`
(Amrutam SKUs). Verify the signature with the public key from
`GET /v1/prescriptions/signing-key`.

### 14. Complete + review

- `POST /v1/consultations/{id}/complete` (doctor): `{ "notes": "Reviewed; Pitta aggravation" }`
- `POST /v1/consultations/{id}/review` (patient, needs `Idempotency-Key`):

```json
{ "rating": 5, "review": "Very helpful consultation." }
```

---

## Admin + privacy

Needs an admin token (MFA-enrolled, created via the CLI).

```bash
curl -s "$BASE/v1/admin/analytics/overview"      -H "Authorization: Bearer $ADMIN"
curl -s "$BASE/v1/admin/analytics/doctors?sort=revenue&limit=10" -H "Authorization: Bearer $ADMIN"
curl -s "$BASE/v1/admin/audit-logs?limit=20"     -H "Authorization: Bearer $ADMIN"
curl -s "$BASE/v1/admin/audit-logs/verify"       -H "Authorization: Bearer $ADMIN"   # audit chain
curl -s "$BASE/v1/admin/outbox?status=dead"      -H "Authorization: Bearer $ADMIN"   # dead-letter queue
```

**DPDP data rights (patient):**

```bash
curl -s "$BASE/v1/users/me/data-export" -H "Authorization: Bearer $TOKEN"   # full export
```

`POST /v1/users/me/erasure` (patient, needs the password again):

```json
{ "password": "Correct-Horse-Battery-9", "confirm": "ERASE MY ACCOUNT" }
```

---

## Things to look for while testing

| Try this | Expected |
|---|---|
| Register with `"accept_terms": false` | `422` (consent required) |
| Register without an `Idempotency-Key` | `400 idempotency_key_required` |
| Book, then re-send with the same key | same response, `Idempotent-Replayed: true` |
| Two patients book the same `slot_id` | one `201`, the other `409 slot_unavailable` |
| Patient opens someone else's `GET /v1/consultations/{id}` | `404` (not `403` — no existence leak) |
| Prescribe a List B medicine | `422 medicine_not_permitted` |
| Any error response | `application/problem+json` with a `code` and `request_id` |

The full automated version of this walk-through is `scripts/e2e_smoke.py` (`make smoke`),
which runs 42 checks end to end.
