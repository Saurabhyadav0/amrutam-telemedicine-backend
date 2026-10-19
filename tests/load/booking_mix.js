// Peak-hour load for 100k consultations/day: ~15 bookings/s and a read-heavy browse mix.
// Run: make load   (k6 in Docker against the compose stack; seed with scripts/seed_search_load.sql)
import http from "k6/http";
import { check } from "k6";

const BASE = __ENV.BASE_URL || "http://localhost:8000";
const READ_RPS = Number(__ENV.READ_RPS || 150);
const BOOK_RPS = Number(__ENV.BOOK_RPS || 15);
const DURATION = __ENV.DURATION || "60s";
const PATIENTS = Number(__ENV.PATIENTS || 60);

// 409 = slot taken / patient time clash: correct behaviour under contention, not an error.
http.setResponseCallback(http.expectedStatuses({ min: 200, max: 299 }, 409));

export const options = {
  scenarios: {
    browse: {
      executor: "constant-arrival-rate", exec: "browse", rate: READ_RPS, timeUnit: "1s",
      duration: DURATION, preAllocatedVUs: 50, maxVUs: 300,
    },
    book: {
      executor: "constant-arrival-rate", exec: "book", rate: BOOK_RPS, timeUnit: "1s",
      duration: DURATION, preAllocatedVUs: 30, maxVUs: 200,
    },
  },
  thresholds: {
    "http_req_duration{kind:read}": ["p(95)<200"],
    "http_req_duration{kind:write}": ["p(95)<500"],
    http_req_failed: ["rate<0.005"],
    checks: ["rate>0.99"],
  },
  summaryTrendStats: ["avg", "p(50)", "p(95)", "p(99)", "max"],
};

const SPECIALTIES = ["kayachikitsa", "panchakarma", "skin_hair", "digestive_health", "womens_health",
  "joint_bone", "stress_sleep", "diabetes_metabolic", "weight_management", "respiratory"];
const LANGS = ["en", "hi", "ta", "te", "mr", "bn"];
const SORTS = ["relevance", "rating", "fee_asc", "next_available"];
const NAMES = ["asha", "bharat", "chitra", "dev", "esha", "iyer", "nair", "patel"];

const pick = (a) => a[Math.floor(Math.random() * a.length)];
const uuid = () => "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
  const r = (Math.random() * 16) | 0;
  return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
});
// Spread synthetic clients over many IPs so per-IP rate limits model real traffic (the stack
// trusts X-Forwarded-For only from configured proxies; compose trusts all for local testing).
const client = () => `10.${(Math.random() * 250) | 0}.${(Math.random() * 250) | 0}.${(Math.random() * 250) | 0}`;
const json = (token) => ({
  "Content-Type": "application/json", "X-Forwarded-For": client(),
  ...(token ? { Authorization: `Bearer ${token}` } : {}),
});

export function setup() {
  const tokens = [];
  for (let i = 0; i < PATIENTS; i++) {
    const email = `k6-${uuid()}@example.com`;
    const password = "K6-Load-Test-Pass-1";
    const reg = http.post(`${BASE}/v1/auth/register`, JSON.stringify({
      email, password, full_name: `Load Patient ${i}`, accept_terms: true,
    }), { headers: { ...json(), "Idempotency-Key": uuid() } });
    if (reg.status !== 201) throw new Error(`register failed: ${reg.status} ${reg.body}`);
    const login = http.post(`${BASE}/v1/auth/login`, JSON.stringify({ email, password }), { headers: json() });
    tokens.push(login.json("tokens.access_token"));
  }
  const doctors = http.get(`${BASE}/v1/doctors?sort=next_available&limit=50`, { headers: json() }).json("items");
  const to = new Date(Date.now() + 7 * 864e5).toISOString();
  let slots = [];
  for (const d of doctors) {
    const r = http.get(`${BASE}/v1/doctors/${d.id}/slots?to=${to}`, { headers: json() });
    slots = slots.concat(r.json().map((s) => s.id));
  }
  if (slots.length < 100) throw new Error(`not enough open slots (${slots.length}); seed load data first`);
  return { tokens, doctorIds: doctors.map((d) => d.id), slots };
}

export function browse(data) {
  const roll = Math.random();
  let res;
  if (roll < 0.7) {
    const q = [];
    if (Math.random() < 0.5) q.push(`specialty=${pick(SPECIALTIES)}`);
    if (Math.random() < 0.3) q.push(`language=${pick(LANGS)}`);
    if (Math.random() < 0.3) q.push(`q=${pick(NAMES)}`);
    q.push(`sort=${pick(SORTS)}`);
    res = http.get(`${BASE}/v1/doctors?${q.join("&")}`, { headers: json(), tags: { kind: "read", name: "search" } });
  } else if (roll < 0.9) {
    res = http.get(`${BASE}/v1/doctors/${pick(data.doctorIds)}`, { headers: json(), tags: { kind: "read", name: "profile" } });
  } else {
    res = http.get(`${BASE}/v1/doctors/${pick(data.doctorIds)}/slots`, { headers: json(), tags: { kind: "read", name: "slots" } });
  }
  check(res, { "read ok": (r) => r.status === 200 });
}

export function book(data) {
  const token = pick(data.tokens);
  const res = http.post(`${BASE}/v1/consultations`, JSON.stringify({
    slot_id: pick(data.slots), consent_to_teleconsultation: true,
  }), { headers: { ...json(token), "Idempotency-Key": uuid() }, tags: { kind: "write", name: "book" } });
  check(res, { "booking accepted or slot taken": (r) => r.status === 201 || r.status === 409 });
  // A random pick can hit a slot this patient already booked: the API resumes that booking
  // (already paid), so only pay for fresh holds.
  if (res.status === 201 && res.json("payment.status") === "created") {
    const paid = http.post(`${BASE}/v1/payments/mock/${res.json("payment.id")}/complete`,
      JSON.stringify({ outcome: "succeeded" }), { headers: json(token), tags: { kind: "write", name: "pay" } });
    check(paid, { "payment confirmed": (r) => r.status === 200 && r.json("status") === "confirmed" });
  }
}
