import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Update

import bot as app
from conftest import ADMIN, TECH, OTHER_TECH, USER, GROUP, message, callback


@pytest.mark.parametrize("value,valid", [("abc", True), ("  abc  ", True), ("ab", False),
    ("x" * 101, False), (None, False), ("abc\0def", False), ("/approve 1", False)])
def test_text_limits(value, valid):
    assert bool(app.valid_text(value, 3, 100)) == valid


@pytest.mark.parametrize("raw", ["0", "-1", "9" * 30, "١٢", "abc", "1 extra"])
def test_id_validation(raw):
    with pytest.raises(ValueError):
        app.command_id("/approve " + raw)


def test_html_escaped(ticket):
    ticket["description"] = '<b>not markup</b> & "text"'
    assert "&lt;b&gt;not markup&lt;/b&gt; &amp;" in app.ticket_text(ticket)


@pytest.mark.parametrize("status", ["done", "cancelled", "pending_verification"])
def test_no_actions_for_closed_or_unapproved(status):
    assert app.action_keyboard(dict(id=1, status=status)) is None


@pytest.mark.parametrize("index", ["-1", "5", "foo", "1:2", "١", "999999999999"])
async def test_invalid_category(index, state, telegram):
    telegram.session.make_request = AsyncMock()
    await state.set_state(app.Form.category)
    c = callback(USER, "cat:" + index, USER).as_(telegram)
    # callback helper creates group chats; use a private message explicitly.
    c = c.model_copy(update={"message": message().as_(telegram)})
    await app.form_cat(c, state)
    assert await state.get_state() == app.Form.category.state
    assert await state.get_data() == {}


async def test_form_steps(state, telegram, form_data):
    telegram.session.make_request = AsyncMock()
    for handler, value, expected in [
        (app.form_name, form_data["full_name"], app.Form.department),
        (app.form_dept, form_data["department"], app.Form.location),
        (app.form_location, form_data["location"], app.Form.category),
    ]:
        await handler(message(text=value).as_(telegram), state)
        assert await state.get_state() == expected.state
    c = callback(USER, "cat:3").model_copy(update={"message": message().as_(telegram)}).as_(telegram)
    await app.form_cat(c, state)
    await app.form_desc(message(text=form_data["description"]).as_(telegram), state)
    assert await state.get_state() == app.Form.photo.state
    assert await state.get_data() == form_data


@pytest.mark.parametrize("verified", [False, True])
async def test_create_ticket(verified, db, state, telegram, api, form_data, monkeypatch):
    telegram.session.make_request = AsyncMock()
    db.fetchrow.return_value = dict(blocked=False, verified=verified)
    db.fetchval.side_effect = [None, 0, 42]  # duplicate, daily count, inserted ID
    publish = AsyncMock()
    monkeypatch.setattr(app, "publish_ticket", publish)
    await state.update_data(**form_data)
    await app.form_submit(message().as_(telegram), state, api)
    insert = db.fetchval.await_args_list[-1]
    assert insert.args[6] == ("new" if verified else "pending_verification")
    assert insert.args[1] == USER
    assert insert.args[7] == 101
    assert await state.get_state() is None
    assert publish.await_count == int(verified)
    assert any("INSERT INTO ticket_events" in c.args[0] for c in db.execute.await_args_list)


@pytest.mark.parametrize("verified,limit", [(False, 3), (True, 20)])
async def test_daily_limit(verified, limit, db, state, telegram, api, form_data):
    telegram.session.make_request = AsyncMock()
    db.fetchrow.return_value = dict(blocked=False, verified=verified)
    db.fetchval.side_effect = [None, limit]
    await state.update_data(**form_data)
    await app.form_submit(message().as_(telegram), state, api)
    db.execute.assert_not_awaited()
    api.send_message.assert_not_awaited()


async def test_duplicate_submission(db, state, telegram, api, form_data):
    telegram.session.make_request = AsyncMock()
    db.fetchrow.return_value = dict(blocked=False, verified=False)
    db.fetchval.return_value = 42
    await state.update_data(**form_data)
    await app.form_submit(message().as_(telegram), state, api)
    db.execute.assert_not_awaited()


async def test_expired_form(db, state, telegram, api):
    telegram.session.make_request = AsyncMock()
    await app.form_submit(message().as_(telegram), state, api)
    db.fetchrow.assert_not_awaited()


@pytest.mark.parametrize("uid,chat", [(USER, GROUP), (TECH, -1009000000099)])
async def test_unauthorized_status(uid, chat, db, telegram, api):
    telegram.session.make_request = AsyncMock()
    await app.change_status(callback(uid, chat=chat).as_(telegram), api)
    db.fetchrow.assert_not_awaited()


@pytest.mark.parametrize("old,action,new", [("new", "take", "in_progress"),
    ("in_progress", "wait", "waiting"), ("waiting", "resume", "in_progress"),
    ("in_progress", "done", "done"), ("new", "cancel", "cancelled"), ("new", "done", "done")])
async def test_status_transitions(old, action, new, db, telegram, api, ticket, monkeypatch):
    telegram.session.make_request = AsyncMock()
    ticket.update(status=old, assignee_id=TECH if old != "new" else None)
    db.fetchrow.side_effect = [dict(blocked=False), ticket]
    monkeypatch.setattr(app, "update_group_message", AsyncMock())
    monkeypatch.setattr(app, "notify_ticket", AsyncMock())
    await app.change_status(callback(data=f"act:1:{action}").as_(telegram), api)
    update = db.execute.await_args_list[0]
    assert update.args[2] == new
    if action in ("take", "done"):
        assert update.args[3] == TECH
    assert db.execute.await_args_list[1].args[3:] == (old, new)
    app.notify_ticket.assert_awaited_once_with(api, 1)


@pytest.mark.parametrize("status,assignee,action,mid", [("done", TECH, "take", 101),
    ("pending_verification", None, "take", 101), ("waiting", TECH, "take", 101),
    ("in_progress", OTHER_TECH, "done", 101), ("new", None, "take", 999)])
async def test_invalid_transitions(status, assignee, action, mid, db, telegram, api, ticket):
    telegram.session.make_request = AsyncMock()
    ticket.update(status=status, assignee_id=assignee)
    db.fetchrow.side_effect = [dict(blocked=False), ticket]
    await app.change_status(callback(data=f"act:1:{action}", mid=mid).as_(telegram), api)
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("verified,blocked,status", [(False, False, "pending_verification"),
    (False, False, "new"), (True, True, "new")])
async def test_no_group_leak(verified, blocked, status, db, api, ticket):
    ticket.update(status=status, group_message_id=None)
    db.fetchval.return_value = USER
    db.fetchrow.side_effect = [dict(verified=verified, blocked=blocked), ticket]
    assert not await app.publish_ticket(api, 1)
    api.send_message.assert_not_awaited()


async def test_failed_photo_keeps_text_delivery(db, api, ticket):
    ticket.update(group_message_id=None, photo_file_id="synthetic-file")
    db.fetchval.return_value = USER
    db.fetchrow.side_effect = [dict(verified=True, blocked=False), ticket]
    api.send_photo.side_effect = RuntimeError("synthetic secret")
    assert await app.publish_ticket(api, 1)
    assert "group_message_id=$2" in db.execute.await_args.args[0]


async def test_private_block_does_not_retry_forever(db, api, ticket):
    db.fetchval.return_value = USER
    db.fetchrow.side_effect = [dict(verified=True, blocked=False), ticket]
    api.send_message.side_effect = TelegramForbiddenError(SendMessage(chat_id=USER, text="test"), "blocked")
    assert await app.deliver_ticket(api, 1, group=False)
    assert "user_notified_status=status" in db.execute.await_args.args[0]


async def test_unchanged_telegram_edit_is_success(db, api, ticket):
    ticket.update(status="done")
    db.fetchval.return_value = USER
    db.fetchrow.side_effect = [dict(verified=True, blocked=False), ticket]
    api.edit_message_text.side_effect = TelegramBadRequest(EditMessageText(text="test"), "message is not modified")
    assert await app.publish_ticket(api, 1)
    assert "group_synced_status=status" in db.execute.await_args.args[0]


@pytest.mark.parametrize("handler", [app.stats, app.queue, app.pending, app.approve, app.reject, app.sync])
async def test_staff_commands_denied(handler, db, telegram, api):
    m = message().as_(telegram)
    args = (m, api) if handler in (app.approve, app.reject, app.sync) else (m,)
    await handler(*args)
    db.fetch.assert_not_awaited()
    db.execute.assert_not_awaited()


async def test_pending_never_in_group(db, telegram):
    await app.pending(message(ADMIN, "/pending", GROUP).as_(telegram))
    db.fetch.assert_not_awaited()


async def test_approve_releases_pending_and_audits(db, telegram, api, monkeypatch):
    telegram.session.make_request = AsyncMock()
    db.fetchrow.return_value = dict(blocked=False)
    db.fetch.return_value = [dict(id=1), dict(id=2)]
    monkeypatch.setattr(app, "publish_ticket", AsyncMock())
    monkeypatch.setattr(app, "notify_ticket", AsyncMock())
    await app.approve(message(ADMIN, f"/approve {USER}").as_(telegram), api)
    assert app.publish_ticket.await_count == 2
    assert "status='pending_verification'" in db.fetch.await_args.args[0]
    assert any("user_events" in c.args[0] for c in db.execute.await_args_list)


async def test_reject_cancels_pending(db, telegram, api):
    telegram.session.make_request = AsyncMock()
    db.fetchrow.return_value = dict(blocked=False)
    db.fetch.return_value = [dict(id=1)]
    await app.reject(message(ADMIN, f"/reject {USER}").as_(telegram), api)
    assert "status='cancelled'" in db.fetch.await_args.args[0]
    assert "status='pending_verification'" in db.fetch.await_args.args[0]
    assert any("blocked=true" in c.args[0] for c in db.execute.await_args_list)


async def test_my_scoped_to_owner(db, api):
    db.fetchrow.return_value = dict(blocked=False)
    db.fetch.return_value = [dict(id=1, status="new", category="ПЗ")]
    await app.show_my(api, USER)
    assert "creator_id=$1" in db.fetch.await_args.args[0]
    assert db.fetch.await_args.args[1] == USER
    assert api.send_message.await_args.args[0] == USER


async def test_middleware_block_and_rate_limit(db, telegram):
    telegram.session.make_request = AsyncMock()
    middleware = app.AccessMiddleware()
    handler = AsyncMock()
    db.fetchrow.return_value = dict(blocked=True)
    await middleware(handler, message().as_(telegram), {})
    handler.assert_not_awaited()
    db.fetchrow.return_value = dict(blocked=False)
    middleware.windows.clear()
    for _ in range(15):
        await middleware(handler, message().as_(telegram), {})
    assert handler.await_count == 10


async def test_missing_callback_message(db, telegram):
    c = callback().model_copy(update={"message": None}).as_(telegram)
    handler = AsyncMock()
    await app.AccessMiddleware()(handler, c, {})
    handler.assert_not_awaited()


async def test_stats_counts_and_month_timezone(db, telegram):
    telegram.session.make_request = AsyncMock()
    db.fetchrow.return_value = dict(blocked=False)
    db.fetch.return_value = [dict(status="new", n=2), dict(status="done", n=3)]
    db.fetchval.return_value = 1
    await app.stats(message(TECH, "/stats").as_(telegram))
    text = telegram.session.make_request.await_args.args[1].text
    assert "🆕 Нова: 2" in text
    assert "✅ Виконана: 3" in text
    assert "Виконано з початку місяця: 1" in text
    assert "Europe/Kyiv" in db.fetchval.await_args.args[0]


async def test_pending_pagination_and_escaping(db, telegram):
    telegram.session.make_request = AsyncMock()
    db.fetchrow.return_value = dict(blocked=False)
    db.fetch.return_value = [dict(telegram_id=USER, full_name="<fake>", department="A&B", tickets=2)]
    await app.pending(message(ADMIN, f"/pending {TECH}").as_(telegram))
    text = telegram.session.make_request.await_args.args[1].text
    assert "&lt;fake&gt;" in text and "A&amp;B" in text
    assert f"/pending {USER}" in text
    assert db.fetch.await_args.args[1] == TECH


async def test_blocked_submit(db, state, telegram, api, form_data):
    telegram.session.make_request = AsyncMock()
    db.fetchrow.return_value = dict(blocked=True, verified=False)
    await state.update_data(**form_data)
    await app.form_submit(message().as_(telegram), state, api)
    db.fetchval.assert_not_awaited()
    db.execute.assert_not_awaited()


async def test_admin_override(db, telegram, api, ticket, monkeypatch):
    telegram.session.make_request = AsyncMock()
    ticket.update(status="in_progress", assignee_id=TECH)
    db.fetchrow.side_effect = [dict(blocked=False), ticket]
    monkeypatch.setattr(app, "update_group_message", AsyncMock())
    monkeypatch.setattr(app, "notify_ticket", AsyncMock())
    await app.change_status(callback(ADMIN, "act:1:done").as_(telegram), api)
    assert db.execute.await_args_list[0].args[2:4] == ("done", TECH)


async def test_group_send_failure_not_marked_delivered(db, api, ticket):
    ticket.update(group_message_id=None)
    db.fetchval.return_value = USER
    db.fetchrow.side_effect = [dict(verified=True, blocked=False), ticket]
    api.send_message.side_effect = OSError("synthetic network failure")
    with pytest.raises(OSError):
        await app.publish_ticket(api, 1)
    db.execute.assert_not_awaited()


async def test_shutdown_drains_current_update():
    started, finish = asyncio.Event(), asyncio.Event()
    completed = []

    async def handler(event, data):
        started.set()
        await finish.wait()
        completed.append(True)

    task = asyncio.create_task(app.DrainMiddleware()(handler, None, {}))
    await started.wait()
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed == [True]


async def test_unknown_callback_gets_answer(telegram):
    telegram.session.make_request = AsyncMock()
    await app.stale_callback(callback(data="unknown:button").as_(telegram))
    assert "застаріла" in telegram.session.make_request.await_args.args[1].text


def test_safe_logging(caplog):
    with caplog.at_level(logging.WARNING):
        app.safe_error("Publish", RuntimeError("PRIVATE_TOKEN_AND_TEXT"))
    assert "RuntimeError" in caplog.text
    assert "PRIVATE_TOKEN_AND_TEXT" not in caplog.text


async def test_real_dispatcher_fsm_and_duplicate_updates(db, telegram):
    """Exercise actual aiogram filter ordering, FSM injection and event isolation."""
    telegram.session.make_request = AsyncMock()
    db.fetchrow.return_value = dict(verified=False, blocked=False)
    dp = Dispatcher(events_isolation=app.SimpleEventIsolation())
    dp.include_router(app.router)
    try:
        await dp.feed_update(telegram, Update(update_id=1, message=message(text="/start", mid=1)))
        await dp.feed_update(telegram, Update(update_id=2, message=message(text="/new", mid=2)))
        state = dp.fsm.get_context(bot=telegram, chat_id=USER, user_id=USER)
        assert await state.get_state() == app.Form.name.state
        await dp.feed_update(telegram, Update(update_id=3, message=message(text="/cancel", mid=3)))
        assert await state.get_state() is None
        await state.set_state(app.Form.photo)
        await state.update_data(full_name="Тестовий Автор", department="Тест", location="Тест",
                                category="ПЗ", description="Синтетичний опис")
        db.fetchval.side_effect = [None, 0, 77]
        await asyncio.gather(*[dp.feed_update(telegram, Update(update_id=4+i, message=message(mid=4+i))) for i in range(2)])
        assert len([c for c in db.fetchval.await_args_list if "INSERT INTO tickets" in c.args[0]]) == 1
    finally:
        await dp.storage.close()
        await dp.fsm.events_isolation.close()
        app.router._parent_router = None
