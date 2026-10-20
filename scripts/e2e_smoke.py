"""End-to-end smoke test against a running stack (default: the local docker compose gateway).

    uv run python scripts/e2e_smoke.py            # creates its own admin via the CLI in Docker
    BASE_URL=https://staging.example.com uv run python scripts/e2e_smoke.py --no-docker

Exercises every core flow through the public API and prints a pass/fail checklist. Exit code
is non-zero if any check fails. Uses unique emails, so it can run repeatedly.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pyotp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
USE_DOCKER = "--no-docker" not in sys.argv
PASSWORD = "Smoke-Test-Pass-2026"  # gitleaks:allow - throwaway test-account password
results: list[tuple[str, bool, str]] = []
# Each run looks like a new client IP so per-IP signup limits (10/hour) don't trip on repeat
# runs. Only honoured where the proxy is trusted (local compose); in AWS the API trusts
# X-Forwarded-For from the ALB subnet only, so real clients can't spoof their way past limits.
RUN_IP = f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
client = httpx.Client(base_url=BASE, timeout=30, headers={"X-Forwarded-For": RUN_IP})


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}{f'  ({detail})' if detail else ''}")
    return ok


def section(title: str) -> None:
    print(f"\n▶ {title}")


def idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def at(hours: float) -> str:
    return (datetime.now(UTC).replace(microsecond=0) + timedelta(hours=hours)).isoformat()


def register(role: str, name: str) -> tuple[str, str]:
    email = f"smoke-{uuid.uuid4().hex[:10]}@example.com"
    resp = client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": name,
            "role": role,
            "accept_terms": True,
        },
        headers=idem(),
    )
    resp.raise_for_status()
    return resp.json()["id"], email


def login(email: str) -> dict[str, Any]:
    resp = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    resp.raise_for_status()
    return resp.json()


def enrol_mfa(token: str) -> tuple[str, str]:
    secret = client.post("/v1/auth/mfa/setup", headers=auth(token)).json()["secret"]
    enabled = client.post(
        "/v1/auth/mfa/enable", json={"code": pyotp.TOTP(secret).now()}, headers=auth(token)
    ).json()
    return secret, enabled["tokens"]["access_token"]


def create_admin() -> str:
    email = f"smoke-admin-{uuid.uuid4().hex[:8]}@example.com"
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "app.cli",
            "create-admin",
            "--email",
            email,
        ],
        input=PASSWORD + "\n",
        text=True,
        check=True,
        capture_output=True,
    )
    _, token = enrol_mfa(login(email)["tokens"]["access_token"])
    return token


def run(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as exc:
        check(f"{name} raised", False, f"{type(exc).__name__}: {exc}")


state: dict[str, Any] = {}


def platform() -> None:
    ready = client.get("/health/ready")
    check("readiness: postgres + redis", ready.status_code == 200, ready.text)
    headers = client.get("/health/live").headers
    check(
        "security headers present",
        headers.get("x-content-type-options") == "nosniff"
        and "strict-transport-security" in headers
        and headers.get("cache-control") == "no-store",
    )
    check("OpenAPI served", "/v1/consultations" in client.get("/openapi.json").json()["paths"])


def identity() -> None:
    pid, pemail = register("patient", "Rohan Verma")
    body = login(pemail)
    check("patient login (no MFA needed)", body["mfa_required"] is False)
    state["patient"] = (pid, body["tokens"]["access_token"], body["tokens"]["refresh_token"])
    rotated = client.post(
        "/v1/auth/refresh", json={"refresh_token": body["tokens"]["refresh_token"]}
    )
    replay = client.post(
        "/v1/auth/refresh", json={"refresh_token": body["tokens"]["refresh_token"]}
    )
    check(
        "refresh rotation + reuse detection",
        rotated.status_code == 200 and replay.status_code == 401,
    )
    state["patient"] = (pid, login(pemail)["tokens"]["access_token"], None)

    did, demail = register("doctor", "Vaidya Meera Kulkarni")
    dlogin = login(demail)
    check("doctor told to enrol MFA", dlogin["mfa_enrollment_required"] is True)
    blocked = client.get("/v1/doctors/me", headers=auth(dlogin["tokens"]["access_token"]))
    check(
        "doctor blocked until MFA",
        blocked.status_code == 403 and blocked.json()["code"] == "mfa_required",
    )
    secret, dtoken = enrol_mfa(dlogin["tokens"]["access_token"])
    challenge = login(demail)
    verify = client.post(
        "/v1/auth/mfa/verify",
        json={
            "mfa_token": challenge["mfa_token"],
            "code": pyotp.TOTP(secret).at(int(time.time()) + 30),
        },
    )
    check("MFA login challenge", challenge["mfa_required"] and verify.status_code == 200)
    state["doctor"] = (did, dtoken)

    if USE_DOCKER:
        state["admin"] = create_admin()
        check(
            "admin via CLI + MFA",
            client.get("/v1/admin/users", headers=auth(state["admin"])).status_code == 200,
        )
    check(
        "patient denied admin API",
        client.get("/v1/admin/users", headers=auth(state["patient"][1])).status_code == 403,
    )


def doctors() -> None:
    did, dtoken = state["doctor"]
    reg = f"MH-AYU-{uuid.uuid4().hex[:6]}"
    profile = client.put(
        "/v1/doctors/me",
        headers=auth(dtoken),
        json={
            "registration_number": reg,
            "registration_council": "Maharashtra Council of Indian Medicine",
            "qualification": "BAMS, MD (Kayachikitsa)",
            "years_experience": 12,
            "specialties": ["digestive_health", "stress_sleep"],
            "languages": ["en", "hi", "mr"],
            "fee_paise": 59900,
            "bio": "Gut health and sleep.",
        },
    )
    check(
        "doctor profile submitted (pending)", profile.json().get("verification_status") == "pending"
    )
    early = client.post(
        "/v1/doctors/me/slots",
        json={"slots": [{"start_at": at(30), "end_at": at(30.5)}]},
        headers={**auth(dtoken), **idem()},
    )
    check("unverified doctor cannot publish slots", early.status_code == 403)
    if "admin" in state:
        approve = client.post(
            f"/v1/admin/doctors/{did}/verification",
            json={"decision": "approve"},
            headers=auth(state["admin"]),
        )
        check(
            "admin verifies registration", approve.json().get("verification_status") == "verified"
        )
    start = date.today() + timedelta(days=2)
    rule = client.post(
        "/v1/doctors/me/slots",
        headers={**auth(dtoken), **idem()},
        json={
            "rule": {
                "start_date": str(start),
                "end_date": str(start + timedelta(days=6)),
                "weekdays": [0, 1, 2, 3, 4],
                "day_start": "10:00",
                "day_end": "12:00",
                "slot_minutes": 30,
                "timezone": "Asia/Kolkata",
            }
        },
    ).json()
    check("weekly rule → slots (IST)", rule["created"] == 20, f"{rule['created']} created")
    again = client.post(
        "/v1/doctors/me/slots",
        headers={**auth(dtoken), **idem()},
        json={
            "slots": [
                {"start_at": rule["slots"][0]["start_at"], "end_at": rule["slots"][0]["end_at"]}
            ]
        },
    ).json()
    check(
        "overlapping slot rejected by DB",
        again["created"] == 0 and again["skipped_overlapping"] == 1,
    )
    near = client.post(
        "/v1/doctors/me/slots",
        headers={**auth(dtoken), **idem()},
        json={
            "slots": [
                {"start_at": at(10 / 60), "end_at": at(40 / 60)},
                {"start_at": at(26), "end_at": at(26.5)},
            ]
        },
    ).json()
    state["slots"] = {
        "near": near["slots"][0]["id"],
        "far": near["slots"][1]["id"],
        "rule": [s["id"] for s in rule["slots"]],
    }
    found = client.get(
        "/v1/doctors", params={"q": "meera", "specialty": "digestive_health"}
    ).json()["items"]
    check("search finds verified doctor", any(d["id"] == did for d in found))
    # Registration numbers are normalised to upper case on input.
    check(
        "registration number shown to patients (TPG)",
        any(d.get("registration_number") == reg.upper() for d in found),
    )


def booking() -> None:
    _, ptoken, _ = state["patient"]
    slot = state["slots"]["near"]
    payload = {
        "slot_id": slot,
        "consent_to_teleconsultation": True,
        "chief_complaint": "Acid reflux at night",
    }
    key = idem()
    first = client.post("/v1/consultations", json=payload, headers={**auth(ptoken), **key})
    replay = client.post("/v1/consultations", json=payload, headers={**auth(ptoken), **key})
    check(
        "booking held, payment created",
        first.status_code == 201 and first.json()["payment"]["status"] == "created",
    )
    check(
        "idempotent replay (same key)",
        replay.headers.get("idempotent-replayed") == "true"
        and replay.json()["consultation"]["id"] == first.json()["consultation"]["id"],
    )
    booked = first.json()
    state["consultation"] = booked["consultation"]["id"]

    others = [register("patient", f"Rival {i}") for i in range(4)]
    tokens = [login(e)["tokens"]["access_token"] for _, e in others]
    contested = state["slots"]["rule"][0]
    with ThreadPoolExecutor(4) as pool:
        codes = list(
            pool.map(
                lambda t: (
                    client.post(
                        "/v1/consultations",
                        json={"slot_id": contested, "consent_to_teleconsultation": True},
                        headers={**auth(t), **idem()},
                    ).status_code
                ),
                tokens,
            )
        )
    check(
        "4 concurrent bookings → exactly 1 wins",
        sorted(codes) == [201, 409, 409, 409],
        str(sorted(codes)),
    )

    pay = client.post(
        f"/v1/payments/mock/{booked['payment']['id']}/complete",
        json={"outcome": "succeeded"},
        headers=auth(ptoken),
    ).json()
    check("signed webhook confirms booking", pay == {"status": "confirmed"})
    forged = client.post(
        "/v1/payments/webhooks/mockpay",
        content=b'{"id":"x"}',
        headers={"X-MockPay-Signature": "t=1,v1=deadbeef"},
    )
    check("forged webhook rejected", forged.status_code == 401)
    stranger = client.get(f"/v1/consultations/{state['consultation']}", headers=auth(tokens[0]))
    check("other patient gets 404 (BOLA)", stranger.status_code == 404)


def consultation_and_rx() -> None:
    _, dtoken = state["doctor"]
    _, ptoken, _ = state["patient"]
    cid = state["consultation"]
    started = client.post(f"/v1/consultations/{cid}/start", headers=auth(dtoken))
    check(
        "doctor starts consult (room token)", started.status_code == 200 and started.json()["token"]
    )
    meds = {m["name"]: m["id"] for m in client.get("/v1/medicines", headers=auth(dtoken)).json()}

    def rx_item(name: str) -> dict[str, Any]:
        return {
            "medicine_id": meds[name],
            "dose": "3 g",
            "frequency": "after meals",
            "duration_days": 21,
        }

    blocked = client.post(
        f"/v1/consultations/{cid}/prescription",
        headers={**auth(dtoken), **idem()},
        json={"diagnosis": "Amlapitta", "items": [rx_item("Agnitundi Vati")]},
    )
    check(
        "TPG List B medicine blocked",
        blocked.status_code == 422 and blocked.json()["code"] == "medicine_not_permitted",
    )
    rx = client.post(
        f"/v1/consultations/{cid}/prescription",
        headers={**auth(dtoken), **idem()},
        json={
            "diagnosis": "Amlapitta",
            "advice": "Early, light dinner",
            "items": [rx_item("Avipattikar Churna"), rx_item("Triphala Churna")],
        },
    ).json()
    from app.modules.prescriptions.signing import verify

    key = client.get("/v1/prescriptions/signing-key").json()
    check(
        "prescription Ed25519 signature verifies",
        verify(key["public_key"], rx["document"], rx["signature"]),
    )
    check(
        "Amrutam product recommendation",
        [m["amrutam_sku"] for m in rx["product_recommendations"]] == ["AMR-TRIPHALA"],
    )
    done = client.post(f"/v1/consultations/{cid}/complete", json={}, headers=auth(dtoken)).json()
    review = client.post(
        f"/v1/consultations/{cid}/review", json={"rating": 5}, headers={**auth(ptoken), **idem()}
    ).json()
    check("complete + review", done["status"] == "completed" and review["rating"] == 5)

    far = client.post(
        "/v1/consultations",
        json={"slot_id": state["slots"]["far"], "consent_to_teleconsultation": True},
        headers={**auth(ptoken), **idem()},
    ).json()
    client.post(
        f"/v1/payments/mock/{far['payment']['id']}/complete",
        json={"outcome": "succeeded"},
        headers=auth(ptoken),
    )
    cancel = client.post(
        f"/v1/consultations/{far['consultation']['id']}/cancel",
        json={"reason": "plans changed"},
        headers={**auth(ptoken), **idem()},
    ).json()
    check("cancel >2h ahead → full refund", cancel["refund_paise"] == 59900)
    state["refund_payment"] = far["payment"]["id"]


def worker_and_admin() -> None:
    if not USE_DOCKER:
        return
    deadline = time.time() + 20
    status = ""
    while time.time() < deadline:
        # psql variable binding (:'id'): the id is quoted by psql, never spliced into SQL.
        status = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "amrutam",
                "-d",
                "amrutam",
                "-At",
                "-v",
                f"id={state['refund_payment']}",
            ],
            input="SELECT status FROM payments WHERE id = :'id';",
            capture_output=True,
            text=True,
        ).stdout.strip()
        if status == "refunded":
            break
        time.sleep(2)
    check("worker processed refund via outbox", status == "refunded", status)
    logs = subprocess.run(
        ["docker", "compose", "logs", "worker", "--no-log-prefix"], capture_output=True, text=True
    ).stdout
    check(
        "worker sent notifications",
        "consultation_confirmed" in logs and "consultation_cancelled" in logs,
    )

    admin = auth(state["admin"])
    refresh = client.post(
        "/v1/admin/analytics/refresh",
        headers=admin,
        json={
            "from_day": str(date.today() - timedelta(days=1)),
            "to_day": str(date.today() + timedelta(days=10)),
        },
    )
    time.sleep(6)
    overview = client.get(
        "/v1/admin/analytics/overview",
        params={"to": str(date.today() + timedelta(days=10))},
        headers=admin,
    ).json()
    check(
        "analytics refreshed by worker",
        refresh.status_code == 202 and overview["consultations_booked"] >= 2,
        f"booked={overview['consultations_booked']} net={overview['net_revenue_paise']}",
    )
    chain = client.get("/v1/admin/audit-logs/verify", headers=admin).json()
    check("audit hash chain valid", chain["valid"], f"{chain['checked']} entries")
    dlq = client.get("/v1/admin/outbox", headers=admin).json()
    check("dead-letter queue empty", dlq == [])


def privacy() -> None:
    _, ptoken, _ = state["patient"]
    export = client.get("/v1/users/me/data-export", headers=auth(ptoken)).json()
    check(
        "DPDP export has consults + signed Rx",
        len(export["consultations"]) == 2 and len(export["prescriptions"]) == 1,
    )
    _, throwaway = register("patient", "Temp User")
    ttoken = login(throwaway)["tokens"]["access_token"]
    erased = client.post(
        "/v1/users/me/erasure",
        json={"password": PASSWORD, "confirm": "ERASE MY ACCOUNT"},
        headers=auth(ttoken),
    )
    after = client.post("/v1/auth/login", json={"email": throwaway, "password": PASSWORD})
    check("DPDP erasure closes account", erased.status_code == 204 and after.status_code == 401)


def observability() -> None:
    if not USE_DOCKER:
        return
    time.sleep(16)  # one Prometheus scrape interval
    targets = httpx.get("http://localhost:9090/api/v1/targets").json()["data"]["activeTargets"]
    up = [t for t in targets if t["health"] == "up"]
    check(
        "Prometheus scraping API replicas + worker",
        len(up) == len(targets) and len(up) >= 2,
        f"{len(up)}/{len(targets)} up",
    )
    rules = httpx.get("http://localhost:9090/api/v1/rules").json()["data"]["groups"]
    check("alert rules loaded", sum(len(g["rules"]) for g in rules) == 17)
    bookings = httpx.get(
        "http://localhost:9090/api/v1/query", params={"query": "sum(bookings_total)"}
    ).json()
    check("business metrics exported", bool(bookings["data"]["result"]))
    boards = httpx.get("http://localhost:3300/api/search", params={"query": "Amrutam"}).json()
    check("Grafana dashboards provisioned", len([b for b in boards if b["type"] == "dash-db"]) == 2)
    logs = httpx.get(
        "http://localhost:3100/loki/api/v1/query_range",
        params={"query": '{service="api"} |= "http_request"', "limit": 5},
    ).json()
    check("API logs in Loki", bool(logs["data"]["result"]))
    traces = httpx.get("http://localhost:16686/api/services").json()["data"] or []
    check("traces in Jaeger", "amrutam-telemed" in traces)


if __name__ == "__main__":
    print(f"E2E smoke test against {BASE}")
    for title, fn in [
        ("Platform", platform),
        ("Identity, MFA, RBAC", identity),
        ("Doctors, slots, search", doctors),
        ("Booking saga + payments", booking),
        ("Consultation + prescription", consultation_and_rx),
        ("Worker, analytics, audit", worker_and_admin),
        ("Privacy (DPDP)", privacy),
        ("Observability", observability),
    ]:
        section(title)
        run(title, fn)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print(json.dumps([f"{n}: {d}" for n, _, d in failed], indent=2))
    sys.exit(1 if failed else 0)
