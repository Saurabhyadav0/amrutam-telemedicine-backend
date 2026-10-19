"""Writes docs/openapi.yaml from the app. CI fails if the committed file is stale:
uv run python scripts/export_openapi.py --check
"""

import base64
import json
import os
import sys
from pathlib import Path

# Build the app without real secrets: the schema doesn't depend on them.
_DUMMY = {
    "JWT_KEYS": json.dumps({"k1": "x" * 32}),
    "JWT_ACTIVE_KID": "k1",
    "DATA_ENCRYPTION_KEYS": json.dumps({"v1": base64.b64encode(b"x" * 32).decode()}),
    "DATA_ACTIVE_KEY_VERSION": "v1",
    "BLIND_INDEX_KEY": base64.b64encode(b"x" * 32).decode(),
    "PAYMENT_WEBHOOK_SECRET": "x" * 32,
    "PRESCRIPTION_SIGNING_KEY": base64.b64encode(b"x" * 32).decode(),
    "ENVIRONMENT": "local",
}
for key, value in _DUMMY.items():
    os.environ.setdefault(key, value)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from app.main import create_app  # noqa: E402

TARGET = Path("docs/openapi.yaml")


def render() -> str:
    spec = create_app().openapi()
    spec["servers"] = [{"url": "https://api.amrutam.global"}, {"url": "http://localhost:8000"}]
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)


def main() -> None:
    rendered = render()
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text() != rendered:
            sys.exit("docs/openapi.yaml is stale: run `make openapi` and commit the result")
        print("docs/openapi.yaml is up to date")
        return
    TARGET.write_text(rendered)
    print(f"wrote {TARGET} ({len(rendered.splitlines())} lines)")


if __name__ == "__main__":
    main()
