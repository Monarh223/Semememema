"""
Скрипт для выдачи прав админа пользователю.
Использование:
  python make_admin.py <telegram_user_id>
  python make_admin.py 7874201166
  BOT_ADMIN_IDS=7874201166 python make_admin.py   # из .env не читаем здесь, только аргумент
Или через .env: задайте BOT_ADMIN_IDS=id1 id2 — тогда можно вызвать без аргумента (возьмёт первый ID).
"""
import asyncio
import logging
import os
import sys

from db import ensure_user, init_db, set_admin, set_allowed


def get_user_id() -> int | None:
    """User ID из аргумента командной строки или из BOT_ADMIN_IDS в .env."""
    if len(sys.argv) >= 2:
        raw = sys.argv[1].strip()
        if raw.isdigit():
            return int(raw)
        print("Ожидается числовой Telegram user_id.", file=sys.stderr)
        return None
    env_ids = os.getenv("BOT_ADMIN_IDS", "").strip().split()
    for x in env_ids:
        if x.isdigit():
            return int(x)
    print(
        "Укажите user_id: python make_admin.py <telegram_user_id>\n"
        "Либо задайте BOT_ADMIN_IDS в .env (например BOT_ADMIN_IDS=7874201166).",
        file=sys.stderr,
    )
    return None


async def main() -> None:
    user_id = get_user_id()
    if user_id is None:
        sys.exit(1)
    try:
        await init_db()
        await ensure_user(user_id, "admin_user")
        await set_allowed(user_id, True)
        await set_admin(user_id, True)
        print(f"Пользователь {user_id} назначен админом и получил доступ.")
    except Exception as e:
        logging.exception("Ошибка БД: %s", e)
        print("Временная ошибка БД, попробуйте позже.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
