"""
Проверка сохранения и чтения токенов в БД при большом объёме (1000 шт.).
Запуск: из папки бота, с активированным venv:
  python check_tokens_db.py

Создаёт тестового пользователя 0, записывает 1000 токенов, забирает их пачкой,
проверяет целостность каждого блока. Затем удаляет тестовые данные.
"""
import asyncio
import os
import sys

# тестовый user_id, не пересекается с реальными
TEST_USER_ID = 999999999

REQUIRED_LINES = [
    "sessionStorage.clear();",
    "localStorage.clear();",
    "__oneme_device_id",
    "__oneme_auth",
    "window.location.reload();",
]


def make_fake_token(seed: int) -> str:
    """Токен в том же формате, что и из browser_max / make_session_block."""
    device_id = f"device_{seed}_abc"
    auth = f"auth_token_{seed}_xyz"
    return (
        "sessionStorage.clear();\n"
        "localStorage.clear();\n"
        f'localStorage.setItem("__oneme_device_id", "{device_id}");\n'
        f'localStorage.setItem("__oneme_auth", "{auth}");\n'
        "window.location.reload();"
    )


def token_is_valid(block: str) -> tuple[bool, str]:
    """Проверка формата блока. Возвращает (ok, сообщение об ошибке)."""
    if not block or not isinstance(block, str):
        return False, "пустой или не строка"
    lines = block.strip().split("\n")
    if len(lines) != 5:
        return False, f"ожидалось 5 строк, получилось {len(lines)}"
    for req in REQUIRED_LINES:
        if req not in block:
            return False, f"нет подстроки: {req!r}"
    return True, ""


async def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    from dotenv import load_dotenv
    load_dotenv()

    from db import (
        init_db,
        ensure_user,
        add_user_token,
        get_user_tokens_count,
        pop_user_tokens,
        _get_conn,
    )

    await init_db()
    # Очистка от прерванного прошлого запуска
    conn = await _get_conn()
    try:
        await conn.execute("DELETE FROM tokens WHERE user_id = ?", (TEST_USER_ID,))
        await conn.execute("DELETE FROM users WHERE user_id = ?", (TEST_USER_ID,))
        await conn.commit()
    finally:
        await conn.close()
    await ensure_user(TEST_USER_ID, "check_tokens_test")

    n = 1000
    print(f"Добавляю {n} токенов в БД…")
    for i in range(n):
        token = make_fake_token(i)
        await add_user_token(TEST_USER_ID, token, f"test_{i}")
        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{n}")

    count_before = await get_user_tokens_count(TEST_USER_ID, only_unused=True)
    if count_before != n:
        print(f"Ошибка: после вставки ожидалось {n} неиспользованных, получилось {count_before}")
        sys.exit(1)
    print(f"Количество неиспользованных: {count_before} [OK]")

    print(f"Забираю все {n} токенов одной пачкой (pop_user_tokens)…")
    popped = await pop_user_tokens(TEST_USER_ID, n)
    if len(popped) != n:
        print(f"Ошибка: вернулось {len(popped)} пар, ожидалось {n}")
        sys.exit(1)
    print(f"Вернулось пар (token, prefix): {len(popped)} [OK]")

    print("Проверка формата каждого токена…")
    broken = []
    for i, (token, prefix) in enumerate(popped):
        ok, msg = token_is_valid(token)
        if not ok:
            broken.append((i, msg, token[:80]))
    if broken:
        print(f"Найдено повреждённых по формату: {len(broken)}")
        for i, msg, snippet in broken[:10]:
            print(f"  [{i}] {msg}: {snippet!r}…")
        if len(broken) > 10:
            print(f"  … и ещё {len(broken) - 10}")
        sys.exit(1)
    print("Все токены прошли проверку формата [OK]")

    print("Сверка: выданные токены должны совпадать с записанными побайтово…")
    mismatch = []
    for i, (token, prefix) in enumerate(popped):
        expected_token = make_fake_token(i)
        expected_prefix = f"test_{i}"
        if token != expected_token:
            mismatch.append((i, "token", len(token), len(expected_token), token[:60], expected_token[:60]))
        if prefix != expected_prefix:
            mismatch.append((i, "prefix", prefix, expected_prefix, None, None))
    if mismatch:
        print(f"Несовпадение записанного и выданного: {len(mismatch)}")
        for item in mismatch[:15]:
            if item[1] == "token":
                i, _, len_t, len_e, snip_t, snip_e = item
                print(f"  [{i}] token: длина выдано {len_t}, ожидалось {len_e}")
                print(f"       выдано:   {snip_t!r}…")
                print(f"       ожидалось: {snip_e!r}…")
            else:
                i, _, got, exp, _, _ = item
                print(f"  [{i}] prefix: выдано {got!r}, ожидалось {exp!r}")
        if len(mismatch) > 15:
            print(f"  … и ещё {len(mismatch) - 15}")
        sys.exit(1)
    print("Все 1000 токенов и префиксы совпадают с записанными [OK]")

    count_after = await get_user_tokens_count(TEST_USER_ID, only_unused=True)
    total_after = await get_user_tokens_count(TEST_USER_ID, only_unused=False)
    if count_after != 0:
        print(f"Ошибка: после pop ожидалось 0 неиспользованных, получилось {count_after}")
        sys.exit(1)
    if total_after != n:
        print(f"Ошибка: всего записей ожидалось {n}, получилось {total_after}")
        sys.exit(1)
    print(f"После выдачи: неиспользованных {count_after}, всего записей {total_after} [OK]")

    # Удаляем тестовые токены и пользователя
    conn = await _get_conn()
    try:
        await conn.execute("DELETE FROM tokens WHERE user_id = ?", (TEST_USER_ID,))
        await conn.execute("DELETE FROM users WHERE user_id = ?", (TEST_USER_ID,))
        await conn.commit()
    finally:
        await conn.close()
    print("Тестовые данные удалены.")

    print("\nИтог: проверка на 1000 токенов пройдена, данные не коцаются.")


if __name__ == "__main__":
    asyncio.run(main())
