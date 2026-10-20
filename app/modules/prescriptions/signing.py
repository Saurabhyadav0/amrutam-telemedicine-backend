"""Ed25519 e-prescription signatures.

The signed message is the canonical JSON of the prescription document (sorted keys, compact
separators, UTF-8). Anyone holding the public key from ``GET /v1/prescriptions/signing-key``
can verify a prescription offline, e.g. a pharmacy. This is the "digital signature or
equivalent electronic authentication" asked for by TPG 2020.
"""

import base64
import hashlib
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()


class PrescriptionSigner:
    def __init__(self, seed_b64: str, key_id: str) -> None:
        self._private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed_b64))
        self.key_id = key_id

    def sign(self, document: dict[str, Any]) -> tuple[str, str]:
        """Returns (sha256 of canonical document, base64 signature)."""
        message = canonical(document)
        return hashlib.sha256(message).hexdigest(), base64.b64encode(
            self._private.sign(message)
        ).decode()

    @property
    def public_key_b64(self) -> str:
        raw = self._private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.b64encode(raw).decode()


def verify(public_key_b64: str, document: dict[str, Any], signature_b64: str) -> bool:
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    try:
        key.verify(base64.b64decode(signature_b64), canonical(document))
    except InvalidSignature:
        return False
    return True
