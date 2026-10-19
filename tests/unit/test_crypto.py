import base64

import pytest

from app.core.crypto import DecryptionError, FieldCipher

K1 = base64.b64encode(b"1" * 32).decode()
K2 = base64.b64encode(b"2" * 32).decode()
BLIND = base64.b64encode(b"x" * 32).decode()


def test_round_trip_and_randomised_nonce() -> None:
    cipher = FieldCipher({"v1": K1}, "v1", BLIND)
    a = cipher.encrypt("+919876543210", aad="profiles.phone:u1")
    b = cipher.encrypt("+919876543210", aad="profiles.phone:u1")
    assert a != b
    assert a.startswith("v1.")
    assert cipher.decrypt(a, aad="profiles.phone:u1") == "+919876543210"


def test_ciphertext_bound_to_row() -> None:
    cipher = FieldCipher({"v1": K1}, "v1", BLIND)
    token = cipher.encrypt("secret", aad="profiles.phone:u1")
    with pytest.raises(DecryptionError):
        cipher.decrypt(token, aad="profiles.phone:u2")


def test_rotation_keeps_old_ciphertexts_readable() -> None:
    old = FieldCipher({"v1": K1}, "v1", BLIND).encrypt("dob", aad="a")
    rotated = FieldCipher({"v1": K1, "v2": K2}, "v2", BLIND)
    assert rotated.decrypt(old, aad="a") == "dob"
    assert rotated.needs_reencryption(old)
    assert not rotated.needs_reencryption(rotated.encrypt("dob", aad="a"))


def test_unknown_key_version_rejected() -> None:
    token = FieldCipher({"v9": K1}, "v9", BLIND).encrypt("x", aad="a")
    with pytest.raises(DecryptionError):
        FieldCipher({"v1": K1}, "v1", BLIND).decrypt(token, aad="a")


def test_blind_index_is_deterministic_and_normalised() -> None:
    cipher = FieldCipher({"v1": K1}, "v1", BLIND)
    assert cipher.blind_index(" +91999 ") == cipher.blind_index("+91999")
    assert cipher.blind_index("+91999") != cipher.blind_index("+91998")
