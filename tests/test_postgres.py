"""Real PostgreSQL tests; each test gets a disposable schema in an explicitly named test DB."""
import asyncio
import os
import uuid
from urllib.parse import urlsplit
from unittest.mock import AsyncMock

import asyncpg
import pytest

import bot as app
from conftest import ADMIN, TECH, OTHER_TECH, USER, message, callback

pytestmark = pytest.mark.integration


@pytest.fixture
async def postgres(monkeypatch):
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL for a disposable PostgreSQL test database")
    if not urlsplit(dsn).path.rstrip("/").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must name a database ending in _test")
    schema = "test_" + uuid.uuid4().hex
    owner = await asyncpg.connect(dsn)
    pool = None
    try:
        await owner.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5,
            server_settings={"search_path": schema, "timezone": "UTC"})
        monkeypatch.setattr(app, "pool", pool)
        async with pool.acquire() as conn:
            sql = (app.BASE_DIR / "schema.sql").read_text(encoding="utf-8")
            await conn.execute(sql)
            await conn.execute(sql)  # migrations must be repeatable
            for uid in (ADMIN, TECH, OTHER_TECH, USER):
                await conn.execute("INSERT INTO users(telegram_id,verified) VALUES($1,$2)", uid, uid != USER)
        yield pool
    finally:
        if pool:
            await pool.close()
        # schema is generated above, never supplied by a user or read from an existing DB.
        await owner.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await owner.close()


async def create_pending(postgres, state, telegram, api, form_data, mid=101):
    telegram.session.make_request = AsyncMock()
    await state.update_data(**form_data)
    await app.form_submit(message(mid=mid).as_(telegram), state, api)
    async with postgres.acquire() as conn:
        return await conn.fetchval("SELECT max(id) FROM tickets")


async def test_pending_approval_and_delivery(postgres, state, telegram, api, form_data):
    tid = await create_pending(postgres, state, telegram, api, form_data)
    api.send_message.assert_not_awaited()
    await app.publish_ticket(api, tid)
    api.send_message.assert_not_awaited()
    await app.approve(message(ADMIN, f"/approve {USER}").as_(telegram), api)
    async with postgres.acquire() as conn:
        t = await conn.fetchrow("SELECT * FROM tickets WHERE id=$1", tid)
        assert t["status"] == "new" and t["group_message_id"] == 101
        assert await conn.fetchval("SELECT count(*) FROM ticket_events") == 2
        assert await conn.fetchval("SELECT count(*) FROM user_events") == 1


async def test_concurrent_assignment(postgres, state, telegram, api, form_data):
    tid = await create_pending(postgres, state, telegram, api, form_data)
    await app.approve(message(ADMIN, f"/approve {USER}").as_(telegram), api)
    await asyncio.gather(*[app.change_status(callback(uid, f"act:{tid}:take").as_(telegram), api)
                           for uid in (TECH, OTHER_TECH)])
    async with postgres.acquire() as conn:
        t = await conn.fetchrow("SELECT * FROM tickets WHERE id=$1", tid)
        assert t["status"] == "in_progress" and t["assignee_id"] in (TECH, OTHER_TECH)
        assert await conn.fetchval("SELECT count(*) FROM ticket_events WHERE new_status='in_progress'") == 1


async def test_concurrent_publication(postgres, state, telegram, api, form_data):
    tid = await create_pending(postgres, state, telegram, api, form_data)
    async with postgres.acquire() as conn:
        await conn.execute("UPDATE users SET verified=true WHERE telegram_id=$1", USER)
        await conn.execute("UPDATE tickets SET status='new' WHERE id=$1", tid)
    await asyncio.gather(app.publish_ticket(api, tid), app.publish_ticket(api, tid))
    api.send_message.assert_awaited_once()


async def test_daily_limit_and_duplicate(postgres, state, telegram, api, form_data):
    for mid in (101, 101, 102, 103, 104):
        await create_pending(postgres, state, telegram, api, form_data, mid)
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM tickets") == 3
        assert await conn.fetchval("SELECT count(*) FROM ticket_events") == 3


async def test_reject_and_constraints(postgres, state, telegram, api, form_data):
    await create_pending(postgres, state, telegram, api, form_data)
    await app.reject(message(ADMIN, f"/reject {USER}").as_(telegram), api)
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT blocked FROM users WHERE telegram_id=$1", USER)
        assert await conn.fetchval("SELECT status FROM tickets") == "cancelled"
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE tickets SET description='short'")


async def test_delivery_failure_recovery(postgres, state, telegram, api, form_data):
    tid = await create_pending(postgres, state, telegram, api, form_data)
    api.send_message.side_effect = RuntimeError("synthetic network failure")
    await app.approve(message(ADMIN, f"/approve {USER}").as_(telegram), api)
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM tickets") == "new"
        assert await conn.fetchval("SELECT group_message_id FROM tickets") is None
    api.send_message.side_effect = None
    await app.retry_deliveries(api)
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT group_message_id FROM tickets WHERE id=$1", tid) == 101
        assert await conn.fetchval("SELECT user_notified_status FROM tickets") == "new"


async def test_stats_and_queue_sql(postgres, state, telegram, api, form_data):
    await create_pending(postgres, state, telegram, api, form_data)
    await app.stats(message(TECH, "/stats").as_(telegram))
    await app.queue(message(TECH, "/queue").as_(telegram))
    assert telegram.session.make_request.await_args.args[1].text == "Черга порожня."
