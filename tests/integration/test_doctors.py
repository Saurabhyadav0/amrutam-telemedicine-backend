from fastapi import FastAPI
from httpx import AsyncClient

from tests.integration.helpers import (
    admin_session,
    bearer,
    doctor_session,
    login,
    practice,
    register,
    unique_token,
    verified_doctor,
)


async def search(client: AsyncClient, **params: object) -> list[dict]:
    resp = await client.get("/v1/doctors", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


async def test_onboarding_requires_doctor_role_and_mfa(client: AsyncClient) -> None:
    patient = await register(client)
    patient_token = (await login(client, patient["email"]))["tokens"]["access_token"]
    resp = await client.put("/v1/doctors/me", json=practice(), headers=bearer(patient_token))
    assert resp.status_code == 403 and resp.json()["code"] == "insufficient_role"

    doctor = await register(client, role="doctor")
    pwd_only = (await login(client, doctor["email"]))["tokens"]["access_token"]
    resp = await client.put("/v1/doctors/me", json=practice(), headers=bearer(pwd_only))
    assert resp.status_code == 403 and resp.json()["code"] == "mfa_required"


async def test_verification_lifecycle_controls_visibility(
    app: FastAPI, client: AsyncClient
) -> None:
    _, admin_token = await admin_session(app, client)
    name = unique_token()
    doctor, token = await doctor_session(client, name=f"Vaidya {name}")

    submitted = await client.put("/v1/doctors/me", json=practice(), headers=bearer(token))
    assert submitted.status_code == 200
    assert submitted.json()["verification_status"] == "pending"
    assert await search(client, q=name) == []
    assert (await client.get(f"/v1/doctors/{doctor['id']}")).status_code == 404

    pending = await client.get(
        "/v1/admin/doctors", params={"status": "pending", "limit": 200}, headers=bearer(admin_token)
    )
    assert doctor["id"] in {d["user_id"] for d in pending.json()["items"]}

    await client.post(
        f"/v1/admin/doctors/{doctor['id']}/verification",
        json={"decision": "approve"},
        headers=bearer(admin_token),
    )
    [card] = await search(client, q=name)
    assert card["registration_number"].startswith("AYU-")  # TPG: shown to patients
    assert (await client.get(f"/v1/doctors/{doctor['id']}")).json()["bio"]

    # Editing credentials on a verified profile requires re-verification.
    again = await client.put(
        "/v1/doctors/me", json=practice(registration_number="AYU-NEW-1"), headers=bearer(token)
    )
    assert again.json()["verification_status"] == "pending"
    assert await search(client, q=name) == []


async def test_rejection_requires_reason(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    doctor, token = await doctor_session(client)
    await client.put("/v1/doctors/me", json=practice(), headers=bearer(token))
    url = f"/v1/admin/doctors/{doctor['id']}/verification"
    assert (
        await client.post(url, json={"decision": "reject"}, headers=bearer(admin_token))
    ).status_code == 422
    rejected = await client.post(
        url,
        json={"decision": "reject", "reason": "Registration not found in NCISM register"},
        headers=bearer(admin_token),
    )
    assert rejected.json()["verification_status"] == "rejected"
    mine = await client.get("/v1/doctors/me", headers=bearer(token))
    assert mine.json()["rejection_reason"].startswith("Registration not found")


async def test_registration_number_is_unique(client: AsyncClient) -> None:
    _, first = await doctor_session(client)
    _, second = await doctor_session(client)
    reg = {"registration_number": f"AYU-{unique_token()}", "registration_council": "ncism"}
    assert (
        await client.put("/v1/doctors/me", json=practice(**reg), headers=bearer(first))
    ).status_code == 200
    dup = await client.put(
        "/v1/doctors/me",
        json=practice(
            **{
                **reg,
                "registration_council": "NCISM",
                "registration_number": reg["registration_number"].lower(),
            }
        ),
        headers=bearer(second),
    )
    assert dup.status_code == 409 and dup.json()["code"] == "registration_taken"


async def test_unknown_vocabulary_rejected(client: AsyncClient) -> None:
    _, token = await doctor_session(client)
    resp = await client.put(
        "/v1/doctors/me", json=practice(specialties=["astrology"]), headers=bearer(token)
    )
    assert resp.status_code == 422
    assert (await client.get("/v1/doctors", params={"specialty": "astrology"})).status_code == 400


async def test_availability_toggle_hides_doctor(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    name = unique_token()
    _, token = await verified_doctor(client, admin_token, name=f"Vaidya {name}")
    assert len(await search(client, q=name)) == 1
    await client.patch(
        "/v1/doctors/me/accepting", json={"is_accepting": False}, headers=bearer(token)
    )
    assert await search(client, q=name) == []


async def test_search_filters_and_sorting(app: FastAPI, client: AsyncClient) -> None:
    _, admin_token = await admin_session(app, client)
    tag = unique_token()
    await verified_doctor(
        client,
        admin_token,
        name=f"Asha {tag}",
        fee_paise=30000,
        specialties=["skin_hair"],
        languages=["hi"],
    )
    await verified_doctor(
        client,
        admin_token,
        name=f"Bharat {tag}",
        fee_paise=80000,
        specialties=["skin_hair", "stress_sleep"],
        languages=["en"],
    )
    await verified_doctor(
        client,
        admin_token,
        name=f"Chitra {tag}",
        fee_paise=50000,
        specialties=["joint_bone"],
        languages=["en", "ta"],
    )

    names = lambda items: [i["display_name"].split()[0] for i in items]  # noqa: E731
    assert names(await search(client, q=tag, sort="fee_asc")) == ["Asha", "Chitra", "Bharat"]
    assert names(await search(client, q=tag, sort="fee_desc")) == ["Bharat", "Chitra", "Asha"]
    assert set(names(await search(client, q=tag, specialty="skin_hair"))) == {"Asha", "Bharat"}
    assert names(await search(client, q=tag, language="ta")) == ["Chitra"]
    assert names(await search(client, q=tag, min_fee=40000, max_fee=60000)) == ["Chitra"]
    # Prefix match on name and specialty words.
    assert names(await search(client, q=f"bhar {tag}")) == ["Bharat"]
    assert set(names(await search(client, q=f"{tag} skin"))) == {"Asha", "Bharat"}

    page = (
        await client.get("/v1/doctors", params={"q": tag, "sort": "fee_asc", "limit": 2})
    ).json()
    assert len(page["items"]) == 2 and page["next_offset"] == 2


async def test_search_is_cached_and_invalidated_on_change(
    app: FastAPI, client: AsyncClient
) -> None:
    _, admin_token = await admin_session(app, client)
    tag = unique_token()
    await verified_doctor(client, admin_token, name=f"First {tag}")
    assert len(await search(client, q=tag)) == 1

    cached_keys = await app.state.redis.keys("cache:doctors:*")
    assert cached_keys, "search result should be cached"

    await verified_doctor(
        client, admin_token, name=f"Second {tag}"
    )  # approval bumps the generation
    assert len(await search(client, q=tag)) == 2


async def test_hostile_search_input_is_harmless(client: AsyncClient) -> None:
    for q in ["':* | !", "a & b | c", "'); DROP TABLE doctors;--", "   ", "ॐ आयुर्वेद"]:
        resp = await client.get("/v1/doctors", params={"q": q})
        assert resp.status_code == 200, q
