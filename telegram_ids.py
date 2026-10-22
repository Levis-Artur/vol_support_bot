"""Manual setup helper. Run only when the main bot is stopped; prints IDs, never message bodies."""
import asyncio
import os

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from dotenv import load_dotenv
from config import BASE_DIR


async def main():
    load_dotenv(BASE_DIR / ".env")
    async with Bot(os.environ["BOT_TOKEN"], session=AiohttpSession(timeout=15)) as bot:
        if (await bot.get_webhook_info()).url:
            print("Webhook configured. Remove it before using this polling helper.")
            return
        updates = await bot.get_updates(timeout=0, allowed_updates=["message"])
        found = set()
        for update in updates:
            m = update.message
            if not m or not m.from_user or m.sender_chat:
                continue
            if (m.text or "").split("@")[0].strip() != "/id":
                continue
            found.add((m.from_user.id, m.chat.id))
        for user_id, chat_id in sorted(found):
            print(f"Telegram ID: {user_id}; chat_id: {chat_id}")
        if not found:
            print("Send /id to the bot and /id@BOT_USERNAME in the group, then run this helper again.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(f"Setup failed ({type(error).__name__}); check BOT_TOKEN, connection and polling conflicts.")
        raise SystemExit(1) from None
