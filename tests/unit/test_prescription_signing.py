import base64

from app.modules.prescriptions.signing import PrescriptionSigner, canonical, verify

SEED = base64.b64encode(b"s" * 32).decode()
OTHER_SEED = base64.b64encode(b"z" * 32).decode()

DOCUMENT = {
    "prescription_id": "11111111-1111-1111-1111-111111111111",
    "diagnosis": "Amlapitta (hyperacidity)",
    "items": [{"medicine": "Triphala Churna", "dose": "1 tsp", "duration_days": 14}],
    "advice": "Early, light dinner",
}


def test_sign_then_verify_round_trip() -> None:
    signer = PrescriptionSigner(SEED, "rx1")
    content_hash, signature = signer.sign(DOCUMENT)
    assert len(content_hash) == 64  # sha256 hex
    assert verify(signer.public_key_b64, DOCUMENT, signature)


def test_signature_is_deterministic_for_same_document() -> None:
    signer = PrescriptionSigner(SEED, "rx1")
    assert signer.sign(DOCUMENT) == signer.sign(dict(DOCUMENT))


def test_any_field_change_breaks_verification() -> None:
    signer = PrescriptionSigner(SEED, "rx1")
    _, signature = signer.sign(DOCUMENT)
    tampered = {**DOCUMENT, "diagnosis": "Something else"}
    assert not verify(signer.public_key_b64, tampered, signature)


def test_reordered_item_fields_do_not_change_signature() -> None:
    # canonical() sorts keys, so field insertion order is irrelevant.
    signer = PrescriptionSigner(SEED, "rx1")
    _, signature = signer.sign(DOCUMENT)
    reordered = {
        "advice": DOCUMENT["advice"],
        "items": DOCUMENT["items"],
        "diagnosis": DOCUMENT["diagnosis"],
        "prescription_id": DOCUMENT["prescription_id"],
    }
    assert verify(signer.public_key_b64, reordered, signature)


def test_wrong_public_key_fails() -> None:
    signer = PrescriptionSigner(SEED, "rx1")
    _, signature = signer.sign(DOCUMENT)
    other_pub = PrescriptionSigner(OTHER_SEED, "rx2").public_key_b64
    assert not verify(other_pub, DOCUMENT, signature)


def test_canonical_is_sorted_compact_utf8() -> None:
    out = canonical({"b": 1, "a": "आयुर्वेद"})
    assert out == '{"a":"आयुर्वेद","b":1}'.encode()


def test_garbage_signature_returns_false_not_raises() -> None:
    signer = PrescriptionSigner(SEED, "rx1")
    assert verify(signer.public_key_b64, DOCUMENT, base64.b64encode(b"x" * 64).decode()) is False


def test_signer_exposes_key_id() -> None:
    assert PrescriptionSigner(SEED, "rx7").key_id == "rx7"
