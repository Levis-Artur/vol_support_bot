import asyncio
import html
import logging
import time
from collections import OrderedDict
from contextlib import suppress

import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from config import BASE_DIR, Settings, positive_id

GROUP_ID = 0
ADMINS = frozenset()
IT = frozenset()
router = Router()
pool = None
logger = logging.getLogger(__name__)
delivery_lock = asyncio.Lock()


class DrainMiddleware(BaseMiddleware):
    """Let an in-flight update finish before closing its database/session."""
    async def __call__(self, handler, event, data):
        task = asyncio.create_task(handler(event, data))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(task, timeout=25)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception as error:
                safe_error("Shutdown update", error)
            raise


def safe_error(operation, error):
    # Exception messages/tracebacks may contain tokens or request contents.
    logger.warning("%s failed (%s)", operation, type(error).__name__)


def valid_text(value, minimum, maximum):
    return (isinstance(value, str) and minimum <= len(value.strip()) <= maximum
            and not any(ord(ch) < 32 and ch not in "\n\t" for ch in value)
            and not value.strip().startswith("/"))


class AccessMiddleware(BaseMiddleware):
    """Bounded rate limit before DB access, and a blocked check for every handler."""
    def __init__(self):
        self.windows = OrderedDict()

    async def __call__(self, handler, event, data):
        user = event.from_user
        message = event.message if isinstance(event, CallbackQuery) else event
        if not user or user.is_bot or not isinstance(message, Message) or message.sender_chat:
            return
        if message.chat.type != ChatType.PRIVATE and message.chat.id != user.id:
            return
        # /id is harmless and is needed to discover a new group's ID.
        if (message.chat.type != ChatType.PRIVATE and message.chat.id != GROUP_ID
                and not (message.text or "").split("@")[0].split() == ["/id"]):
            return
        now = time.monotonic()
        start, count = self.windows.pop(user.id, (now, 0))
        if now - start >= 10:
            start, count = now, 0
        self.windows[user.id] = (start, count + 1)
        if len(self.windows) > 4096:
            self.windows.popitem(last=False)
        if count >= 10:
            if isinstance(event, CallbackQuery):
                await event.answer("Забагато дій. Зачекайте 10 секунд.")
            return
        stored = await get_user(user.id)
        if stored and stored["blocked"]:
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ обмежено.", show_alert=True)
            else:
                await event.answer("Доступ обмежено. Зверніться до адміністратора.")
            return
        return await handler(event, data)


access = AccessMiddleware()
router.message.outer_middleware(access)
router.callback_query.outer_middleware(access)


@router.errors()
async def handle_error(event):
    safe_error("Update", event.exception)
    update = event.update
    try:
        if update.callback_query:
            await update.callback_query.answer("Помилка обробки. Спробуйте пізніше.", show_alert=True)
        elif update.message and update.message.chat.type == ChatType.PRIVATE:
            await update.message.answer("Не вдалося завершити дію. Перевірте /my; за потреби почніть /new.")
    except Exception as error:
        safe_error("Error response", error)
    return True
CATEGORIES = ["Комп'ютер", "Принтер", "Мережа", "ПЗ", "Інше"]
STATUSES = {"pending_verification": "⏳ Очікує перевірки", "new": "🆕 Нова",
            "in_progress": "🔧 В роботі", "waiting": "⏸ Очікування",
            "done": "✅ Виконана", "cancelled": "🚫 Скасована"}

class Form(StatesGroup):
    name = State()
    department = State()
    location = State()
    category = State()
    description = State()
    photo = State()

def esc(value):
    return html.escape(str(value or ""), quote=False)

def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Нова заявка", callback_data="begin")],
        [InlineKeyboardButton(text="📋 Мої заявки", callback_data="mine")],
    ])

def category_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=c, callback_data=f"cat:{i}")] for i,c in enumerate(CATEGORIES)
    ])

def action_keyboard(ticket):
    tid = ticket["id"]
    status = ticket["status"]
    buttons = []
    if status == "new":
        buttons = [[InlineKeyboardButton(text="🙋 Взяти в роботу", callback_data=f"act:{tid}:take")]]
    elif status in ("in_progress", "waiting"):
        if status == "in_progress":
            buttons.append([InlineKeyboardButton(text="⏸ Очікування", callback_data=f"act:{tid}:wait")])
        else:
            buttons.append([InlineKeyboardButton(text="▶️ Продовжити", callback_data=f"act:{tid}:resume")])
        buttons.append([InlineKeyboardButton(text="✅ Виконано", callback_data=f"act:{tid}:done")])
    if status in ("new", "in_progress", "waiting"):
        buttons.append([InlineKeyboardButton(text="🚫 Скасувати", callback_data=f"act:{tid}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

def ticket_text(t):
    return (f"<b>Заявка #{t['id']:06}</b>\n"
            f"<b>Статус:</b> {STATUSES[t['status']]}\n"
            f"<b>Працівник:</b> {esc(t['full_name'])}\n"
            f"<b>Підрозділ:</b> {esc(t['department'])}\n"
            f"<b>Кабінет/місце:</b> {esc(t['location'])}\n"
            f"<b>Категорія:</b> {esc(t['category'])}\n"
            f"<b>Опис:</b> {esc(t['description'])}\n"
            f"<b>Виконавець ID:</b> {t['assignee_id'] or 'не призначено'}")

async def ensure_user(user):
    async with pool.acquire() as conn:
        await conn.execute("""INSERT INTO users(telegram_id)
          VALUES($1) ON CONFLICT(telegram_id) DO NOTHING""", user.id)

async def get_user(uid):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", uid)

async def ticket_by_id(tid):
    async with pool.acquire() as conn:
        return await conn.fetchrow("""SELECT t.*,
          COALESCE(t.submitter_name,u.full_name) AS full_name,
          COALESCE(t.submitter_department,u.department) AS department FROM tickets t
          JOIN users u ON u.telegram_id=t.creator_id WHERE t.id=$1""", tid)

async def publish_ticket(bot, tid):
    return await deliver_ticket(bot, tid, group=True)

async def update_group_message(bot, tid):
    try:
        return await deliver_ticket(bot, tid, group=True)
    except Exception as error:
        safe_error("Group delivery", error)


async def deliver_ticket(bot, tid, *, group):
    # One process; DB row locks also serialize delivery with approve/reject/status.
    # Lock order is always creator then ticket. Telegram cannot provide exactly-once sends.
    async with delivery_lock, pool.acquire() as conn, conn.transaction():
        creator = await conn.fetchval("SELECT creator_id FROM tickets WHERE id=$1", tid)
        if creator is None:
            return False
        user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1 FOR NO KEY UPDATE", creator)
        t = await conn.fetchrow("""SELECT t.*,
          COALESCE(t.submitter_name,u.full_name) AS full_name,
          COALESCE(t.submitter_department,u.department) AS department
          FROM tickets t JOIN users u ON u.telegram_id=t.creator_id
          WHERE t.id=$1 FOR UPDATE OF t""", tid)
        if not t or user["blocked"]:
            return False
        if not group:
            if t["user_notified_status"] == t["status"]:
                return False
            try:
                await bot.send_message(creator, f"🔔 Заявка #{tid:06}: {STATUSES[t['status']]}")
            except TelegramForbiddenError:
                # Do not retry forever when the recipient has blocked the bot.
                logger.warning("Private delivery unavailable")
            await conn.execute("UPDATE tickets SET user_notified_status=status WHERE id=$1", tid)
            return True
        if not user["verified"] or t["status"] == "pending_verification":
            return False
        if not t["group_message_id"] and t["status"] != "new":
            return False
        changed = False
        if not t["group_message_id"]:
            msg = await bot.send_message(GROUP_ID, ticket_text(t), reply_markup=action_keyboard(t))
            await conn.execute("""UPDATE tickets SET group_message_id=$2,
                group_synced_status=status WHERE id=$1""", tid, msg.message_id)
            changed = True
        elif t["group_synced_status"] != t["status"]:
            try:
                await bot.edit_message_text(chat_id=GROUP_ID, message_id=t["group_message_id"],
                    text=ticket_text(t), reply_markup=action_keyboard(t))
            except TelegramBadRequest as error:
                if "message is not modified" not in error.message.lower():
                    raise
            await conn.execute("UPDATE tickets SET group_synced_status=status WHERE id=$1", tid)
            changed = True
        if t["photo_file_id"] and not t["group_photo_message_id"]:
            # A photo failure must not roll back the successful text delivery marker.
            try:
                photo = await bot.send_photo(GROUP_ID, t["photo_file_id"], caption=f"Фото до заявки #{tid:06}")
            except Exception as error:
                safe_error("Photo delivery", error)
            else:
                await conn.execute("UPDATE tickets SET group_photo_message_id=$2 WHERE id=$1", tid, photo.message_id)
                changed = True
        return changed


async def notify_ticket(bot, tid):
    try:
        await deliver_ticket(bot, tid, group=False)
    except Exception as error:
        safe_error("Private delivery", error)


async def retry_deliveries(bot):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""SELECT t.id FROM tickets t JOIN users u ON u.telegram_id=t.creator_id
          WHERE NOT u.blocked AND t.next_delivery_at <= now() AND
          (t.user_notified_status IS DISTINCT FROM t.status OR
           (u.verified AND t.status <> 'pending_verification' AND
            ((t.status='new' AND t.group_message_id IS NULL) OR
             (t.group_message_id IS NOT NULL AND
              (t.group_synced_status IS DISTINCT FROM t.status OR
               (t.photo_file_id IS NOT NULL AND t.group_photo_message_id IS NULL))))))
          ORDER BY t.next_delivery_at,t.id LIMIT 50""")
    sent = 0
    for row in rows:
        # Rotate failures so one bad recipient cannot starve the rest of the queue.
        async with pool.acquire() as conn:
            await conn.execute("UPDATE tickets SET next_delivery_at=now()+interval '60 seconds' WHERE id=$1", row["id"])
        try:
            sent += bool(await publish_ticket(bot, row["id"]))
        except Exception as error:
            safe_error("Retry group delivery", error)
        await notify_ticket(bot, row["id"])
    return sent


async def delivery_worker(bot):
    while True:
        try:
            await retry_deliveries(bot)
        except Exception as error:
            safe_error("Delivery worker", error)
        await asyncio.sleep(30)

async def inform_user(bot, uid, text):
    try:
        await bot.send_message(uid, text)
    except Exception as error:
        safe_error("Information delivery", error)

@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user)
    u = await get_user(m.from_user.id)
    if u["blocked"]:
        return await m.answer("Ваш доступ обмежено. Зверніться до адміністратора.")
    if not u["verified"]:
        await m.answer("🔐 Ви ще не підтверджені адміністратором. Можете подати заявку, "
                       "але вона потрапить до ІТ-черги лише після перевірки.")
    await m.answer("Вітаємо у Support Bot УПП м. Луцьк.", reply_markup=menu())

@router.message(Command("id"))
async def chat_id(m: Message):
    await m.answer(f"Ваш ID: <code>{m.from_user.id}</code>\nID чату: <code>{m.chat.id}</code>")

@router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
async def cancel_form(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Створення заявки скасовано.", reply_markup=menu())

async def begin_form(uid, state, bot):
    u = await get_user(uid)
    if not u or u["blocked"]:
        return await inform_user(bot, uid, "Спочатку виконайте /start або зверніться до адміністратора.")
    await state.clear()
    await state.set_state(Form.name)
    await bot.send_message(uid, "Введіть ім'я та прізвище (без спеціальних/чутливих даних):")

@router.callback_query(F.data == "begin")
async def begin_cb(c: CallbackQuery, state: FSMContext, bot: Bot):
    await c.answer()
    if not isinstance(c.message, Message) or c.message.chat.type != ChatType.PRIVATE:
        return
    await begin_form(c.from_user.id, state, bot)

@router.message(Command("new"), F.chat.type == ChatType.PRIVATE)
async def begin_cmd(m: Message, state: FSMContext, bot: Bot):
    await begin_form(m.from_user.id, state, bot)

@router.message(Form.name, F.chat.type == ChatType.PRIVATE, ~F.text.startswith("/"))
async def form_name(m: Message, state: FSMContext):
    if not valid_text(m.text, 3, 100):
        return await m.answer("Введіть ім'я текстом (3–100 символів).")
    await state.update_data(full_name=m.text.strip())
    await state.set_state(Form.department)
    await m.answer("Вкажіть підрозділ (до 100 символів):")

@router.message(Form.department, F.chat.type == ChatType.PRIVATE, ~F.text.startswith("/"))
async def form_dept(m: Message, state: FSMContext):
    if not valid_text(m.text, 2, 100):
        return await m.answer("Вкажіть підрозділ текстом (2–100 символів).")
    await state.update_data(department=m.text.strip())
    await state.set_state(Form.location)
    await m.answer("Де виникла проблема? Кабінет або місце (до 120 символів):")

@router.message(Form.location, F.chat.type == ChatType.PRIVATE, ~F.text.startswith("/"))
async def form_location(m: Message, state: FSMContext):
    if not valid_text(m.text, 1, 120):
        return await m.answer("Введіть коротку назву місця (до 120 символів).")
    await state.update_data(location=m.text.strip())
    await state.set_state(Form.category)
    await m.answer("Оберіть категорію:", reply_markup=category_keyboard())

@router.callback_query(Form.category, F.data.startswith("cat:"))
async def form_cat(c: CallbackQuery, state: FSMContext):
    if not isinstance(c.message, Message) or c.message.chat.type != ChatType.PRIVATE:
        return await c.answer()
    try:
        prefix, index = c.data.split(":")
        if not index.isascii() or not index.isdecimal() or not 0 <= int(index) < len(CATEGORIES):
            raise ValueError
        category = CATEGORIES[int(index)]
    except (ValueError, IndexError, AttributeError):
        return await c.answer("Некоректна категорія", show_alert=True)
    await c.answer()
    await state.update_data(category=category)
    await state.set_state(Form.description)
    await c.message.answer("Опишіть проблему (10–1000 символів). Не надсилайте паролі або службові дані.")

@router.message(Form.description, F.chat.type == ChatType.PRIVATE, ~F.text.startswith("/"))
async def form_desc(m: Message, state: FSMContext):
    if not valid_text(m.text, 10, 1000):
        return await m.answer("Потрібен текстовий опис довжиною 10–1000 символів.")
    await state.update_data(description=m.text.strip())
    await state.set_state(Form.photo)
    await m.answer("Надішліть одне фото (необов'язково) або напишіть «пропустити».")

@router.message(Form.photo, F.chat.type == ChatType.PRIVATE, ~F.text.startswith("/"))
async def form_submit(m: Message, state: FSMContext, bot: Bot):
    if m.media_group_id:
        return await m.answer("Надішліть одне фото, без альбому.")
    if m.photo:
        if (m.photo[-1].file_size or 0) > 10 * 1024 * 1024:
            return await m.answer("Фото має бути не більше 10 МБ.")
        photo = m.photo[-1].file_id
    elif m.text and m.text.strip().casefold() in ("пропустити", "skip", "-"):
        photo = None
    else:
        return await m.answer("Надішліть фото або слово «пропустити».")
    d = await state.get_data()
    if (not all(valid_text(d.get(key), lo, hi) for key, lo, hi in
                (("full_name", 3, 100), ("department", 2, 100), ("location", 1, 120), ("description", 10, 1000)))
            or d.get("category") not in CATEGORIES):
        await state.clear()
        return await m.answer("Форма застаріла. Почніть з /new.")
    uid = m.from_user.id
    async with pool.acquire() as conn:
        async with conn.transaction():
            u = await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1 FOR NO KEY UPDATE", uid)
            if not u or u["blocked"]:
                await state.clear()
                return await m.answer("Доступ обмежений.")
            existing = await conn.fetchval("SELECT id FROM tickets WHERE creator_id=$1 AND source_message_id=$2", uid, m.message_id)
            if existing:
                await state.clear()
                return await m.answer(f"Заявку #{existing:06} уже збережено. Перегляд: /my.")
            count = await conn.fetchval("""SELECT count(*) FROM tickets
                WHERE creator_id=$1 AND created_at >= now() - interval '24 hours'""", uid)
            if count >= (20 if u["verified"] else 3):
                await state.clear()
                return await m.answer("Ліміт заявок за останні 24 години вичерпано.")
            status = "new" if u["verified"] else "pending_verification"
            await conn.execute("""UPDATE users SET full_name=$2,department=$3 WHERE telegram_id=$1""",
                               uid, d["full_name"], d["department"])
            tid = await conn.fetchval("""INSERT INTO tickets
              (creator_id,location,category,description,photo_file_id,status,
               source_message_id,submitter_name,submitter_department)
              VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
              uid,d["location"],d["category"],d["description"],photo,status,
              m.message_id,d["full_name"],d["department"])
            await conn.execute("""INSERT INTO ticket_events(ticket_id,actor_id,new_status)
                 VALUES($1,$2,$3)""",tid,uid,status)
    await state.clear()
    await m.answer(f"✅ Заявку #{tid:06} збережено.\nСтатус: {STATUSES[status]}",reply_markup=menu())
    async with pool.acquire() as conn:
        await conn.execute("UPDATE tickets SET user_notified_status=$2 WHERE id=$1 AND status=$2", tid, status)
    if status == "new":
        try:
            await publish_ticket(bot, tid)
        except Exception as error:
            safe_error("Publish", error)

async def show_my(bot, uid):
    user = await get_user(uid)
    if not user or user["blocked"]:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("""SELECT id,status,category,created_at FROM tickets
            WHERE creator_id=$1 ORDER BY id DESC LIMIT 10""",uid)
    text = "\n".join(f"#{r['id']:06} — {STATUSES[r['status']]} — {esc(r['category'])}"
                     for r in rows) if rows else "У вас ще немає заявок."
    await bot.send_message(uid, text)

@router.message(Command("my"), F.chat.type == ChatType.PRIVATE)
async def my_cmd(m: Message, bot: Bot):
    await show_my(bot,m.from_user.id)

@router.callback_query(F.data == "mine")
async def my_cb(c: CallbackQuery, bot: Bot):
    await c.answer()
    if isinstance(c.message, Message) and c.message.chat.type == ChatType.PRIVATE:
        await show_my(bot,c.from_user.id)

def is_it(uid):
    return uid in IT


async def staff_allowed(m, *, admin=False, private=False):
    if not m.from_user or m.sender_chat or m.from_user.id not in (ADMINS if admin else IT):
        return False
    if private and m.chat.type != ChatType.PRIVATE:
        return False
    if m.chat.id not in (GROUP_ID, m.from_user.id):
        return False
    user = await get_user(m.from_user.id)
    return bool(user and not user["blocked"])


def command_id(text):
    parts = (text or "").split()
    if len(parts) != 2:
        raise ValueError("Expected one ID")
    return positive_id(parts[1])

@router.callback_query(F.data.startswith("act:"))
async def change_status(c: CallbackQuery, bot: Bot):
    if not isinstance(c.message, Message) or c.message.chat.id != GROUP_ID or not is_it(c.from_user.id):
        return await c.answer("Недостатньо прав.",show_alert=True)
    try:
        _, raw_id, action = c.data.split(":")
        tid = positive_id(raw_id)
    except (ValueError,AttributeError):
        return await c.answer("Некоректна дія.",show_alert=True)
    operations = {"take": ("new","in_progress"), "wait": ("in_progress","waiting"),
                  "resume": ("waiting","in_progress"), "done": (None,"done"),
                  "cancel": (None,"cancelled")}
    if action not in operations:
        return await c.answer("Невідома дія.",show_alert=True)
    old_expected, new = operations[action]
    uid = c.from_user.id
    async with pool.acquire() as conn:
        async with conn.transaction():
            actor = await conn.fetchrow("SELECT blocked FROM users WHERE telegram_id=$1", uid)
            if not actor or actor["blocked"]:
                return await c.answer("Доступ обмежено.", show_alert=True)
            eligible = await conn.fetchval("""SELECT telegram_id FROM users
                WHERE telegram_id=(SELECT creator_id FROM tickets WHERE id=$1)
                AND verified AND NOT blocked FOR NO KEY UPDATE""", tid)
            if not eligible:
                return await c.answer("Заявка недоступна.", show_alert=True)
            t = await conn.fetchrow("SELECT * FROM tickets WHERE id=$1 FOR UPDATE",tid)
            if not t:
                return await c.answer("Заявку не знайдено.",show_alert=True)
            if t["group_message_id"] != c.message.message_id:
                return await c.answer("Ця кнопка застаріла.", show_alert=True)
            old = t["status"]
            if old in ("done","cancelled","pending_verification"):
                return await c.answer("Заявку вже закрито або не підтверджено.",show_alert=True)
            if old_expected and old != old_expected:
                return await c.answer("Статус уже змінився.",show_alert=True)
            if action in ("done","cancel") and old not in ("new","in_progress","waiting"):
                return await c.answer("Недопустимий перехід.",show_alert=True)
            # Only the assignee can change progress; an admin can override.
            if action != "take" and t["assignee_id"] and t["assignee_id"] != uid and uid not in ADMINS:
                return await c.answer("Ця заявка закріплена за іншим виконавцем.",show_alert=True)
            if action == "take" or (action == "done" and not t["assignee_id"]):
                assignee = uid
            else:
                assignee = t["assignee_id"]
            await conn.execute("""UPDATE tickets SET status=$2, assignee_id=$3,
              completed_at=CASE WHEN $2='done' THEN now() ELSE NULL END,updated_at=now(),next_delivery_at=now()
              WHERE id=$1""",tid,new,assignee)
            await conn.execute("""INSERT INTO ticket_events(ticket_id,actor_id,old_status,new_status)
              VALUES($1,$2,$3,$4)""",tid,uid,old,new)
    await c.answer("Статус змінено.")
    await update_group_message(bot,tid)
    await notify_ticket(bot,tid)

@router.message(Command("stats"))
async def stats(m: Message):
    if not await staff_allowed(m):
        return
    async with pool.acquire() as conn:
        counts = await conn.fetch("SELECT status,count(*) AS n FROM tickets GROUP BY status")
        completed = await conn.fetchval("""SELECT count(*) FROM tickets
          WHERE status='done' AND completed_at >=
          (date_trunc('month',now() AT TIME ZONE 'Europe/Kyiv') AT TIME ZONE 'Europe/Kyiv')""")
    counts = {r["status"]:r["n"] for r in counts}
    text = "<b>📊 Статистика (за весь час)</b>\n"
    text += "\n".join(f"{name}: {counts.get(code,0)}" for code,name in STATUSES.items())
    text += f"\nВиконано з початку місяця: {completed}"
    await m.answer(text)

@router.message(Command("queue"))
async def queue(m: Message):
    if not await staff_allowed(m):
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("""SELECT t.id,t.status,t.category FROM tickets t
        JOIN users u ON u.telegram_id=t.creator_id
        WHERE u.verified AND NOT u.blocked AND t.status IN ('new','in_progress','waiting')
        ORDER BY t.created_at,t.id LIMIT 20""")
    await m.answer("\n".join(f"#{r['id']:06} {STATUSES[r['status']]} — {esc(r['category'])}"
                   for r in rows) if rows else "Черга порожня.")

@router.message(Command("pending"))
async def pending(m: Message):
    if not await staff_allowed(m, admin=True, private=True):
        return
    try:
        after = command_id(m.text) if len((m.text or "").split()) > 1 else 0
    except ValueError:
        return await m.answer("Використання: /pending або /pending TELEGRAM_ID (наступна сторінка)")
    async with pool.acquire() as conn:
        rows = await conn.fetch("""SELECT u.telegram_id,u.full_name,u.department,
          count(t.id) AS tickets FROM users u
          LEFT JOIN tickets t ON t.creator_id=u.telegram_id AND t.status='pending_verification'
          WHERE u.verified=false AND u.blocked=false AND u.telegram_id > $1
          GROUP BY u.telegram_id ORDER BY u.telegram_id LIMIT 5""", after)
    if not rows:
        return await m.answer("Непідтверджених користувачів немає.")
    await m.answer("\n".join(f"ID <code>{r['telegram_id']}</code> | {esc(r['full_name'])}"
       f" | {esc(r['department'])} | заявок: {r['tickets']}" for r in rows)
       + "\nДля підтвердження: /approve TELEGRAM_ID"
       + f"\nНаступна сторінка: /pending {rows[-1]['telegram_id']}")

@router.message(Command("approve"))
async def approve(m: Message, bot: Bot):
    if not await staff_allowed(m, admin=True, private=True):
        return
    try:
        uid = command_id(m.text)
    except (ValueError, IndexError):
        return await m.answer("Використання: /approve 123456789")
    async with pool.acquire() as conn:
        async with conn.transaction():
            u = await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1 FOR NO KEY UPDATE",uid)
            if not u or u["blocked"]:
                return await m.answer("Користувача не знайдено або він заблокований.")
            await conn.execute("UPDATE users SET verified=true WHERE telegram_id=$1",uid)
            await conn.execute("INSERT INTO user_events(user_id,actor_id,action) VALUES($1,$2,'approve')", uid, m.from_user.id)
            pending = await conn.fetch("""UPDATE tickets SET status='new',updated_at=now(),next_delivery_at=now()
              WHERE creator_id=$1 AND status='pending_verification' RETURNING id""",uid)
            for row in pending:
                await conn.execute("""INSERT INTO ticket_events(ticket_id,actor_id,
                  old_status,new_status) VALUES($1,$2,'pending_verification','new')""",
                  row["id"],m.from_user.id)
    await m.answer(f"✅ Користувача {uid} підтверджено. Заявок: {len(pending)}.")
    await inform_user(bot,uid,"✅ Ваш обліковий запис підтверджено. Заявки поставлено в чергу доставки ІТ-відділу.")
    for row in pending:
        try:
            await publish_ticket(bot,row["id"])
        except Exception as error:
            safe_error("Approved ticket delivery", error)
        await notify_ticket(bot,row["id"])

@router.message(Command("reject"))
async def reject(m: Message, bot: Bot):
    if not await staff_allowed(m, admin=True, private=True):
        return
    try:
        uid = command_id(m.text)
    except (ValueError, IndexError):
        return await m.answer("Використання: /reject 123456789")
    if uid in IT:
        return await m.answer("Не можна блокувати ІТ/адміністратора.")
    async with pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1 FOR NO KEY UPDATE", uid)
            if not user:
                return await m.answer("Користувача не знайдено.")
            await conn.execute("UPDATE users SET blocked=true,verified=false WHERE telegram_id=$1",uid)
            await conn.execute("INSERT INTO user_events(user_id,actor_id,action) VALUES($1,$2,'reject')", uid, m.from_user.id)
            rows = await conn.fetch("""UPDATE tickets SET status='cancelled',updated_at=now()
              WHERE creator_id=$1 AND status='pending_verification' RETURNING id""",uid)
            for row in rows:
                await conn.execute("""INSERT INTO ticket_events(ticket_id,actor_id,
                  old_status,new_status) VALUES($1,$2,'pending_verification','cancelled')""",
                  row["id"],m.from_user.id)
    await m.answer(f"Користувача {uid} заблоковано; неперевірені заявки скасовано.")
    await inform_user(bot,uid,"Доступ обмежено адміністратором. Неперевірені заявки скасовано.")

@router.message(Command("sync"))
async def sync(m: Message, bot: Bot):
    if not await staff_allowed(m, admin=True):
        return
    sent = await retry_deliveries(bot)
    await m.answer(f"Оновлено карток/фото в групі: {sent}. Повторні спроби — автоматично щохвилини.")


@router.callback_query()
async def stale_callback(c: CallbackQuery):
    await c.answer("Кнопка застаріла. Скористайтеся /new або /my.")

@router.message(F.chat.type == ChatType.PRIVATE)
async def fallback(m: Message):
    await m.answer("Команди: /start, /new, /my, /cancel, /id. Для нової заявки натисніть /new.")

async def main():
    global pool, GROUP_ID, ADMINS, IT
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Library errors may include Telegram URLs or payloads. Application errors are sanitized.
    logging.getLogger("aiogram").disabled = True
    logging.getLogger("aiogram.event").disabled = True
    logging.getLogger("aiogram.dispatcher").disabled = True
    try:
        settings = Settings.from_env()
    except ValueError as error:
        logger.error("Configuration: %s", error)  # config.py only emits fixed, secret-free messages
        raise
    GROUP_ID, ADMINS, IT = settings.group_id, settings.admins, settings.it
    bot = None
    worker = None
    dp = Dispatcher(events_isolation=SimpleEventIsolation())
    dp.update.outer_middleware(DrainMiddleware())
    try:
        pool = await asyncpg.create_pool(**settings.database, min_size=1, max_size=10,
                                        command_timeout=30, timeout=15, server_settings={"timezone": "UTC"})
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute((BASE_DIR / "schema.sql").read_text(encoding="utf-8"))
            for uid in IT:
                await conn.execute("""INSERT INTO users(telegram_id,verified)
                    VALUES($1,true) ON CONFLICT(telegram_id)
                    DO UPDATE SET verified=true,blocked=false""",uid)
        bot = Bot(settings.token, session=AiohttpSession(timeout=15),
                  default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp.include_router(router)
        # Reject webhook conflicts explicitly; do not silently drop pending updates.
        webhook = await bot.get_webhook_info()
        if webhook.url:
            logger.error("Webhook is configured; remove it before using polling")
            raise RuntimeError("WebhookConfigured")
        worker = asyncio.create_task(delivery_worker(bot))
        logger.info("Bot initialized; polling started")
        # Sequential handling plus update isolation prevents duplicate FSM submissions.
        await dp.start_polling(bot, handle_as_tasks=False, close_bot_session=False)
    finally:
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await dp.storage.close()
        await dp.fsm.events_isolation.close()
        if bot:
            await bot.session.close()
        if pool:
            try:
                await asyncio.wait_for(pool.close(), timeout=10)
            except TimeoutError:
                pool.terminate()
        logger.info("Bot stopped")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as error:
        safe_error("Startup/runtime; check configuration, database and webhook", error)
        raise SystemExit(1) from None
