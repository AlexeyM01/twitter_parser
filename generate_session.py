#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Одноразовый скрипт: получает TELEGRAM_SESSION (Telethon StringSession).

Строка сессии — это «вход» в ваш Telegram-аккаунт. Храните её только
в секретах GitHub / локальном .env. Никому не пересылайте.

Запуск:
    pip install telethon
    python generate_session.py
"""
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    print("Данные берутся на https://my.telegram.org -> API development tools\n")
    api_id = int(input("api_id: ").strip())
    api_hash = input("api_hash: ").strip()

    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        me = await client.get_me()
        print(f"\nУспешно! Аккаунт: @{me.username or me.id} ({me.first_name})")
        print("\n--- Скопируйте строку ниже в секрет TELEGRAM_SESSION ---")
        print(client.session.save())
        print("---------------------------------------------------------")
        print("\nВажно: аккаунт должен быть создателем или админом канала,")
        print("иначе публиковать посты он не сможет.")


if __name__ == "__main__":
    asyncio.run(main())
