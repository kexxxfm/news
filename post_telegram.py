# -*- coding: utf-8 -*-
"""Публікує готовий випуск (episode.mp3) у Telegram-каналі. Дані бере з tg_post.json, який пише bot.py.

Два способи (обирається автоматично):
  1) від імені вашого акаунта (секрет TG_SESSION) — акаунт має бути АДМІНІСТРАТОРОМ каналу з правом публікації;
  2) від імені бота (секрет TG_BOT_TOKEN, токен із @BotFather) — бот має бути адміністратором каналу.
Якщо задано TG_BOT_TOKEN — використовується бот, інакше акаунт.
"""
import asyncio
import json
import os
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeAudio


async def post(p: dict, env=os.environ, client_cls=TelegramClient, session_cls=StringSession):
    """Відправляє mp3 в канал як аудіо з назвою, виконавцем і підписом. Повертає (id повідомлення, хто публікував)."""
    api_id, api_hash = int(env["TG_API_ID"]), env["TG_API_HASH"]
    token = (env.get("TG_BOT_TOKEN") or "").strip()
    if token:
        client = client_cls(session_cls(), api_id, api_hash)
        await client.start(bot_token=token)
        who = "бот"
    else:
        client = client_cls(session_cls(env["TG_SESSION"]), api_id, api_hash)
        await client.connect()
        who = "акаунт"
    try:
        if not token and not await client.is_user_authorized():
            raise RuntimeError("сесія TG_SESSION недійсна: створіть її заново (make_session_qr.py)")
        msg = await client.send_file(
            p["channel"], p["file"], caption=p["caption"], mime_type="audio/mpeg",
            attributes=[DocumentAttributeAudio(duration=int(p["duration"]), title=p["title"], performer=p["performer"])])
        return msg.id, who
    finally:
        await client.disconnect()


def main() -> int:
    try:
        with open("tg_post.json", encoding="utf-8") as f:
            p = json.load(f)
    except OSError:
        print("tg_post.json немає: публікувати нічого.")
        return 0
    try:
        mid, who = asyncio.run(post(p))
    except Exception as e:
        print(f"::error::Не вдалося опублікувати в {p.get('channel')}: {e!r}")
        print("Перевірте: акаунт (або бот) має бути адміністратором каналу з правом публікації повідомлень; "
              "канал вказано правильно (панель: Настройки, або змінна TG_POST_CHANNEL).")
        return 1
    print(f"Опубліковано в {p['channel']} ({who}), повідомлення №{mid}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
