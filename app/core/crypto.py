"""Application-level field encryption for PHI/PII.

Ciphertext format: ``<key_version>.<base64url(nonce || ciphertext || tag)>``.
The version prefix lets old rows decrypt after the active key rotates; a background job
re-encrypts rows whose version is not the active one (see docs/security.md, key rotation).

``aad`` (associated data) binds a ciphertext to its column and row, e.g.
``profiles.phone:<user_id>``, so a ciphertext copied into another row fails to decrypt.

In production the version keys are data keys unwrapped at boot from a KMS-held key
(envelope encryption); the app only ever sees the unwrapped data keys via env/secret store.
"""

import base64
import hashlib
import hmac
import os
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12


class DecryptionError(Exception):
    pass


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class FieldCipher:
    def __init__(self, keys: Mapping[str, str], active_version: str, blind_index_key: str) -> None:
        self._keys = {version: base64.b64decode(key) for version, key in keys.items()}
        self._active = active_version
        self._blind_key = base64.b64decode(blind_index_key)

    @property
    def active_version(self) -> str:
        return self._active

    def encrypt(self, plaintext: str, *, aad: str) -> str:
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(self._keys[self._active]).encrypt(nonce, plaintext.encode(), aad.encode())
        return f"{self._active}.{_b64url_encode(nonce + sealed)}"

    def decrypt(self, token: str, *, aad: str) -> str:
        version, _, payload = token.partition(".")
        key = self._keys.get(version)
        if key is None or not payload:
            raise DecryptionError("Unknown key version")
        raw = _b64url_decode(payload)
        try:
            plain = AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], aad.encode())
        except InvalidTag as exc:
            raise DecryptionError("Ciphertext failed authentication") from exc
        return plain.decode()

    def needs_reencryption(self, token: str) -> bool:
        return token.partition(".")[0] != self._active

    def blind_index(self, value: str) -> str:
        """Deterministic keyed hash for equality lookups/uniqueness on encrypted columns."""
        normalized = value.strip().lower().encode()
        return hmac.new(self._blind_key, normalized, hashlib.sha256).hexdigest()


def sha256_hex(value: str | bytes) -> str:
    data = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()
