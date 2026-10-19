"""Writes a local .env with freshly generated secrets. Never commit the result."""

import base64
import json
import secrets
import sys
from pathlib import Path

TEMPLATE = Path(".env.example")
TARGET = Path(".env")


def main() -> None:
    if TARGET.exists() and "--force" not in sys.argv:
        sys.exit(".env already exists (use --force to overwrite)")
    values = {
        "JWT_KEYS": json.dumps({"k1": secrets.token_urlsafe(48)}),
        "DATA_ENCRYPTION_KEYS": json.dumps(
            {"v1": base64.b64encode(secrets.token_bytes(32)).decode()}
        ),
        "BLIND_INDEX_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        "PAYMENT_WEBHOOK_SECRET": secrets.token_urlsafe(48),
        "PRESCRIPTION_SIGNING_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
    }
    lines = []
    for line in TEMPLATE.read_text().splitlines():
        key = line.split("=", 1)[0]
        lines.append(f"{key}='{values[key]}'" if key in values else line)
    TARGET.write_text("\n".join(lines) + "\n")
    print("Wrote .env with new secrets")


if __name__ == "__main__":
    main()
