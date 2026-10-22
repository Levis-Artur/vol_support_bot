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
async def legacy_postgres(monkeypatch):
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
            sql = (app.BASE_DIR / "tests/fixtures/schema_legacy.sql").read_text(encoding="utf-8")
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


@pytest.fixture
async def postgres(legacy_postgres):
    async with legacy_postgres.acquire() as conn, conn.transaction():
        await app.initialize_database(conn)
    return legacy_postgres


async def create_ticket(postgres, state, telegram, api, form_data, mid=101):
    telegram.session.make_request = AsyncMock()
    await state.update_data(**form_data)
    await app.form_submit(message(mid=mid).as_(telegram), state, api)
    async with postgres.acquire() as conn:
        return await conn.fetchval("SELECT max(id) FROM tickets")


async def test_public_creation_and_delivery(postgres, state, telegram, api, form_data):
    tid = await create_ticket(postgres, state, telegram, api, form_data)
    api.send_message.assert_awaited_once()
    async with postgres.acquire() as conn:
        t = await conn.fetchrow("SELECT * FROM tickets WHERE id=$1", tid)
        assert t["status"] == "new" and t["group_message_id"] == 101
        assert not await conn.fetchval("SELECT verified FROM users WHERE telegram_id=$1", USER)
        assert await conn.fetchval("SELECT count(*) FROM ticket_events") == 1
        assert await conn.fetchval("SELECT count(*) FROM user_events") == 0


async def test_concurrent_assignment(postgres, state, telegram, api, form_data):
    tid = await create_ticket(postgres, state, telegram, api, form_data)
    await asyncio.gather(*[app.change_status(callback(uid, f"act:{tid}:take").as_(telegram), api)
                           for uid in (TECH, OTHER_TECH)])
    async with postgres.acquire() as conn:
        t = await conn.fetchrow("SELECT * FROM tickets WHERE id=$1", tid)
        assert t["status"] == "in_progress" and t["assignee_id"] in (TECH, OTHER_TECH)
        assert await conn.fetchval("SELECT count(*) FROM ticket_events WHERE new_status='in_progress'") == 1


async def test_concurrent_publication(postgres, api):
    async with postgres.acquire() as conn:
        tid = await legacy_ticket(conn, status="new")
    await asyncio.gather(app.publish_ticket(api, tid), app.publish_ticket(api, tid))
    api.send_message.assert_awaited_once()


async def test_daily_limit_and_duplicate(postgres, state, telegram, api, form_data):
    for mid in (101, 101, 102, 103, 104, 105, 106):
        await create_ticket(postgres, state, telegram, api, form_data, mid)
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM tickets") == 5
        assert await conn.fetchval("SELECT count(*) FROM ticket_events") == 5


async def test_limit_expires_after_24_hours(postgres, state, telegram, api, form_data):
    async with postgres.acquire() as conn:
        for _ in range(5):
            await legacy_ticket(conn, status="new")
        await conn.execute("UPDATE tickets SET created_at=now()-interval '25 hours'")
    await create_ticket(postgres, state, telegram, api, form_data)
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM tickets") == 6


async def test_preemptive_block_survives_registration(postgres, telegram, state, api):
    telegram.session.make_request = AsyncMock()
    fresh = USER + 8
    for _ in range(2):
        await app.block(message(ADMIN, f"/block {fresh}").as_(telegram), api)
    await app.start(message(fresh, "/start").as_(telegram), state)
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT blocked FROM users WHERE telegram_id=$1", fresh)
        assert await conn.fetchval("SELECT count(*) FROM user_events WHERE user_id=$1", fresh) == 1
    assert "обмежено" in telegram.session.make_request.await_args.args[1].text


async def test_block_unblock_and_constraints(postgres, state, telegram, api, form_data):
    await create_ticket(postgres, state, telegram, api, form_data)
    await app.block(message(ADMIN, f"/block {USER}").as_(telegram), api)
    await create_ticket(postgres, state, telegram, api, form_data, mid=102)
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT blocked FROM users WHERE telegram_id=$1", USER)
        assert await conn.fetchval("SELECT count(*) FROM tickets") == 1
        assert await conn.fetchval("SELECT status FROM tickets") == "new"
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE tickets SET description='short'")
    await app.unblock(message(ADMIN, f"/unblock {USER}").as_(telegram), api)
    await create_ticket(postgres, state, telegram, api, form_data, mid=103)
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM tickets") == 2
        assert [r["action"] for r in await conn.fetch("SELECT action FROM user_events ORDER BY id")] == ["block", "unblock"]


async def test_delivery_failure_recovery(postgres, state, telegram, api, form_data):
    api.send_message.side_effect = RuntimeError("synthetic network failure")
    tid = await create_ticket(postgres, state, telegram, api, form_data)
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM tickets") == "new"
        assert await conn.fetchval("SELECT group_message_id FROM tickets") is None
    api.send_message.side_effect = None
    await app.retry_deliveries(api)
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT group_message_id FROM tickets WHERE id=$1", tid) == 101
        assert await conn.fetchval("SELECT user_notified_status FROM tickets") == "new"


async def test_stats_and_queue_sql(postgres, state, telegram, api, form_data):
    await create_ticket(postgres, state, telegram, api, form_data)
    await app.stats(message(TECH, "/stats").as_(telegram))
    await app.queue(message(TECH, "/queue").as_(telegram))
    assert "Нова" in telegram.session.make_request.await_args.args[1].text


async def legacy_ticket(conn, uid=USER, status="pending_verification"):
    return await conn.fetchval("""INSERT INTO tickets(creator_id,location,category,description,photo_file_id,status)
        VALUES($1,'Тестове місце','ПЗ','Синтетична несправність','synthetic-photo',$2) RETURNING id""", uid, status)


async def test_migration_preserves_data_blocks_and_audit(legacy_postgres, api):
    async with legacy_postgres.acquire() as conn:
        blocked_user = USER + 1
        await conn.execute("INSERT INTO users(telegram_id,blocked) VALUES($1,true)", blocked_user)
        tid = await legacy_ticket(conn)
        blocked_tid = await legacy_ticket(conn, blocked_user)
        closed_tid = await legacy_ticket(conn, status="cancelled")
        before = dict(await conn.fetchrow("SELECT * FROM tickets WHERE id=$1", tid))
        async with conn.transaction():
            await app.initialize_database(conn)
        after = dict(await conn.fetchrow("SELECT * FROM tickets WHERE id=$1", tid))
        for key in before.keys() - {"status", "updated_at", "next_delivery_at"}:
            assert before[key] == after[key], key
        assert after["status"] == "new"
        assert after["next_delivery_at"] > after["updated_at"]
        assert await conn.fetchval("SELECT status FROM tickets WHERE id=$1", blocked_tid) == "pending_verification"
        assert await conn.fetchval("SELECT status FROM tickets WHERE id=$1", closed_tid) == "cancelled"
        assert await conn.fetchval("SELECT blocked FROM users WHERE telegram_id=$1", blocked_user)
        events = await conn.fetch("SELECT * FROM ticket_events")
        assert len(events) == 1 and events[0]["ticket_id"] == tid
        assert events[0]["actor_id"] is None
        assert (events[0]["old_status"], events[0]["new_status"]) == ("pending_verification", "new")
        async with conn.transaction():
            await app.initialize_database(conn)
        assert dict(await conn.fetchrow("SELECT * FROM tickets WHERE id=$1", tid)) == after
        assert await conn.fetchval("SELECT count(*) FROM ticket_events") == 1
    await app.retry_deliveries(api)
    api.send_message.assert_not_awaited()


async def test_legacy_backlog_delivered_in_batches(legacy_postgres, api):
    async with legacy_postgres.acquire() as conn:
        for _ in range(12):
            await legacy_ticket(conn)
        async with conn.transaction():
            await app.initialize_database(conn)
        assert await conn.fetchval("SELECT count(DISTINCT next_delivery_at) FROM tickets") == 12
        await conn.execute("UPDATE tickets SET next_delivery_at=now()-interval '1 second'")
    assert await app.retry_deliveries(api) == 5
    async with legacy_postgres.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM tickets WHERE group_message_id IS NOT NULL") == 5
        assert await conn.fetchval("SELECT count(*) FROM tickets WHERE group_message_id IS NULL") == 7
    assert await app.retry_deliveries(api) == 5


async def test_unblock_releases_legacy_only_once(legacy_postgres, telegram, api):
    telegram.session.make_request = AsyncMock()
    async with legacy_postgres.acquire() as conn:
        await conn.execute("UPDATE users SET blocked=true WHERE telegram_id=$1", USER)
        tid = await legacy_ticket(conn)
        async with conn.transaction():
            await app.initialize_database(conn)
    await app.unblock(message(ADMIN, f"/unblock {USER}").as_(telegram), api)
    await app.unblock(message(ADMIN, f"/unblock {USER}").as_(telegram), api)
    async with legacy_postgres.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM tickets WHERE id=$1", tid) == "new"
        assert await conn.fetchval("SELECT count(*) FROM ticket_events") == 1
        assert await conn.fetchval("SELECT actor_id FROM ticket_events") == ADMIN
        assert await conn.fetchval("SELECT count(*) FROM user_events") == 1
        assert await conn.fetchval("SELECT group_message_id FROM tickets") is None


async def test_start_registers_user_and_keeps_blocks(postgres, telegram, state):
    telegram.session.make_request = AsyncMock()
    fresh = USER + 5
    await app.start(message(fresh, "/start").as_(telegram), state)
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT telegram_id FROM users WHERE telegram_id=$1", fresh) == fresh
        await conn.execute("UPDATE users SET blocked=true WHERE telegram_id=$1", TECH)
        async with conn.transaction():
            await app.initialize_database(conn)
        assert await conn.fetchval("SELECT blocked FROM users WHERE telegram_id=$1", TECH)
    await app.start(message(TECH, "/start").as_(telegram), state)
    assert "обмежено" in telegram.session.make_request.await_args.args[1].text


async def test_outsider_cannot_change_status_or_access_other_tickets(postgres, state, telegram, api, form_data):
    tid = await create_ticket(postgres, state, telegram, api, form_data)
    await app.change_status(callback(USER, f"act:{tid}:take").as_(telegram), api)
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM tickets WHERE id=$1", tid) == "new"
    api.send_message.reset_mock()
    await app.show_my(api, OTHER_TECH)
    assert "ще немає" in api.send_message.await_args.args[1]


async def test_concurrent_submissions_enforce_five(postgres, telegram, api, form_data):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    async def submit(mid):
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=ADMIN, chat_id=USER, user_id=USER))
        await create_ticket(postgres, state, telegram, api, form_data, mid)

    await asyncio.gather(*(submit(mid) for mid in range(201, 209)))
    async with postgres.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM tickets") == 5


async def test_migration_rollback_on_audit_failure(legacy_postgres):
    async with legacy_postgres.acquire() as conn:
        tid = await legacy_ticket(conn)
        await conn.execute("""CREATE FUNCTION reject_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic audit failure'; END; $$;
            CREATE TRIGGER fail_audit BEFORE INSERT ON ticket_events
            FOR EACH ROW EXECUTE FUNCTION reject_audit();""")
        with pytest.raises(asyncpg.RaiseError):
            async with conn.transaction():
                await app.initialize_database(conn)
        assert await conn.fetchval("SELECT status FROM tickets WHERE id=$1", tid) == "pending_verification"
        assert await conn.fetchval("SELECT count(*) FROM ticket_events") == 0
