"""Read configuration only at startup; never include secret values in errors."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent


def positive_id(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise ValueError("Invalid ID")
    result = int(value)
    if not 0 < result <= 2**63 - 1:
        raise ValueError("Invalid ID")
    return result


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    group_id: int
    admins: frozenset[int]
    it: frozenset[int]
    database: dict = field(repr=False)

    @classmethod
    def from_env(cls):
        load_dotenv(BASE_DIR / ".env", override=False)
        token = os.getenv("BOT_TOKEN", "")
        try:
            validate_token(token)
            if "REPLACE" in token:
                raise ValueError
        except (TokenValidationError, ValueError):
            raise ValueError("Set a valid BOT_TOKEN") from None
        try:
            group_id = int(os.getenv("IT_GROUP_ID", ""))
            if not -(2**63) < group_id < 0:
                raise ValueError
        except ValueError:
            raise ValueError("IT_GROUP_ID must be a negative group ID") from None
        roles = {}
        for key in ("ADMIN_IDS", "IT_IDS"):
            try:
                roles[key] = frozenset(positive_id(s.strip()) for s in os.getenv(key, "").split(",") if s.strip())
            except ValueError:
                raise ValueError(f"{key} must contain positive numeric IDs") from None
        if not roles["ADMIN_IDS"]:
            raise ValueError("Set ADMIN_IDS")
        dsn = os.getenv("DATABASE_URL")
        if dsn:
            if not dsn.startswith(("postgresql://", "postgres://")):
                raise ValueError("DATABASE_URL must be a PostgreSQL URL")
            database = {"dsn": dsn}
        else:
            password = os.getenv("DB_PASSWORD", "")
            if not password or "REPLACE" in password:
                raise ValueError("Set DB_PASSWORD or DATABASE_URL")
            try:
                port = int(os.getenv("DB_PORT", "5432"))
                if not 1 <= port <= 65535:
                    raise ValueError
            except ValueError:
                raise ValueError("Invalid DB_PORT") from None
            database = dict(host=os.getenv("DB_HOST", "localhost"), port=port,
                            user=os.getenv("DB_USER", "support"),
                            database=os.getenv("DB_NAME", "support"), password=password)
        return cls(token, group_id, roles["ADMIN_IDS"], roles["IT_IDS"] | roles["ADMIN_IDS"], database)
