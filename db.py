"""
SQLite: учёт пользователей, доступ по разрешению админа, счёт успешных токенов.
"""
import logging
import os
from pathlib import Path

try:
    import aiosqlite
    _AIOSQLITE = True
except ImportError:
    aiosqlite = None
    _AIOSQLITE = False

DB_PATH = os.getenv("BOT_DB_PATH", "bot.db")
INIT_ADMIN_IDS = [x for x in os.getenv("BOT_ADMIN_IDS", "").strip().split() if x]


async def _get_conn():
    if not _AIOSQLITE:
        raise ImportError("Установите: pip install aiosqlite")
    return await aiosqlite.connect(DB_PATH)


async def init_db() -> None:
    """Создаёт таблицы и первого админа."""
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                allowed INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0,
                tokens_count INTEGER NOT NULL DEFAULT 0,
                last_success_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
            )
            await conn.execute(
                """
            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token TEXT NOT NULL,
                filename_prefix TEXT,
                is_used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                used_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """
            )
            try:
                await conn.execute("ALTER TABLE tokens ADD COLUMN filename_prefix TEXT")
                await conn.commit()
            except Exception:
                pass
            await conn.execute(
                """
            CREATE TABLE IF NOT EXISTS proxies (
                proxy TEXT PRIMARY KEY,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                removed_at TEXT
            )
            """
            )
            await conn.execute(
                """
            CREATE TABLE IF NOT EXISTS proxy_stats (
                proxy TEXT PRIMARY KEY,
                use_count INTEGER NOT NULL DEFAULT 0,
                token_count INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT
            )
            """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_allowed ON users(allowed)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tokens_user_used ON tokens(user_id, is_used)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS support_groups (
                    group_id INTEGER PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    title TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (owner_id) REFERENCES users(user_id)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS support_group_members (
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    added_at TEXT NOT NULL DEFAULT (datetime('now')),
                    token_access INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, user_id),
                    FOREIGN KEY (group_id) REFERENCES support_groups(group_id) ON DELETE CASCADE
                )
                """
            )
            try:
                await conn.execute("ALTER TABLE support_group_members ADD COLUMN token_access INTEGER DEFAULT 0")
                await conn.commit()
            except Exception:
                pass
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS support_group_token_stats (
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    tokens_count INTEGER NOT NULL DEFAULT 0,
                    last_at TEXT,
                    PRIMARY KEY (group_id, user_id),
                    FOREIGN KEY (group_id) REFERENCES support_groups(group_id) ON DELETE CASCADE
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS used_phones (
                    phone TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            try:
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN balance_cents INTEGER NOT NULL DEFAULT 0"
                )
                await conn.commit()
            except Exception:
                pass
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS crypto_invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    invoice_id INTEGER NOT NULL UNIQUE,
                    amount_cents INTEGER NOT NULL,
                    payload TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    paid_at TEXT,
                    notify_chat_id INTEGER,
                    notify_thread_id INTEGER,
                    notify_sent INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_crypto_invoices_user ON crypto_invoices(user_id, status)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            for col_sql in (
                "ALTER TABLE crypto_invoices ADD COLUMN notify_chat_id INTEGER",
                "ALTER TABLE crypto_invoices ADD COLUMN notify_thread_id INTEGER",
                "ALTER TABLE crypto_invoices ADD COLUMN notify_sent INTEGER NOT NULL DEFAULT 0",
            ):
                try:
                    await conn.execute(col_sql)
                    await conn.commit()
                except Exception:
                    pass
            for aid in INIT_ADMIN_IDS:
                if not aid.isdigit():
                    continue
                uid = int(aid)
                await conn.execute(
                    """INSERT OR IGNORE INTO users (user_id, allowed, is_admin)
                       VALUES (?, 1, 1)""",
                    (uid,),
                )
            await conn.commit()
        except Exception:
            logging.exception("DB error in init_db")
            raise
    finally:
        await conn.close()


async def log_proxy_add(proxy: str) -> None:
    """Фиксирует добавление прокси (proxy — строка из proxies.txt)."""
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                """
                INSERT INTO proxies (proxy, added_at, removed_at)
                VALUES (?, datetime('now'), NULL)
                ON CONFLICT(proxy) DO UPDATE SET
                    added_at = excluded.added_at,
                    removed_at = NULL
                """,
                (proxy,),
            )
            await conn.commit()
        except Exception:
            logging.exception("DB error in log_proxy_add")
            raise
    finally:
        await conn.close()


async def log_proxy_remove(proxy: str) -> None:
    """Помечает прокси как удалённый (removed_at = now)."""
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                """
                UPDATE proxies
                SET removed_at = datetime('now')
                WHERE proxy = ?
                """,
                (proxy,),
            )
            await conn.commit()
        except Exception:
            logging.exception("DB error in log_proxy_remove")
            raise
    finally:
        await conn.close()


async def list_proxies_meta() -> list[dict]:
    """Возвращает список прокси с датой добавления/удаления."""
    conn = await _get_conn()
    try:
        try:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT proxy, added_at, removed_at FROM proxies ORDER BY added_at DESC"
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception:
            logging.exception("DB error in list_proxies_meta")
            raise
    finally:
        await conn.close()


async def log_proxy_usage(proxy: str, tokens_delta: int = 0) -> None:
    """
    Увеличивает счётчики использования прокси.
    use_count — каждый раз, когда прокси выбирается для запроса/браузера.
    token_count — увеличивается на tokens_delta при успешном получении токена через этот прокси.
    """
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                """
            INSERT INTO proxy_stats (proxy, use_count, token_count, last_used_at)
            VALUES (?, 1, ?, datetime('now'))
            ON CONFLICT(proxy) DO UPDATE SET
                use_count   = proxy_stats.use_count   + 1,
                token_count = proxy_stats.token_count + excluded.token_count,
                last_used_at = datetime('now')
            """,
                (proxy, tokens_delta),
            )
            await conn.commit()
        except Exception:
            logging.exception("DB error in log_proxy_usage")
            raise
    finally:
        await conn.close()


async def get_proxy_stats() -> dict[str, dict]:
    """Возвращает статистику по прокси: proxy -> {use_count, token_count, last_used_at}."""
    conn = await _get_conn()
    try:
        try:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT proxy, use_count, token_count, last_used_at FROM proxy_stats"
            )
            rows = await cur.fetchall()
            return {r["proxy"]: dict(r) for r in rows}
        except Exception:
            logging.exception("DB error in get_proxy_stats")
            raise
    finally:
        await conn.close()


async def is_phone_used(phone: str) -> bool:
    """Проверяет, был ли уже получен токен по этому номеру (нормализованный формат, например +79001234567)."""
    if not phone or not phone.strip():
        return False
    conn = await _get_conn()
    try:
        try:
            cur = await conn.execute(
                "SELECT 1 FROM used_phones WHERE phone = ?",
                (phone.strip(),),
            )
            return (await cur.fetchone()) is not None
        except Exception:
            logging.exception("DB error in is_phone_used")
            raise
    finally:
        await conn.close()


async def add_used_phone(phone: str) -> None:
    """Фиксирует, что по этому номеру уже был получен токен."""
    if not phone or not phone.strip():
        return
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                "INSERT OR IGNORE INTO used_phones (phone) VALUES (?)",
                (phone.strip(),),
            )
            await conn.commit()
        except Exception:
            logging.exception("DB error in add_used_phone")
            raise
    finally:
        await conn.close()


async def get_user(user_id: int) -> dict | None:
    """Возвращает user или None."""
    conn = await _get_conn()
    try:
        try:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return dict(row)
        except Exception:
            logging.exception("DB error in get_user")
            raise
    finally:
        await conn.close()


async def is_allowed(user_id: int) -> bool:
    """Разрешён ли доступ (allowed=1 или is_admin=1)."""
    u = await get_user(user_id)
    if u is None:
        return False
    return bool(u.get("allowed") or u.get("is_admin"))


async def is_admin(user_id: int) -> bool:
    u = await get_user(user_id)
    return u is not None and bool(u.get("is_admin"))


async def ensure_user(user_id: int, username: str | None = None) -> None:
    """Добавляет пользователя, если нет."""
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                """INSERT OR IGNORE INTO users (user_id, username)
                   VALUES (?, ?)""",
                (user_id, username or ""),
            )
            if username:
                await conn.execute(
                    "UPDATE users SET username = ? WHERE user_id = ?",
                    (username, user_id),
                )
            await conn.commit()
        except Exception:
            logging.exception("DB error in ensure_user")
            raise
    finally:
        await conn.close()


async def set_allowed(user_id: int, allowed: bool) -> bool:
    """Разрешить/запретить. Возвращает True если запись обновлена или создана."""
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                """INSERT INTO users (user_id, allowed) VALUES (?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET allowed = excluded.allowed""",
                (user_id, 1 if allowed else 0),
            )
            await conn.commit()
            return True
        except Exception:
            logging.exception("DB error in set_allowed")
            raise
    finally:
        await conn.close()


async def set_admin(user_id: int, is_admin_flag: bool) -> bool:
    conn = await _get_conn()
    try:
        try:
            cur = await conn.execute(
                "UPDATE users SET is_admin = ? WHERE user_id = ?",
                (1 if is_admin_flag else 0, user_id),
            )
            await conn.commit()
            return cur.rowcount > 0
        except Exception:
            logging.exception("DB error in set_admin")
            raise
    finally:
        await conn.close()


async def inc_tokens(user_id: int) -> None:
    """Увеличивает счётчик успешных токенов."""
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                """UPDATE users SET
                   tokens_count = tokens_count + 1,
                   last_success_at = datetime('now')
                   WHERE user_id = ?""",
                (user_id,),
            )
            await conn.commit()
        except Exception:
            logging.exception("DB error in inc_tokens")
            raise
    finally:
        await conn.close()


async def list_users() -> list[dict]:
    """Список всех пользователей (id, username, allowed, is_admin, tokens_count)."""
    conn = await _get_conn()
    try:
        try:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """SELECT user_id, username, allowed, is_admin, tokens_count, last_success_at, created_at
                   FROM users ORDER BY created_at DESC"""
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception:
            logging.exception("DB error in list_users")
            raise
    finally:
        await conn.close()


async def stats_summary() -> dict:
    """Сводка: всего пользователей, допущенных, токенов."""
    conn = await _get_conn()
    try:
        try:
            cur = await conn.execute(
                """SELECT
                   COUNT(*) as total,
                   SUM(CASE WHEN allowed=1 OR is_admin=1 THEN 1 ELSE 0 END) as allowed_count,
                   SUM(tokens_count) as total_tokens
                   FROM users"""
            )
            row = await cur.fetchone()
            return {
                "total": row[0] or 0,
                "allowed": row[1] or 0,
                "tokens": row[2] or 0,
            }
        except Exception:
            logging.exception("DB error in stats_summary")
            raise
    finally:
        await conn.close()


async def add_user_token(user_id: int, token: str, filename_prefix: str | None = None) -> None:
    """Сохраняет полученный токен для пользователя. filename_prefix — для имени файла при выдаче (номер, token_qr и т.д.)."""
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                "INSERT INTO tokens (user_id, token, filename_prefix) VALUES (?, ?, ?)",
                (user_id, token, filename_prefix or "token"),
            )
            await conn.commit()
        except Exception:
            logging.exception("DB error in add_user_token")
            raise
    finally:
        await conn.close()


async def get_user_tokens_count(user_id: int, only_unused: bool = True) -> int:
    """Возвращает количество токенов пользователя (по умолчанию только невыданные)."""
    conn = await _get_conn()
    try:
        try:
            if only_unused:
                sql = "SELECT COUNT(*) FROM tokens WHERE user_id = ? AND is_used = 0"
            else:
                sql = "SELECT COUNT(*) FROM tokens WHERE user_id = ?"
            cur = await conn.execute(sql, (user_id,))
            row = await cur.fetchone()
            return int(row[0] or 0)
        except Exception:
            logging.exception("DB error in get_user_tokens_count")
            raise
    finally:
        await conn.close()


async def pop_user_tokens(user_id: int, limit: int) -> list[tuple[str, str]]:
    """
    Возвращает и помечает как использованные до `limit` токенов пользователя.
    Возвращает список пар (token, filename_prefix) для сохранения имён файлов при выдаче.
    """
    if limit <= 0:
        return []

    conn = await _get_conn()
    try:
        try:
            # Критическая секция: блокируем запись, чтобы параллельные выдачи
            # не могли выбрать один и тот же набор неиспользованных токенов.
            await conn.execute("BEGIN IMMEDIATE")
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """
            SELECT id, token, COALESCE(filename_prefix, 'token') AS filename_prefix
            FROM tokens
            WHERE user_id = ? AND is_used = 0
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
                (user_id, limit),
            )
            rows = await cur.fetchall()
            if not rows:
                await conn.commit()
                return []

            ids = [r["id"] for r in rows]
            # SQLite ограничивает число параметров (часто 999); батчируем UPDATE по 500
            chunk_size = 500
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i : i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                await conn.execute(
                    f"""
                    UPDATE tokens
                    SET is_used = 1, used_at = datetime('now')
                    WHERE id IN ({placeholders}) AND is_used = 0
                    """,
                    chunk,
                )
            await conn.commit()
            return [(r["token"], r["filename_prefix"]) for r in rows]
        except Exception:
            await conn.rollback()
            logging.exception("DB error in pop_user_tokens")
            raise
    finally:
        await conn.close()


# --- Группы-саппорты: токены, сделанные в группе, идут владельцу группы ---

async def get_support_group(group_id: int) -> dict | None:
    """Возвращает запись support_groups или None."""
    conn = await _get_conn()
    try:
        try:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT group_id, owner_id, title, created_at FROM support_groups WHERE group_id = ?",
                (group_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None
        except Exception:
            logging.exception("DB error in get_support_group")
            raise
    finally:
        await conn.close()


async def register_support_group(group_id: int, owner_id: int, title: str | None = None) -> bool:
    """Регистрирует группу как саппорт-группу. Владелец — кто получает токены. Возвращает True если создано."""
    conn = await _get_conn()
    try:
        try:
            cur = await conn.execute(
                """INSERT INTO support_groups (group_id, owner_id, title)
                   VALUES (?, ?, ?)
                   ON CONFLICT(group_id) DO NOTHING""",
                (group_id, owner_id, title or ""),
            )
            await conn.commit()
            return cur.rowcount > 0
        except Exception:
            logging.exception("DB error in register_support_group")
            raise
    finally:
        await conn.close()


async def add_support_member(group_id: int, user_id: int) -> bool:
    """Добавляет пользователя в список саппортов группы."""
    conn = await _get_conn()
    try:
        try:
            cur = await conn.execute(
                """INSERT OR IGNORE INTO support_group_members (group_id, user_id)
                   VALUES (?, ?)""",
                (group_id, user_id),
            )
            await conn.commit()
            return cur.rowcount > 0
        except Exception:
            logging.exception("DB error in add_support_member")
            raise
    finally:
        await conn.close()


async def remove_support_member(group_id: int, user_id: int) -> bool:
    """Удаляет пользователя из саппортов группы."""
    conn = await _get_conn()
    try:
        try:
            cur = await conn.execute(
                "DELETE FROM support_group_members WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )
            await conn.commit()
            return cur.rowcount > 0
        except Exception:
            logging.exception("DB error in remove_support_member")
            raise
    finally:
        await conn.close()


async def list_support_members(group_id: int) -> list[dict]:
    """Список саппортов группы: [{"user_id": int, "token_access": bool}, ...] (без владельца)."""
    conn = await _get_conn()
    try:
        try:
            cur = await conn.execute(
                "SELECT user_id, COALESCE(token_access, 0) FROM support_group_members WHERE group_id = ? ORDER BY added_at",
                (group_id,),
            )
            rows = await cur.fetchall()
            return [{"user_id": r[0], "token_access": bool(r[1])} for r in rows]
        except Exception:
            logging.exception("DB error in list_support_members")
            raise
    finally:
        await conn.close()


async def set_support_member_token_access(group_id: int, user_id: int, token_access: bool) -> bool:
    """Включить/выключить доступ к токенам группы для саппорта. Возвращает True если запись обновлена."""
    conn = await _get_conn()
    try:
        try:
            cur = await conn.execute(
                """UPDATE support_group_members SET token_access = ? WHERE group_id = ? AND user_id = ?""",
                (1 if token_access else 0, group_id, user_id),
            )
            await conn.commit()
            return cur.rowcount > 0
        except Exception:
            logging.exception("DB error in set_support_member_token_access")
            raise
    finally:
        await conn.close()


async def support_member_has_token_access(group_id: int, user_id: int) -> bool:
    """Есть ли у саппорта доступ к токенам группы. Для владельца не вызывать — у владельца всегда есть."""
    conn = await _get_conn()
    try:
        try:
            cur = await conn.execute(
                "SELECT COALESCE(token_access, 0) FROM support_group_members WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )
            row = await cur.fetchone()
            return bool(row and row[0])
        except Exception:
            logging.exception("DB error in support_member_has_token_access")
            raise
    finally:
        await conn.close()


async def inc_support_group_token_stats(group_id: int, user_id: int) -> None:
    """Увеличивает счётчик токенов, сделанных пользователем в этой группе (кто нажал QR/номер)."""
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                """
                INSERT INTO support_group_token_stats (group_id, user_id, tokens_count, last_at)
                VALUES (?, ?, 1, datetime('now'))
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    tokens_count = tokens_count + 1,
                    last_at = datetime('now')
                """,
                (group_id, user_id),
            )
            await conn.commit()
        except Exception:
            logging.exception("DB error in inc_support_group_token_stats")
            raise
    finally:
        await conn.close()


async def get_support_group_stats(group_id: int) -> list[dict]:
    """Статистика по группе: кто сколько токенов сделал. Список dict с user_id, username, tokens_count."""
    conn = await _get_conn()
    try:
        try:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """SELECT s.user_id, s.tokens_count, s.last_at, u.username
                   FROM support_group_token_stats s
                   LEFT JOIN users u ON u.user_id = s.user_id
                   WHERE s.group_id = ?
                   ORDER BY s.tokens_count DESC""",
                (group_id,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception:
            logging.exception("DB error in get_support_group_stats")
            raise
    finally:
        await conn.close()


async def reset_support_group_stats(group_id: int) -> None:
    """Обнуляет статистику токенов по группе (все счётчики для этой группы)."""
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                "DELETE FROM support_group_token_stats WHERE group_id = ?",
                (group_id,),
            )
            await conn.commit()
        except Exception:
            logging.exception("DB error in reset_support_group_stats")
            raise
    finally:
        await conn.close()


async def get_balance_cents(user_id: int) -> int:
    """Баланс пользователя в центах USD (1 USD = 100 центов)."""
    conn = await _get_conn()
    try:
        try:
            cur = await conn.execute(
                "SELECT COALESCE(balance_cents, 0) FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0
        except Exception:
            logging.exception("DB error in get_balance_cents")
            raise
    finally:
        await conn.close()


async def try_debit_balance_cents(user_id: int, cents: int) -> bool:
    """
    Снимает cents с balance_cents, если хватает. При cents <= 0 возвращает True без изменений.
    """
    if cents <= 0:
        return True
    conn = await _get_conn()
    try:
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT COALESCE(balance_cents, 0) FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cur.fetchone()
            bal = int(row[0]) if row else 0
            if bal < cents:
                await conn.rollback()
                return False
            await conn.execute(
                """
                UPDATE users SET balance_cents = COALESCE(balance_cents, 0) - ?
                WHERE user_id = ?
                """,
                (cents, user_id),
            )
            await conn.commit()
            return True
        except Exception:
            await conn.rollback()
            logging.exception("DB error in try_debit_balance_cents")
            raise
    finally:
        await conn.close()


async def admin_set_balance_cents(user_id: int, cents: int) -> None:
    """Задаёт абсолютный баланс пользователя в центах USD (пользователь должен существовать — вызовите ensure_user)."""
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                "UPDATE users SET balance_cents = ? WHERE user_id = ?",
                (cents, user_id),
            )
            await conn.commit()
        except Exception:
            logging.exception("DB error in admin_set_balance_cents")
            raise
    finally:
        await conn.close()


async def insert_crypto_invoice(
    user_id: int,
    invoice_id: int,
    amount_cents: int,
    payload: str,
    notify_chat_id: int | None = None,
    notify_thread_id: int | None = None,
) -> int:
    """Сохраняет созданный в Crypto Pay счёт. Возвращает локальный id (для callback)."""
    conn = await _get_conn()
    try:
        try:
            cur = await conn.execute(
                """
                INSERT INTO crypto_invoices (
                    user_id, invoice_id, amount_cents, payload, status,
                    notify_chat_id, notify_thread_id
                )
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    user_id,
                    invoice_id,
                    amount_cents,
                    payload[:4096] if payload else "",
                    notify_chat_id,
                    notify_thread_id,
                ),
            )
            await conn.commit()
            return int(cur.lastrowid)
        except Exception:
            logging.exception("DB error in insert_crypto_invoice")
            raise
    finally:
        await conn.close()


async def get_crypto_invoice_by_local_id(local_id: int) -> dict | None:
    """Запись счёта по локальному id (для фоновой проверки оплаты)."""
    conn = await _get_conn()
    try:
        try:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """
                SELECT id, user_id, invoice_id, amount_cents, payload, status, created_at, paid_at,
                       notify_chat_id, notify_thread_id, notify_sent
                FROM crypto_invoices WHERE id = ?
                """,
                (local_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None
        except Exception:
            logging.exception("DB error in get_crypto_invoice_by_local_id")
            raise
    finally:
        await conn.close()


async def get_crypto_invoice_local(local_id: int, user_id: int) -> dict | None:
    """Локальная запись счёта по id и user_id."""
    conn = await _get_conn()
    try:
        try:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """
                SELECT id, user_id, invoice_id, amount_cents, payload, status, created_at, paid_at,
                       notify_chat_id, notify_thread_id, notify_sent
                FROM crypto_invoices WHERE id = ? AND user_id = ?
                """,
                (local_id, user_id),
            )
            row = await cur.fetchone()
            return dict(row) if row else None
        except Exception:
            logging.exception("DB error in get_crypto_invoice_local")
            raise
    finally:
        await conn.close()


async def finalize_paid_crypto_invoice(local_id: int, user_id: int) -> bool:
    """
    Атомарно: если счёт pending — помечает paid и зачисляет balance_cents.
    Возвращает True если зачисление выполнено, False если уже было или запись не найдена.
    """
    conn = await _get_conn()
    try:
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                """
                SELECT amount_cents FROM crypto_invoices
                WHERE id = ? AND user_id = ? AND status = 'pending'
                """,
                (local_id, user_id),
            )
            row = await cur.fetchone()
            if not row:
                await conn.rollback()
                return False
            amount_cents = int(row[0])
            cur2 = await conn.execute(
                """
                UPDATE crypto_invoices
                SET status = 'paid', paid_at = datetime('now')
                WHERE id = ? AND user_id = ? AND status = 'pending'
                """,
                (local_id, user_id),
            )
            if cur2.rowcount == 0:
                await conn.rollback()
                return False
            await conn.execute(
                """
                UPDATE users
                SET balance_cents = COALESCE(balance_cents, 0) + ?
                WHERE user_id = ?
                """,
                (amount_cents, user_id),
            )
            await conn.commit()
            return True
        except Exception:
            await conn.rollback()
            logging.exception("DB error in finalize_paid_crypto_invoice")
            raise
    finally:
        await conn.close()


async def list_pending_crypto_invoices(limit: int = 100) -> list[dict]:
    """Счета в статусе pending для фоновой проверки оплаты."""
    conn = await _get_conn()
    try:
        try:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """
                SELECT id, user_id, invoice_id, amount_cents, notify_chat_id, notify_thread_id,
                       notify_sent, payload
                FROM crypto_invoices
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception:
            logging.exception("DB error in list_pending_crypto_invoices")
            raise
    finally:
        await conn.close()


async def mark_crypto_invoice_notified(local_id: int) -> None:
    """Помечает, что оповещение о пополнении уже отправлено (кнопка или авто)."""
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                "UPDATE crypto_invoices SET notify_sent = 1 WHERE id = ?",
                (local_id,),
            )
            await conn.commit()
        except Exception:
            logging.exception("DB error in mark_crypto_invoice_notified")
            raise
    finally:
        await conn.close()


async def is_support_group_member_or_owner(group_id: int, user_id: int) -> bool:
    """Проверяет, может ли user_id использовать бота в этой группе (владелец или в списке саппортов)."""
    g = await get_support_group(group_id)
    if not g:
        return False
    if g["owner_id"] == user_id:
        return True
    conn = await _get_conn()
    try:
        try:
            cur = await conn.execute(
                "SELECT 1 FROM support_group_members WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )
            return (await cur.fetchone()) is not None
        except Exception:
            logging.exception("DB error in is_support_group_member_or_owner")
            raise
    finally:
        await conn.close()


async def get_app_setting(key: str) -> str | None:
    """Возвращает значение настройки по ключу или None."""
    conn = await _get_conn()
    try:
        try:
            cur = await conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (key,),
            )
            row = await cur.fetchone()
            return row[0] if row else None
        except Exception:
            logging.exception("DB error in get_app_setting")
            raise
    finally:
        await conn.close()


async def set_app_setting(key: str, value: str) -> None:
    """Сохраняет (upsert) настройку приложения."""
    conn = await _get_conn()
    try:
        try:
            await conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = datetime('now')
                """,
                (key, value),
            )
            await conn.commit()
        except Exception:
            logging.exception("DB error in set_app_setting")
            raise
    finally:
        await conn.close()


async def get_token_price_cents(default_cents: int = 20) -> int:
    """
    Возвращает цену создания токена в центах из app_settings.
    Если настройка отсутствует/повреждена — default_cents.
    """
    raw = await get_app_setting("token_price_cents")
    if raw is None:
        return max(0, int(default_cents))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return max(0, int(default_cents))


async def set_token_price_cents(cents: int) -> None:
    """Сохраняет цену создания токена в центах."""
    await set_app_setting("token_price_cents", str(max(0, int(cents))))
