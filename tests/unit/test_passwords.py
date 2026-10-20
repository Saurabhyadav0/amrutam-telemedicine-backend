from app.core.security import PasswordService

# asyncio_mode=auto in pyproject.toml runs async tests without a marker

PASSWORD = "Correct-Horse-Battery-9"


async def test_hash_is_salted_and_verifiable() -> None:
    svc = PasswordService()
    h1 = await svc.hash(PASSWORD)
    h2 = await svc.hash(PASSWORD)
    assert h1 != h2  # unique salt per hash
    assert h1.startswith("$argon2id$")
    assert await svc.verify(h1, PASSWORD) is True
    assert await svc.verify(h2, PASSWORD) is True


async def test_wrong_password_fails() -> None:
    svc = PasswordService()
    h = await svc.hash(PASSWORD)
    assert await svc.verify(h, "wrong-password-xyz") is False


async def test_verify_never_raises_on_malformed_hash() -> None:
    svc = PasswordService()
    assert await svc.verify("not-a-real-hash", PASSWORD) is False
    assert await svc.verify("", PASSWORD) is False


async def test_verify_dummy_runs_without_error() -> None:
    # Used so an unknown email spends the same time as a real verification (timing defence).
    svc = PasswordService()
    await svc.verify_dummy(PASSWORD)  # must not raise


async def test_current_hash_does_not_need_rehash() -> None:
    svc = PasswordService()
    assert svc.needs_rehash(await svc.hash(PASSWORD)) is False
