import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, User
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import bot as app

# Synthetic IDs and token; no Telegram request is ever made by this fixture.
TOKEN = "900000001:" + "x" * 35
ADMIN = 900000001
TECH = 900000002
OTHER_TECH = 900000003
USER = 900000004
GROUP = -1009000000001


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setattr(app, "ADMINS", {ADMIN})
    monkeypatch.setattr(app, "IT", {ADMIN, TECH, OTHER_TECH})
    monkeypatch.setattr(app, "GROUP_ID", GROUP)
    monkeypatch.setattr(app, "delivery_lock", asyncio.Lock())
    app.access.windows.clear()


@pytest.fixture
async def telegram():
    bot = Bot(TOKEN)
    bot.session.make_request = AsyncMock(side_effect=AssertionError("Unexpected Telegram call"))
    yield bot
    await bot.session.close()


def message(uid=USER, text="skip", chat=None, mid=101, **extra):
    return Message(message_id=mid, date=datetime.now(timezone.utc),
                   chat=Chat(id=uid if chat is None else chat, type="private" if chat is None else "supergroup"),
                   from_user=User(id=uid, is_bot=False, first_name="Synthetic"), text=text, **extra)


def callback(uid=TECH, data="act:1:take", chat=GROUP, mid=101):
    return CallbackQuery(id="synthetic-callback", from_user=User(id=uid, is_bot=False, first_name="Synthetic"),
                         chat_instance="test", data=data, message=message(uid, "card", chat, mid))


@pytest.fixture
def state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=ADMIN, chat_id=USER, user_id=USER))


@pytest.fixture
def db(monkeypatch):
    conn = SimpleNamespace(fetchrow=AsyncMock(), fetchval=AsyncMock(), execute=AsyncMock(), fetch=AsyncMock())

    @asynccontextmanager
    async def context():
        yield conn

    conn.transaction = context
    pool = SimpleNamespace(acquire=context)
    monkeypatch.setattr(app, "pool", pool)
    return conn


@pytest.fixture
def api():
    return SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)),
                           send_photo=AsyncMock(return_value=SimpleNamespace(message_id=102)),
                           edit_message_text=AsyncMock())


@pytest.fixture
def form_data():
    return dict(full_name="Тестовий Працівник", department="Тестовий відділ", location="Тестове місце",
                category="ПЗ", description="Синтетичний опис несправності")


@pytest.fixture
def ticket(form_data):
    return dict(id=1, creator_id=USER, assignee_id=None, status="new", group_message_id=101,
                photo_file_id=None, group_photo_message_id=None, group_synced_status="new",
                user_notified_status=None, **form_data)
