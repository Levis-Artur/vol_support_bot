"""Read-only database/schema health check. No Telegram requests."""
import asyncio
import asyncpg
from config import Settings


async def check():
    conn = await asyncpg.connect(**Settings.from_env().database, timeout=5)
    try:
        await conn.execute("SELECT group_synced_status,user_notified_status FROM tickets LIMIT 0")
        await conn.execute("SELECT action FROM user_events LIMIT 0")
    finally:
        await conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(check())
    except Exception:
        # Docker stores healthcheck output; do not leak the DSN or driver error.
        raise SystemExit(1) from None
