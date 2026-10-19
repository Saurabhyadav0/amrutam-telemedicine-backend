"""Operational commands. Usage: ``python -m app.cli create-admin --email admin@example.com``."""

import argparse
import asyncio
import getpass
import sys

from app.core.config import get_settings
from app.core.db import Database
from app.core.deps import get_field_cipher, get_password_service, get_token_service
from app.core.errors import AppError
from app.core.rate_limit import RateLimiter
from app.modules.auth.schemas import RegisterRequest
from app.modules.auth.service import AuthService, NewAccount
from app.modules.users.models import UserRole


async def create_admin(email: str, full_name: str, password: str) -> None:
    # Reuse the public password policy so admins can't have weaker passwords than patients.
    RegisterRequest.model_validate(
        {"email": email, "password": password, "full_name": full_name, "accept_terms": True}
    )
    settings = get_settings()
    db = Database(settings.database_url, pool_size=1, max_overflow=0)
    try:
        async with db.sessionmaker() as session:
            service = AuthService(
                session,
                settings=settings,
                tokens=get_token_service(),
                cipher=get_field_cipher(),
                passwords=get_password_service(),
                rate_limiter=RateLimiter(redis=None),  # type: ignore[arg-type]  # unused here
            )
            user = await service.create_account(
                NewAccount(
                    email=email, password=password, full_name=full_name, role=UserRole.ADMIN
                ),
                ctx=None,
            )
            print(f"Created admin {user.id}. They must enrol MFA before using admin endpoints.")
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin")
    admin.add_argument("--email", required=True)
    admin.add_argument("--full-name", default="Platform Admin")
    args = parser.parse_args()

    if args.command == "create-admin":
        # Interactive prompt in a terminal; piped stdin for automation (never a CLI argument,
        # which would leak into shell history and the process list).
        password = (
            getpass.getpass("Password (min 12 chars): ")
            if sys.stdin.isatty()
            else sys.stdin.readline().rstrip("\n")
        )
        try:
            asyncio.run(create_admin(args.email, args.full_name, password))
        except (AppError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
