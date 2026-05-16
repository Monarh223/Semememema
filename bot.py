"""
Telegram-бот: открывает web.max.ru, шлёт QR пользователю,
после сканирования получает блок из localStorage и присылает его в чат.
Команда /link_phone — привязка по номеру: SMS → веб-токен → тот же блок.
Доступ только по разрешению админа. БД: учёт пользователей, счёт токенов.
"""
import asyncio
import logging
import os
import re
import random
import secrets
import sqlite3
import types
from io import BytesIO
from pathlib import Path
from typing import Awaitable, Callable

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, Conflict
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from db import (
    ensure_user,
    inc_tokens,
    init_db,
    is_admin,
    is_allowed,
    list_users,
    set_admin,
    set_allowed,
    stats_summary,
    add_user_token,
    get_user_tokens_count,
    pop_user_tokens,
    list_proxies_meta,
    log_proxy_add,
    log_proxy_remove,
    get_support_group,
    register_support_group,
    add_support_member,
    remove_support_member,
    list_support_members,
    set_support_member_token_access,
    support_member_has_token_access,
    is_support_group_member_or_owner,
    get_user,
    inc_support_group_token_stats,
    get_support_group_stats,
    reset_support_group_stats,
    is_phone_used,
    add_used_phone,
    get_balance_cents,
    try_debit_balance_cents,
    admin_set_balance_cents,
    insert_crypto_invoice,
    get_crypto_invoice_local,
    get_crypto_invoice_by_local_id,
    finalize_paid_crypto_invoice,
    list_pending_crypto_invoices,
    mark_crypto_invoice_notified,
    get_token_price_cents,
    set_token_price_cents,
)
from cryptopay import (
    create_invoice_usd,
    get_invoice_by_id,
    is_cryptopay_configured,
)
from browser_max import (
    PROXY_EXHAUSTED_MSG,
    check_proxy_ip,
    get_proxy_for_request_async,
    run_max_qr_flow,
    run_max_qr_flow_with_auth,
)
try:
    from proxy_seller_sync import sync_proxies_with_file_and_db, ProxySellerError
except ImportError:
    sync_proxies_with_file_and_db = None
    ProxySellerError = Exception  # чтобы except ProxySellerError не падал


def _get_proxy_seller_sync():
    """Возвращает (sync_proxies_with_file_and_db, ProxySellerError). При вызове повторно пробует импорт."""
    global sync_proxies_with_file_and_db, ProxySellerError
    if sync_proxies_with_file_and_db is not None:
        return sync_proxies_with_file_and_db, ProxySellerError
    try:
        from proxy_seller_sync import sync_proxies_with_file_and_db as _fn, ProxySellerError as _err
        sync_proxies_with_file_and_db = _fn
        ProxySellerError = _err
        return _fn, _err
    except ImportError as e:
        return None, type("ProxySellerError", (Exception,), {})(str(e))


load_dotenv()

try:
    from register_account import (
        get_login_token_by_phone_async,
        get_web_token_via_qr_async,
        complete_registration_async,
        make_client_profile,
    )
    _register_account_error = ""
except ImportError as e:
    get_login_token_by_phone_async = None
    get_web_token_via_qr_async = None
    complete_registration_async = None
    make_client_profile = None
    _register_account_error = str(e)

try:
    from pymax.core import MaxClient
    from pymax.payloads import UserAgentPayload
    _PYMAX_AVAILABLE = True
except Exception:
    MaxClient = None  # type: ignore[assignment]
    UserAgentPayload = None  # type: ignore[assignment]
    _PYMAX_AVAILABLE = False

from names_pool import get_random_russian_name

# Ожидание пароля 2FA в потоке «по номеру»: chat_id -> Future[str]
# Ключ — (chat_id, message_thread_id). В группе с топиками у каждого топика свой ключ.
_password_waiters: dict[tuple[int, int | None], asyncio.Future] = {}
# Фоновые задачи (QR, link_phone) — отменяем при остановке бота
_background_tasks: set[asyncio.Task] = set()
# Ограничение одновременных QR-флоу на пользователя: chat_id -> активные задачи
_qr_active_count: dict[int, int] = {}
# Активные задачи QR по flow_id для точечной отмены по кнопке
_qr_tasks: dict[str, asyncio.Task] = {}
PROXIES_FILE = Path(__file__).with_name("proxies.txt")

# Пользователи, у которых включён режим выдачи токенов в ZIP
_zip_mode_users: set[int] = set()


def _get_message_thread_id(update: Update) -> int | None:
    """ID топика (темы) в группе с форумом. None — личка или группа без топиков."""
    if update.message and getattr(update.message, "message_thread_id", None) is not None:
        return update.message.message_thread_id
    if update.callback_query and update.callback_query.message:
        return getattr(update.callback_query.message, "message_thread_id", None)
    return None


def _send_kwargs(chat_id: int, message_thread_id: int | None) -> dict:
    """kwargs для bot.send_message/send_photo/send_document: chat_id и при наличии — message_thread_id."""
    out: dict = {"chat_id": chat_id}
    if message_thread_id is not None:
        out["message_thread_id"] = message_thread_id
    return out


async def _edit_or_resend_text(
    query,
    text: str,
    bot,
    parse_mode: str | None = None,
    reply_markup=None,
) -> None:
    """Редактирует сообщение; если это фото (нет текста) — удаляет и отправляет новое текстовое."""
    try:
        await query.edit_message_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup
        )
    except BadRequest as e:
        err = str(e).lower()
        if "not modified" in err:
            return
        if "no text" in err or "message to edit" in err:
            await query.delete_message()
            thread_id = getattr(query.message, "message_thread_id", None) if query.message else None
            kw = _send_kwargs(query.message.chat_id, thread_id)
            await bot.send_message(
                **kw,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        else:
            raise


def _menu_photo_path() -> Path | None:
    """Путь к фото для панели меню. В .env: MENU_PHOTO_PATH=путь/к/фото.jpg (относительно папки бота или абсолютный).
    Если переменная не задана или файл не найден — проверяются файлы maxmenu.jpg и menu_photo.jpg в папке бота (удобно на сервере)."""
    script_dir = Path(__file__).resolve().parent
    raw = os.getenv("MENU_PHOTO_PATH", "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = script_dir / p
        if p.is_file():
            return p
        logging.warning("Меню: фото из MENU_PHOTO_PATH не найдено: %s", p)
    for name in ("maxmenu.jpg", "menu_photo.jpg", "menu.jpg"):
        candidate = script_dir / name
        if candidate.is_file():
            return candidate
    return None


def _create_background_task(coro):
    """Запускает корутину в фоне и добавляет задачу в _background_tasks для отмены при shutdown."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    def _done(t):
        _background_tasks.discard(t)
    task.add_done_callback(_done)
    return task


async def _get_admin_ids() -> list[int]:
    """Список user_id всех админов (для рассылки уведомлений о прокси)."""
    users = await list_users()
    return [u["user_id"] for u in users if u.get("is_admin")]


async def _proxy_seller_poll_loop(app: Application) -> None:
    """
    Периодически опрашивает API Proxy-Seller, синхронизирует прокси с файлом (добавляет новые,
    удаляет те, которых уже нет в API), уведомляет админов об изменениях.
    Интервал задаётся в .env: PROXY_SELLER_POLL_MINUTES (по умолчанию 10). 0 = отключить опрос.
    """
    poll_min = float(os.getenv("PROXY_SELLER_POLL_MINUTES", "10").strip() or "0")
    if poll_min <= 0:
        logging.info("Опрос Proxy-Seller отключён (PROXY_SELLER_POLL_MINUTES=0 или не задан).")
        return
    interval_sec = max(60.0, poll_min * 60.0)
    sync_fn, ProxySellerErr = _get_proxy_seller_sync()
    if sync_fn is None:
        logging.info("Опрос Proxy-Seller не запущен: API недоступен.")
        return
    logging.info("Опрос Proxy-Seller каждые %.0f мин.", poll_min)
    while True:
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            break
        try:
            added, removed = await sync_fn(merge=False)
            if added == 0 and removed == 0:
                continue
            admin_ids = await _get_admin_ids()
            if not admin_ids:
                continue
            text = (
                "🔄 <b>Прокси обновлены из Proxy-Seller</b>\n\n"
                f"Добавлено: {added}\n"
                f"Удалено (закончились/нет в API): {removed}"
            )
            for uid in admin_ids:
                try:
                    await app.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
                except Exception:
                    pass
        except asyncio.CancelledError:
            break
        except ProxySellerErr as e:
            logging.warning("Proxy-Seller опрос: %s", e)
        except Exception as e:
            logging.exception("Proxy-Seller опрос: %s", e)


async def _send_topup_paid_message(
    app: Application,
    *,
    chat_id: int,
    thread_id: int | None,
    amount_cents: int,
) -> None:
    """Короткое уведомление в чат после авто-определения оплаты."""
    kw = _send_kwargs(chat_id, thread_id)
    await app.bot.send_message(
        **kw,
        text=_text_balance_topped_up(amount_cents),
        parse_mode="HTML",
    )


async def _crypto_pay_poll_loop(app: Application) -> None:
    """
    Фоновый опрос Crypto Pay: если счёт оплачен, зачисляем и шлём уведомление в тот же чат.
    Интервал: CRYPTOPAY_POLL_SECONDS (по умолчанию 12), 0 = выключить.
    """
    raw = os.getenv("CRYPTOPAY_POLL_SECONDS", "12").strip()
    interval = float(raw or "12")
    if interval <= 0:
        logging.info("Опрос Crypto Pay отключён (CRYPTOPAY_POLL_SECONDS=0).")
        return
    await asyncio.sleep(4)
    logging.info("Фоновая проверка оплаты Crypto Pay каждые %.0f с.", interval)
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        if not is_cryptopay_configured():
            continue
        try:
            pending = await list_pending_crypto_invoices(limit=80)
            if not pending:
                continue
            for row in pending:
                try:
                    local_id = int(row["id"])
                    uid = int(row["user_id"])
                    api_iid = int(row["invoice_id"])
                    inv = await get_invoice_by_id(api_iid)
                    if not inv or str(inv.get("status", "")).lower() != "paid":
                        continue
                    await finalize_paid_crypto_invoice(local_id, uid)
                    fresh = await get_crypto_invoice_by_local_id(local_id)
                    if not fresh or fresh.get("status") != "paid":
                        continue
                    if int(fresh.get("notify_sent") or 0):
                        continue
                    chat_id = fresh.get("notify_chat_id")
                    if chat_id is None:
                        chat_id = uid
                    thread_id = fresh.get("notify_thread_id")
                    amt_cents = int(fresh.get("amount_cents") or 0)
                    await _send_topup_paid_message(
                        app,
                        chat_id=int(chat_id),
                        thread_id=thread_id,
                        amount_cents=amt_cents,
                    )
                    await mark_crypto_invoice_notified(local_id)
                except Exception:
                    logging.exception("Crypto Pay poll: счёт id=%s", row.get("id"))
        except asyncio.CancelledError:
            break
        except Exception:
            logging.exception("Crypto Pay poll loop")


class SkipAuthorizationError(Exception):
    """Пользователь нажал «Пропустить» — кода нет, не трактовать след. сообщение как код."""



def make_session_block(device_id: str, auth_token: str) -> str:
    """Тот же формат, что и блок из browser_max (localStorage + reload)."""
    lines = [
        "sessionStorage.clear();",
        "localStorage.clear();",
        f'localStorage.setItem("__oneme_device_id", {repr(device_id)});',
        f'localStorage.setItem("__oneme_auth", {repr(auth_token)});',
        "window.location.reload();",
    ]
    return "\n".join(lines)


async def _send_tokens_as_txt_file(
    chat_id: int,
    app: Application,
    blocks: list[str],
    filename_prefix: str = "max_session",
    caption: str | None = None,
    message_thread_id: int | None = None,
    blocks_with_prefixes: list[tuple[str, str]] | None = None,
    zip_mode: bool = False,
    auto_zip_threshold: int = 15,
) -> None:
    """Отправляет блоки в виде .txt-файлов или одного ZIP.
    Если задан blocks_with_prefixes — список (token, filename_prefix).
    При zip_mode или количестве блоков > auto_zip_threshold все файлы упаковываются в ZIP.
    """
    if not blocks and not blocks_with_prefixes:
        return
    kw = _send_kwargs(chat_id, message_thread_id)
    default_caption = "Файл с данными для переноса сессии. Скопируйте содержимое в нужное место."

    # Нормализуем список в формат (block, prefix)
    items: list[tuple[str, str]] = []
    if blocks_with_prefixes:
        items.extend(list(blocks_with_prefixes))
    elif blocks:
        for i, block in enumerate(blocks):
            pref = f"{filename_prefix}_{i + 1}" if len(blocks) > 1 else filename_prefix
            items.append((block, pref))

    if not items:
        return

    # Решаем, нужен ли ZIP
    use_zip = zip_mode or len(items) > auto_zip_threshold
    if use_zip:
        from zipfile import ZipFile, ZIP_DEFLATED

        bio = BytesIO()
        with ZipFile(bio, "w", ZIP_DEFLATED) as zf:
            for i, (block, prefix) in enumerate(items, start=1):
                if len(items) > 1:
                    fname = f"{prefix}_{i}.txt"
                else:
                    fname = f"{prefix}.txt"
                zf.writestr(fname, block)
        bio.seek(0)
        bio.name = "max_tokens.zip"
        await app.bot.send_document(
            **kw,
            document=bio,
            filename=bio.name,
            caption=caption or "ZIP‑архив с токенами.",
        )
        return

    # Обычная отправка .txt файлов по одному
    for i, (block, prefix) in enumerate(items, start=1):
        bio = BytesIO(block.encode("utf-8"))
        if len(items) > 1:
            filename = f"{prefix}_{i}.txt"
            file_caption = caption if i == 1 else f"Токен {i} из {len(items)}."
        else:
            filename = f"{prefix}.txt"
            file_caption = caption or default_caption
        bio.name = filename
        await app.bot.send_document(
            **kw,
            document=bio,
            filename=filename,
            caption=file_caption,
        )


def _phone_to_filename_prefix(phone: str) -> str:
    """Из номера +79001234567 делаем префикс для имени файла: 79001234567 (только цифры)."""
    return re.sub(r"\D", "", phone) or "phone"


async def poll_playwright_and_send(
    reply_chat_id: int,
    credit_user_id: int,
    app: Application,
    reply_thread_id: int | None = None,
    actor_user_id: int | None = None,
    qr_flow_id: str | None = None,
) -> None:
    """Запускает флоу: шлёт QR в reply_chat_id (в топик reply_thread_id при наличии); блок туда же; токены — credit_user_id.
    actor_user_id — кто нажал (для статистики и подписи «Токен QR • ник»)."""
    kw = _send_kwargs(reply_chat_id, reply_thread_id)
    gen = run_max_qr_flow(poll_interval=2.0, timeout=300.0)
    try:
        qr_bytes = await gen.__anext__()
        cancel_data = f"qr_cancel:{qr_flow_id}" if qr_flow_id else "qr_cancel"
        cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data=cancel_data)],
        ])
        await app.bot.send_photo(
            **kw,
            photo=BytesIO(qr_bytes),
            caption=(
                "Сканируйте QR в приложении MAX:\n"
                "Профиль → Устройства → Войти по QR-коду.\n\n"
                "После сканирования пришлю готовый блок для переноса сессии."
            ),
            reply_markup=cancel_kb,
        )
        block = await gen.__anext__()
        if block:
            if not await _can_save_token_to_vault(
                app, credit_user_id, reply_chat_id, reply_thread_id
            ):
                if reply_chat_id < 0 and actor_user_id is not None and actor_user_id != credit_user_id:
                    await app.bot.send_message(
                        **_send_kwargs(reply_chat_id, reply_thread_id),
                        text=(
                            "⚠️ У владельца недостаточно средств — токен не сохранён в «Мои токены». "
                            "Файл с сессией ниже."
                        ),
                    )
                actor_row = await get_user(actor_user_id) if actor_user_id else None
                actor_nick = f"@{actor_row['username']}" if actor_row and actor_row.get("username") else (f"ID{actor_user_id}" if actor_user_id else "")
                qr_caption = f"Токен QR • {actor_nick}" if actor_nick else "Токен QR"
                qr_caption += " (не сохранён в боте — не хватило баланса)"
                await _send_tokens_as_txt_file(
                    reply_chat_id,
                    app,
                    [block],
                    filename_prefix="token_qr",
                    caption=qr_caption,
                    message_thread_id=reply_thread_id,
                )
                return
            await inc_tokens(credit_user_id)
            if reply_chat_id < 0 and actor_user_id is not None:
                await inc_support_group_token_stats(reply_chat_id, actor_user_id)
            await add_user_token(credit_user_id, block, "token_qr")
            # В группе саппорт не получает файл — токен только владельцу, он запросит через «Мои токены»
            if reply_chat_id < 0 and actor_user_id is not None and actor_user_id != credit_user_id:
                await app.bot.send_message(
                    **_send_kwargs(reply_chat_id, reply_thread_id),
                    text="✅ Токен успешно добавлен владельцу группы. Он может запросить его через «Мои токены».",
                )
            else:
                actor_row = await get_user(actor_user_id) if actor_user_id else None
                actor_nick = f"@{actor_row['username']}" if actor_row and actor_row.get("username") else (f"ID{actor_user_id}" if actor_user_id else "")
                qr_caption = f"Токен QR • {actor_nick}" if actor_nick else "Токен QR"
                await _send_tokens_as_txt_file(
                    reply_chat_id,
                    app,
                    [block],
                    filename_prefix="token_qr",
                    caption=qr_caption,
                    message_thread_id=reply_thread_id,
                )
        else:
            await app.bot.send_message(
                **kw,
                text="Время ожидания вышло. Отправьте /qr и попробуйте снова.",
            )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await app.bot.send_message(**kw, text=f"Ошибка: {e}")
    finally:
        await gen.aclose()


async def _limited_qr_flow(
    reply_chat_id: int,
    credit_user_id: int,
    app: Application,
    reply_thread_id: int | None = None,
    actor_user_id: int | None = None,
    qr_flow_id: str | None = None,
) -> None:
    """Обёртка над poll_playwright_and_send с учётом лимита активных QR-флоу на чат."""
    _qr_active_count[reply_chat_id] = _qr_active_count.get(reply_chat_id, 0) + 1
    try:
        await poll_playwright_and_send(
            reply_chat_id,
            credit_user_id,
            app,
            reply_thread_id,
            actor_user_id,
            qr_flow_id,
        )
    finally:
        current = _qr_active_count.get(reply_chat_id, 1)
        if current <= 1:
            _qr_active_count.pop(reply_chat_id, None)
        else:
            _qr_active_count[reply_chat_id] = current - 1


def _start_qr_flow(
    reply_chat_id: int,
    credit_user_id: int,
    app: Application,
    reply_thread_id: int | None = None,
    actor_user_id: int | None = None,
) -> str:
    """Запускает QR-флоу в фоне и сохраняет задачу для отмены по кнопке."""
    qr_flow_id = secrets.token_urlsafe(8)
    task = _create_background_task(
        _limited_qr_flow(
            reply_chat_id,
            credit_user_id,
            app,
            reply_thread_id,
            actor_user_id,
            qr_flow_id,
        )
    )
    _qr_tasks[qr_flow_id] = task

    def _done(t):
        _qr_tasks.pop(qr_flow_id, None)

    task.add_done_callback(_done)
    return qr_flow_id




WORK_MODE = True

def _admin_panel_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📊 Статистика", callback_data="admin:stats"),
            InlineKeyboardButton("👥 Пользователи", callback_data="admin:users"),
        ],
        [
            InlineKeyboardButton("🔌 Прокси", callback_data="admin:proxies"),
            InlineKeyboardButton("💵 Прайс", callback_data="admin:price"),
        ],
        [
            InlineKeyboardButton("📢 Рассылка", callback_data="admin:broadcast"),
            InlineKeyboardButton("⚙️ Настройки", callback_data="admin:settings"),
        ],
        [
            InlineKeyboardButton("🔘 ВКЛ/ВЫКЛ WORK", callback_data="admin:work"),
        ],
    ]
    return InlineKeyboardMarkup(rows)

ACCESS_DENIED = "Доступ запрещён. Ожидайте одобрения администратора."

# Контекст «чат для ответа» и «кому зачислять токены» (в группе-саппорте — владельцу)
async def _get_reply_and_credit(update: Update) -> tuple[int, int] | None:
    """
    Возвращает (reply_chat_id, credit_user_id) если пользователь может пользоваться ботом в этом чате:
    - В личке: (user_id, user_id) если у пользователя есть доступ.
    - В группе-саппорте: (group_id, owner_id) если пользователь — владелец или в списке саппортов.
    Иначе None.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return None
    await ensure_user(user.id, user.username or user.first_name or "")
    if chat.type in ("group", "supergroup"):
        group_id = chat.id
        g = await get_support_group(group_id)
        if not g:
            return None
        if not await is_support_group_member_or_owner(group_id, user.id):
            return None
        return (group_id, g["owner_id"])
    # Личка
    if not await is_allowed(user.id):
        return None
    return (user.id, user.id)

# Тексты кнопок меню (для обработки нажатий)
BTN_QR = "📱 QR-код"
BTN_PHONE = "📞 По номеру"
BTN_PROXY = "🔍 Проверить прокси"
BTN_MENU = "📋 Меню"
BTN_HELP = "❓ Помощь"

# Ссылка на поддержку (без @)
SUPPORT_USERNAME = "maxtokensupp"
SUPPORT_LINK = f"https://t.me/{SUPPORT_USERNAME}"

# Лимит длины одного сообщения в Telegram
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
# Постраничный список в админке «Пользователи»
ADMIN_USERS_PAGE_SIZE = 10

# После ошибок в флоу «по номеру» — подсказка и кнопка повтора
LINK_PHONE_RETRY_SUFFIX = "\n\nПовторите попытку или введите другой номер: /link_phone"
LINK_PHONE_RETRY_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("📞 Повторить / другой номер", callback_data="menu:phone")],
])


def _split_message(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Разбивает текст на части не длиннее max_len по границам строк."""
    if len(text) <= max_len:
        return [text] if text else []
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        cut = rest[:max_len].rfind("\n")
        if cut <= 0:
            cut = max_len
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    return chunks


async def _admin_show_proxies(update: Update, bot=None) -> None:
    """Показывает список прокси из файла с датой добавления (без HTML-разметки)."""
    try:
        try:
            lines = PROXIES_FILE.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        lines = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]

        meta_rows = await list_proxies_meta()
        meta = {row["proxy"]: row for row in meta_rows}

        if not lines:
            text = "В файле proxies.txt сейчас нет прокси."
        else:
            out: list[str] = ["Прокси из proxies.txt:\n"]
            for idx, p in enumerate(lines, start=1):
                m = meta.get(p)
                added = m.get("added_at") if m else "?"
                removed = m.get("removed_at") if m else None
                status = "удалён" if removed else "активен"
                out.append(f"{idx}. {p}\n   добавлен: {added} ({status})")
            text = "\n".join(out)
        text += (
            "\n\nКоманды:\n"
            "/admin proxy_add ip:port:user:pass — добавить\n"
            "/admin proxy_del <номер|строка> — удалить"
        )
    except Exception as e:
        text = f"Ошибка при чтении proxies.txt или БД: {e}"

    # Обработка и команды (/admin proxies), и колбэка (через fake_update)
    # Разбиваем на части, если текст длиннее лимита Telegram
    chunks = _split_message(text)
    send_target = update.message
    chat_id = update.effective_chat.id if update.effective_chat else None
    proxy_refresh_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("↻ Обновить из Proxy-Seller", callback_data="admin:proxy_refresh")],
        [InlineKeyboardButton("◀ Назад", callback_data="admin:menu")],
    ]) if bot else None

    for i, part in enumerate(chunks):
        is_last = i == len(chunks) - 1
        reply_markup = proxy_refresh_kb if (is_last and proxy_refresh_kb) else None
        if send_target:
            await send_target.reply_text(part, reply_markup=reply_markup)
        elif bot is not None and chat_id is not None:
            await bot.send_message(chat_id=chat_id, text=part, reply_markup=reply_markup)


async def _admin_add_proxy(update: Update, proxy_line: str) -> None:
    proxy_line = proxy_line.strip()
    if not proxy_line:
        await update.message.reply_text(
            "Укажите прокси: /admin proxy_add ip:port:user:pass\n"
            "Можно сразу пачкой, по одному на строку."
        )
        return
    # Разбиваем пачку: переносы строк / запятые
    batch = proxy_line.replace(",", "\n").splitlines()
    to_add = [p.strip() for p in batch if p.strip()]
    if not to_add:
        await update.message.reply_text("Не нашёл ни одной непустой строки с прокси.")
        return
    try:
        lines = PROXIES_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    cleaned = [l.strip() for l in lines if l.strip()]
    added = []
    skipped = []
    for p in to_add:
        if p in cleaned:
            skipped.append(p)
            continue
        cleaned.append(p)
        added.append(p)
    PROXIES_FILE.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
    for p in added:
        await log_proxy_add(p)
    if not added:
        await update.message.reply_text("Все указанные прокси уже есть в proxies.txt.")
        return
    text_lines = [f"Добавлено прокси: {len(added)}"]
    for p in added:
        text_lines.append(f"• <code>{p}</code>")
    if skipped:
        text_lines.append(f"\nПропущено (уже были): {len(skipped)}")
    await update.message.reply_text("\n".join(text_lines), parse_mode="HTML")


async def _admin_delete_proxy(update: Update, target: str) -> None:
    target = target.strip()
    try:
        lines = PROXIES_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        await update.message.reply_text("Файл proxies.txt не найден.")
        return
    stripped = [l.strip() for l in lines if l.strip()]
    if not stripped:
        await update.message.reply_text("Список прокси пуст.")
        return

    proxy_to_remove: str | None = None
    if target.isdigit():
        idx = int(target)
        if not (1 <= idx <= len(stripped)):
            await update.message.reply_text(f"Номер вне диапазона 1..{len(stripped)}.")
            return
        proxy_to_remove = stripped[idx - 1]
    else:
        for p in stripped:
            if p == target:
                proxy_to_remove = p
                break
        if proxy_to_remove is None:
            await update.message.reply_text("Такой прокси в файле не найден.")
            return

    new_lines = [p for p in stripped if p != proxy_to_remove]
    PROXIES_FILE.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
    await log_proxy_remove(proxy_to_remove)
    await update.message.reply_text(f"Прокси удалён:\n<code>{proxy_to_remove}</code>", parse_mode="HTML")


def _main_keyboard() -> ReplyKeyboardMarkup:
    """Пустая reply-клавиатура для совместимости."""
    return ReplyKeyboardMarkup([[]], resize_keyboard=True)

def _welcome_text() -> str:
    return (
        "🔐 <b>FAST MAX BOT</b>\n\n"
        "Сдача номеров MAX нерегов!\n\n"
        "━━━━━━━━━━━━━━\n"
        "👇 <b>Выберите действие:</b>"
    )


async def _welcome_caption_html(user_id: int) -> str:
    cents = await get_balance_cents(user_id)
    usd = cents / 100.0

    numbers = 0
    try:
        numbers = await get_user_tokens_count(user_id, only_unused=False)
    except Exception:
        pass

    price = 0.0
    try:
        price = (await _token_creation_price_cents()) / 100.0
    except Exception:
        pass

    return (
        _welcome_text()
        + f"\n\n💰 <b>Баланс:</b> ${usd:.2f} USD"
        + f"\n📊 <b>Сдано номеров:</b> {numbers}"
        + f"\n📌 <b>Прайс на сдачу:</b> ${price:.1f}"
    )


def _format_usd(cents: int) -> str:
    return f"${cents / 100.0:.2f}"


def _text_balance_topped_up(amount_cents: int) -> str:
    """Короткое уведомление: баланс пополнен на указанную сумму."""
    return f"✅ Ваш баланс пополнен на <b>{_format_usd(amount_cents)} USD</b>."


def _cryptopay_fee_line() -> str:
    """
    Строка про комиссию для экрана оплаты.
    CRYPTOPAY_FEE_TEXT — полный произвольный текст (приоритет).
    Иначе CRYPTOPAY_FEE_PERCENT (по умолчанию 3) — ориентировочный %.
    """
    custom = os.getenv("CRYPTOPAY_FEE_TEXT", "").strip()
    if custom:
        return custom
    pct = os.getenv("CRYPTOPAY_FEE_PERCENT", "3").strip()
    try:
        float(pct.replace(",", "."))
        return (
            f"Комиссия Crypto Bot: <b>~{pct}%</b> "
            f"(точная сумма в USDT показывается при оплате)"
        )
    except ValueError:
        return "Комиссия удерживается Crypto Bot при оплате (см. в приложении)."


# Пополнение через Crypto Pay (USD, центы)
MIN_DEPOSIT_CENTS = 100  # $1
MAX_DEPOSIT_CENTS = 100_000  # $1000

# Цена сохранения одного нового токена в боте (QR / привязка по номеру). .env: TOKEN_CREATION_PRICE_USD=0.2
def _token_creation_price_default_cents() -> int:
    raw = os.getenv("TOKEN_CREATION_PRICE_USD", "0.2").strip()
    try:
        usd = float((raw or "0.2").replace(",", "."))
        return max(0, int(round(usd * 100)))
    except ValueError:
        return 20


async def _token_creation_price_cents() -> int:
    """Цена создания токена: из БД, fallback — TOKEN_CREATION_PRICE_USD из .env."""
    return await get_token_price_cents(_token_creation_price_default_cents())


async def _msg_insufficient_balance_for_token_html() -> str:
    """Текст: не хватает средств на создание токена (до запуска QR/телефона и при списании)."""
    cents = await _token_creation_price_cents()
    return (
        f"❌ Недостаточно средств на балансе. Создание токена стоит "
        f"<b>{_format_usd(cents)} USD</b>. Пополните баланс: /balance или «💳 Пополнить»."
    )


async def _require_balance_before_token_flow(
    app: Application,
    credit_user_id: int,
    reply_chat_id: int,
    reply_thread_id: int | None,
    *,
    edit_query=None,
) -> bool:
    """
    Проверка баланса до запуска QR / link_phone.
    Если средств не хватает — сообщение уже отправлено (или отредактировано при edit_query).
    """
    cents = await _token_creation_price_cents()
    if cents <= 0:
        return True
    u = await get_user(credit_user_id)
    await ensure_user(credit_user_id, (u.get("username") or "") if u else "")
    bal = await get_balance_cents(credit_user_id)
    if bal >= cents:
        return True
    text = await _msg_insufficient_balance_for_token_html()
    if edit_query is not None:
        await _edit_or_resend_text(
            edit_query, text, app.bot, parse_mode="HTML"
        )
    else:
        kw = _send_kwargs(reply_chat_id, reply_thread_id)
        await app.bot.send_message(**kw, text=text, parse_mode="HTML")
    return False


async def _can_save_token_to_vault(
    app: Application,
    credit_user_id: int,
    reply_chat_id: int,
    reply_thread_id: int | None,
) -> bool:
    """
    Списывает с баланса владельца токена цену сохранения (после успешного получения сессии).
    Возвращает False, если не хватает средств (в чат уже отправлено сообщение).
    """
    cents = await _token_creation_price_cents()
    if cents <= 0:
        return True
    u = await get_user(credit_user_id)
    await ensure_user(credit_user_id, (u.get("username") or "") if u else "")
    if await try_debit_balance_cents(credit_user_id, cents):
        return True
    kw = _send_kwargs(reply_chat_id, reply_thread_id)
    await app.bot.send_message(
        **kw,
        text=await _msg_insufficient_balance_for_token_html(),
        parse_mode="HTML",
    )
    return False


def _deposit_amount_kb() -> InlineKeyboardMarkup:
    """Кнопки сумм для пополнения (callback pay:amt:<cents>)."""
    amounts = [
        (500, "$5"),
        (1000, "$10"),
        (2500, "$25"),
        (5000, "$50"),
        (10000, "$100"),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for cents, label in amounts:
        row.append(InlineKeyboardButton(label, callback_data=f"pay:amt:{cents}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀ Меню", callback_data="menu:refresh")])
    return InlineKeyboardMarkup(rows)


async def _create_and_send_crypto_invoice(
    *,
    uid: int,
    amount_cents: int,
    bot,
    reply_chat_id: int,
    reply_thread_id: int | None,
    edit_query,
) -> None:
    """Создаёт счёт Crypto Pay и показывает ссылку + кнопку проверки (редактирует callback или шлёт сообщение)."""
    amount_usd = amount_cents / 100.0
    payload = f"maxqr:{uid}:{amount_cents}"
    try:
        inv = await create_invoice_usd(
            amount_usd,
            payload,
            description=f"Пополнение баланса {_format_usd(amount_cents)}",
        )
    except Exception as e:
        logging.exception("createInvoice failed")
        err = f"Не удалось создать счёт: {e}"
        if edit_query:
            await _edit_or_resend_text(edit_query, err, bot)
        else:
            kw = _send_kwargs(reply_chat_id, reply_thread_id)
            await bot.send_message(**kw, text=err)
        return
    invoice_id = int(inv.get("invoice_id", 0))
    if not invoice_id:
        err = "Crypto Pay не вернул номер счёта."
        if edit_query:
            await _edit_or_resend_text(edit_query, err, bot)
        else:
            kw = _send_kwargs(reply_chat_id, reply_thread_id)
            await bot.send_message(**kw, text=err)
        return
    try:
        local_id = await insert_crypto_invoice(
            uid,
            invoice_id,
            amount_cents,
            payload,
            notify_chat_id=reply_chat_id,
            notify_thread_id=reply_thread_id,
        )
    except sqlite3.IntegrityError:
        err = "Такой счёт уже есть. Создайте новый через меню."
        if edit_query:
            await _edit_or_resend_text(edit_query, err, bot)
        else:
            kw = _send_kwargs(reply_chat_id, reply_thread_id)
            await bot.send_message(**kw, text=err)
        return
    url = (inv.get("bot_invoice_url") or inv.get("pay_url") or "").strip()
    if not url:
        err = "Crypto Pay не вернул ссылку на оплату."
        if edit_query:
            await _edit_or_resend_text(edit_query, err, bot)
        else:
            kw = _send_kwargs(reply_chat_id, reply_thread_id)
            await bot.send_message(**kw, text=err)
        return
    fee_html = _cryptopay_fee_line()
    text = (
        "💎 <b>Пополнение баланса</b>\n\n"
        f"Сумма к зачислению: <b>{_format_usd(amount_cents)} USD</b>\n\n"
        "Оплата в <b>USDT</b>, сеть <b>TRC-20</b> (через Crypto Bot).\n"
        f"{fee_html}\n\n"
        "Нажмите кнопку ниже → в Crypto Bot выберите USDT и завершите перевод.\n"
        "После оплаты нажмите «Проверить оплату»."
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💎 Пополнить USDT (TRC-20)", url=url)],
            [InlineKeyboardButton("💳 Проверить оплату", callback_data=f"pay:check:{local_id}")],
            [InlineKeyboardButton("◀ Меню", callback_data="menu:refresh")],
        ]
    )
    if edit_query:
        await _edit_or_resend_text(
            edit_query, text, bot, parse_mode="HTML", reply_markup=kb
        )
    else:
        kw = _send_kwargs(reply_chat_id, reply_thread_id)
        await bot.send_message(
            **kw, text=text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )


def _help_guide_text() -> str:
    """Текст гайда по использованию бота."""
    return (
        "❓ <b>Как пользоваться ботом</b>\n\n"
        "Бот помогает перенести сессию MAX в текстовый блок для входа в браузере.\n\n"
        "📋 <b>Команды</b>\n"
        "• /start — открыть меню\n"
        "• /qr — получить QR-код для сканирования в MAX\n"
        "• /link_phone — привязка по номеру телефона (SMS → блок)\n"
        "• /tokens — показать сохранённые токены и выдать себе 1, 3 или 5\n"
        "• /tokens N — сразу выдать N токенов (например, /tokens 3)\n"
        "• /balance — баланс в USD\n"
        "• /topup N — пополнить баланс на N USD через Crypto Bot (если настроено)\n\n"
        "📱 <b>QR-код</b>\n"
        "• Нажмите «📱 QR-код» или отправьте /qr\n"
        "• Откройте в приложении MAX: Профиль → Устройства → Войти по QR-коду\n"
        "• Отсканируйте QR из сообщения бота\n"
        "• Бот пришлёт .txt файл с блоком — скопируйте его в консоль браузера для переноса сессии\n\n"
        "📞 <b>По номеру телефона</b>\n"
        "• Нажмите «📞 По номеру» или отправьте /link_phone\n"
        "• Введите номер в формате +79001234567 или 9001234567\n"
        "• Введите код из SMS\n"
        "• Если включён 2FA — введите пароль по запросу\n"
        "• Бот пришлёт .txt файл с блоком для переноса сессии\n\n"
        "🎁 <b>Мои токены</b>\n"
        "• Нажмите «🎁 Мои токены» или отправьте /tokens\n"
        "• Показывает, сколько у вас сохранённых токенов\n"
        "• Можно выдать себе 1, 3 или 5 токенов в виде файлов (или /tokens 3 для выдачи трёх)\n\n"
        "💰 <b>Баланс (USD)</b>\n"
        "• «💰 Баланс» или /balance — внутренний счёт в боте\n"
        "• «💳 Пополнить» или /topup — через Crypto Bot (если задан CRYPTOPAY_API_TOKEN). "
        "<a href=\"https://help.send.tg/ru/articles/10279948-crypto-pay-api\">Crypto Pay API</a>\n"
        "• За сохранение каждого нового токена в боте (после QR или /link_phone) с баланса списывается $0.20 "
        "(по умолчанию; см. TOKEN_CREATION_PRICE_USD в .env)\n\n"
        "При ошибках используйте кнопку «Отмена» или «Повторить / другой номер». Команда /link_phone — снова ввод номера."
    )


def _menu_inline_kb(show_admin: bool = False) -> InlineKeyboardMarkup:
    """Главное меню."""
    rows = [
        [
            InlineKeyboardButton("📱 По номеру", callback_data="menu:phone"),
            InlineKeyboardButton("📂 Мои номера", callback_data="menu:tokens"),
        ],
        [
            InlineKeyboardButton("💰 Баланс", callback_data="menu:balance"),
            InlineKeyboardButton("❓ Помощь", callback_data="menu:help"),
        ],
        [
            InlineKeyboardButton("🆘 Поддержка", url=SUPPORT_LINK),
        ],
    ]

    if show_admin:
        rows.append([
            InlineKeyboardButton("⚙️ Админ-панель", callback_data="menu:admin")
        ])

    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    await ensure_user(user.id, user.username or user.first_name or "")
    # Группа: регистрация саппорт-группы или приветствие
    if chat.type in ("group", "supergroup"):
        group_id = chat.id
        g = await get_support_group(group_id)
        if g:
            if not await is_support_group_member_or_owner(group_id, user.id):
                await update.message.reply_text(
                    "В этой группе бот работает в режиме саппорта. "
                    "Использовать могут только владелец и добавленные саппорты."
                )
                return
            await update.message.reply_text(
                "👥 <b>Группа-саппорт</b>\n\n"
                "Все токены (QR, по номеру), сделанные здесь, зачисляются <b>владельцу</b> группы.\n\n"
                "Саппорты могут пользоваться ботом (QR, по номеру, токены) без доступа в личке.\n\n"
                "Команды владельца:\n"
                "/addsupport — в ответ на сообщение или /addsupport USER_ID — добавить саппорта\n"
                "/delsupport USER_ID — убрать\n"
                "/supportlist — список саппортов, выдача/забор доступа к токенам\n"
                "/groupstats — кто сколько токенов сделал\n\n"
                "Используйте кнопки ниже или /qr, /link_phone, /tokens.",
                parse_mode="HTML",
                reply_markup=_menu_inline_kb(await is_admin(g["owner_id"])),
            )
            return
        if not await is_allowed(user.id):
            await update.message.reply_text(
                "Сначала получите доступ в личке у администратора, затем отправьте /start здесь снова."
            )
            return
        created = await register_support_group(group_id, user.id, getattr(chat, "title", None))
        if created:
            await update.message.reply_text(
                "✅ <b>Группа зарегистрирована</b>\n\n"
                "Все токены, сделанные в этой группе (QR, по номеру), будут зачисляться вам.\n\n"
                "Добавьте саппортов (кто может делать токены здесь):\n"
                "• /addsupport — в ответ на сообщение пользователя\n"
                "• /addsupport USER_ID — по числовому ID\n"
                "/delsupport USER_ID — убрать\n"
                "/supportlist — список саппортов и выдача/забор доступа к токенам\n"
                "/groupstats — статистика по группе\n\n"
                "💡 Добавленные саппорты смогут использовать бота в этой группе (QR, по номеру, токены) даже без доступа в личке.",
                parse_mode="HTML",
                reply_markup=_menu_inline_kb(await is_admin(user.id)),
            )
        else:
            await update.message.reply_text("Эта группа уже зарегистрирована другим владельцем.")
        return
    # Личка
    if not await is_allowed(user.id):
        await update.message.reply_text(
            "⛔ " + ACCESS_DENIED,
            reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True),
        )
        return
    chat_id = user.id
    show_admin = await is_admin(chat_id)
    welcome_html = await _welcome_caption_html(user.id)
    photo_path = _menu_photo_path()
    if photo_path:
        try:
            with open(photo_path, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=welcome_html,
                    parse_mode="HTML",
                    reply_markup=_menu_inline_kb(show_admin),
                )
        except Exception as e:
            logging.warning("Не удалось отправить фото меню: %s", e)
            await update.message.reply_text(
                welcome_html,
                parse_mode="HTML",
                reply_markup=_menu_inline_kb(show_admin),
            )
    else:
        await update.message.reply_text(
            welcome_html,
            parse_mode="HTML",
            reply_markup=_menu_inline_kb(show_admin),
        )



async def cmd_addsupport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Добавить саппорта в группу: /addsupport или /addsupport USER_ID или в ответ на сообщение."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in ("group", "supergroup") or not user:
        return
    group_id = chat.id
    g = await get_support_group(group_id)
    if not g or g["owner_id"] != user.id:
        await update.message.reply_text("Команда только для владельца группы. Добавлять саппортов может только тот, кто зарегистрировал группу через /start.")
        return
    target_id: int | None = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id
    args = (context.args or [])
    if target_id is None and args:
        try:
            target_id = int(args[0].strip())
        except ValueError:
            pass
    if target_id is None:
        await update.message.reply_text(
            "Укажите пользователя: отправьте /addsupport в ответ на его сообщение или /addsupport USER_ID (числовой ID)."
        )
        return
    if target_id == user.id:
        await update.message.reply_text("Владелец уже может пользоваться ботом, добавлять не нужно.")
        return
    await ensure_user(target_id, None)
    added = await add_support_member(group_id, target_id)
    await update.message.reply_text("✅ Добавлен в саппорты." if added else "Этот пользователь уже в списке саппортов.")


async def cmd_delsupport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Убрать саппорта: /delsupport USER_ID."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in ("group", "supergroup") or not user:
        return
    group_id = chat.id
    g = await get_support_group(group_id)
    if not g or g["owner_id"] != user.id:
        await update.message.reply_text("Команда только для владельца группы.")
        return
    args = (context.args or [])
    if not args:
        await update.message.reply_text("Использование: /delsupport USER_ID")
        return
    try:
        target_id = int(args[0].strip())
    except ValueError:
        await update.message.reply_text("Укажите числовой ID пользователя: /delsupport 123456789")
        return
    removed = await remove_support_member(group_id, target_id)
    await update.message.reply_text("✅ Удалён из саппортов." if removed else "Пользователь не был в списке саппортов.")


async def cmd_supportlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список саппортов группы + статистика кто сколько токенов сделал. Владелец может забирать доступ кнопками."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        return
    group_id = chat.id
    g = await get_support_group(group_id)
    if not g:
        await update.message.reply_text("Эта группа не зарегистрирована как саппорт-группа. Отправьте /start.")
        return
    owner_id = g["owner_id"]
    members = await list_support_members(group_id)
    stats = await get_support_group_stats(group_id)
    stats_by_uid = {s["user_id"]: s["tokens_count"] for s in stats}

    owner_row = await get_user(owner_id)
    owner_name = f"@{owner_row['username']}" if owner_row and owner_row.get("username") else str(owner_id)
    lines = [f"👑 Владелец (получает все токены): {owner_name} (ID: {owner_id})"]
    if owner_id in stats_by_uid:
        lines.append(f"   └ сделано токенов в этой группе: {stats_by_uid[owner_id]}")
    lines.append("")
    for m in members:
        uid = m["user_id"]
        token_ok = m.get("token_access", False)
        row = await get_user(uid)
        name = f"@{row['username']}" if row and row.get("username") else str(uid)
        cnt = stats_by_uid.get(uid, 0)
        tok = "✅ доступ к токенам" if token_ok else "❌ без доступа к токенам"
        lines.append(f"• {name} (ID: {uid}) — токенов сделано: {cnt}, {tok}")
    if len(members) == 0:
        lines.append("Саппортов пока нет. /addsupport — добавить.")
    text = "👥 Саппорты группы:\n\n" + "\n".join(lines)

    rows = []
    if user and user.id == owner_id:
        rows.append([InlineKeyboardButton("📊 Статистика по группе", callback_data="group:stats")])
        for m in members:
            uid = m["user_id"]
            token_ok = m.get("token_access", False)
            row = await get_user(uid)
            name = (row and row.get("username")) and f"@{row['username']}" or str(uid)
            if len(name) > 25:
                name = str(uid)
            rows.append([
                InlineKeyboardButton(f"🚫 Забрать доступ — {name}", callback_data=f"group_revoke:{uid}"),
                InlineKeyboardButton(
                    "✅ Токены" if token_ok else "❌ Токены",
                    callback_data=f"group_tokens:{uid}:{'0' if token_ok else '1'}",
                ),
            ])
    kb = InlineKeyboardMarkup(rows) if rows else None
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_groupstats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Статистика по группе: кто сколько токенов сделал (видят владелец и саппорты)."""
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return
    group_id = chat.id
    g = await get_support_group(group_id)
    if not g:
        await update.message.reply_text("Эта группа не зарегистрирована. Отправьте /start.")
        return
    user = update.effective_user
    if not user or not await is_support_group_member_or_owner(group_id, user.id):
        await update.message.reply_text("Только владелец и саппорты группы могут смотреть статистику.")
        return
    stats = await get_support_group_stats(group_id)
    if not stats:
        await update.message.reply_text("📊 В этой группе пока никто не сделал токенов.")
        return
    lines = ["📊 <b>Токены по группе</b>\n"]
    for s in stats:
        uid = s["user_id"]
        cnt = s["tokens_count"]
        username = s.get("username")
        name = f"@{username}" if username else str(uid)
        lines.append(f"• {name}: {cnt}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _check_access(update: Update) -> bool:
    """Возвращает False и отправляет сообщение, если доступ запрещён (только для лички)."""
    user = update.effective_user
    if not user:
        return False
    await ensure_user(user.id, user.username or user.first_name or "")
    if not await is_allowed(user.id):
        await update.message.reply_text(ACCESS_DENIED)
        return False
    return True


async def _get_reply_and_credit_or_deny(update: Update) -> tuple[int, int] | None:
    """Возвращает (reply_chat_id, credit_user_id) или None и отправляет сообщение об отказе."""
    rc = await _get_reply_and_credit(update)
    if rc is not None:
        return rc
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        g = await get_support_group(chat.id)
        if g:
            await (update.message or update.callback_query.message).reply_text(
                "Только владелец группы и добавленные саппорты могут использовать бота здесь."
            )
        else:
            await (update.message or update.callback_query.message).reply_text(
                "Эта группа не зарегистрирована. Получите доступ в личке, затем отправьте /start в этой группе."
            )
    else:
        msg = update.message or (update.callback_query and update.callback_query.message)
        if msg:
            await msg.reply_text("⛔ " + ACCESS_DENIED)
    return None


async def _can_access_tokens_in_group(chat, user_id: int) -> bool:
    """В группе: только владелец или саппорт с доступом к токенам может забирать токены. В личке — не проверяем здесь."""
    if not chat or chat.type not in ("group", "supergroup"):
        return True
    g = await get_support_group(chat.id)
    if not g:
        return False
    if g["owner_id"] == user_id:
        return True
    return await support_member_has_token_access(chat.id, user_id)


async def cmd_qr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rc = await _get_reply_and_credit_or_deny(update)
    if rc is None:
        return
    reply_chat_id, credit_user_id = rc
    reply_thread_id = _get_message_thread_id(update)
    if _qr_active_count.get(reply_chat_id, 0) >= 5:
        await update.message.reply_text(
            "У вас уже запущено 5 операций QR одновременно. "
            "Дождитесь завершения текущих перед запуском новых."
        )
        return
    if not await _require_balance_before_token_flow(
        context.application, credit_user_id, reply_chat_id, reply_thread_id
    ):
        return
    await update.message.reply_text(
        "Открываю сессию, готовлю QR… Подождите до минуты."
    )
    _start_qr_flow(
        reply_chat_id, credit_user_id, context.application,
        reply_thread_id, update.effective_user.id if update.effective_user else None,
    )


def _normalize_phone(text: str) -> str | None:
    """Принимает: +79001234567, 79001234567, 89001234567, 9001234567 и варианты с пробелами."""
    digits = re.sub(r"\D", "", text.strip())
    if len(digits) == 10 and digits.startswith("9"):
        return f"+7{digits}"
    if len(digits) == 11:
        if digits[0] == "7" and digits[1] == "9":
            return f"+{digits}"
        if digits[0] == "8" and digits[1] == "9":
            return f"+7{digits[1:]}"
        if digits[0] == "9" and digits[1] == "9":
            return f"+7{digits[1:]}"
    return None


async def cmd_check_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверка IP через браузер с той же конфигурацией прокси, что и /qr."""
    if not await _check_access(update):
        return
    user = update.effective_user
    if not user or not await is_admin(user.id):
        await update.message.reply_text("Недостаточно прав.")
        return
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("Проверяю IP…")
    try:
        ip, used_proxy, proxy_desc = await check_proxy_ip()
        if proxy_desc == PROXY_EXHAUSTED_MSG:
            text = proxy_desc
        elif used_proxy and proxy_desc:
            text = f"IP: {ip}\n\nСравните с IP в приложении: Настройки → Устройства."
        else:
            text = f"IP: {ip}\n\nСравните с IP в приложении: Настройки → Устройства."
    except Exception as e:
        text = f"Ошибка: {e}"
    await msg.edit_text(text)


async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Управление личными токенами пользователя (в группе-саппорте — токенами владельца).

    /tokens          — показать, сколько токенов доступно
    /tokens N        — выдать N токенов (если есть в запасе)
    """
    rc = await _get_reply_and_credit_or_deny(update)
    if rc is None:
        return
    reply_chat_id, credit_user_id = rc
    chat = update.effective_chat
    user = update.effective_user
    if chat and user and not await _can_access_tokens_in_group(chat, user.id):
        await update.message.reply_text(
            "Доступ к токенам в этой группе только у владельца. Владелец может выдать вам доступ в /supportlist."
        )
        return
    reply_thread_id = _get_message_thread_id(update)

    args = context.args or []
    if not args:
        total_unused = await get_user_tokens_count(credit_user_id, only_unused=True)
        total_all = await get_user_tokens_count(credit_user_id, only_unused=False)
        zip_on = credit_user_id in _zip_mode_users
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Выдать 1", callback_data="tokens:get:1"),
                    InlineKeyboardButton("Выдать 3", callback_data="tokens:get:3"),
                ],
                [
                    InlineKeyboardButton("Выдать 5", callback_data="tokens:get:5"),
                    InlineKeyboardButton(
                        "ZIP: вкл" if zip_on else "ZIP: выкл",
                        callback_data="tokens:zip_toggle",
                    ),
                ],
                [
                    InlineKeyboardButton("Ввести количество", callback_data="tokens:input"),
                ],
            ]
        )
        await update.message.reply_text(
            "📦 <b>Ваши токены</b>\n\n"
            f"• Доступно к выдаче: <b>{total_unused}</b>\n"
            f"• Всего сохранено: <b>{total_all}</b>\n\n"
            "Нажмите кнопку ниже, чтобы выдать нужное количество токенов.\n"
            "Каждый токен придёт в виде .txt-файла.",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    try:
        count = int(args[0])
    except ValueError:
        await update.message.reply_text("Укажите количество: /tokens 1 или /tokens 3")
        return

    if count <= 0:
        await update.message.reply_text("Количество должно быть положительным числом.")
        return

    tokens = await pop_user_tokens(credit_user_id, count)
    if not tokens:
        await update.message.reply_text("У вас нет доступных токенов для выдачи.")
        return

    zip_on = credit_user_id in _zip_mode_users or len(tokens) > 15
    await _send_tokens_as_txt_file(
        reply_chat_id,
        context.application,
        [],
        filename_prefix="max_tokens",
        caption=(
            f"✅ Выдано токенов: {len(tokens)}.\n"
            "Откройте файл, скопируйте нужный блок и вставьте в нужном месте для переноса сессии."
        ),
        message_thread_id=reply_thread_id,
        blocks_with_prefixes=tokens,
        zip_mode=zip_on,
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать баланс в USD (личный счёт того, кто вызвал команду)."""
    rc = await _get_reply_and_credit_or_deny(update)
    if rc is None:
        return
    user = update.effective_user
    uid = user.id if user else rc[1]
    cents = await get_balance_cents(uid)
    await update.message.reply_text(
        f"💰 <b>Ваш баланс</b>\n\n{_format_usd(cents)} USD",
        parse_mode="HTML",
    )


async def cmd_topup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пополнение баланса через Crypto Pay: /topup или /topup 10."""
    rc = await _get_reply_and_credit_or_deny(update)
    if rc is None:
        return
    reply_chat_id, _credit_user_id = rc
    user = update.effective_user
    uid = user.id if user else _credit_user_id
    reply_thread_id = _get_message_thread_id(update)
    if not is_cryptopay_configured():
        await update.message.reply_text(
            "Пополнение через Crypto Bot не настроено. "
            "Администратору нужно задать CRYPTOPAY_API_TOKEN в .env "
            "(см. Crypto Pay в @CryptoBot)."
        )
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "💳 Выберите сумму пополнения (USD) или укажите вручную: /topup 10",
            reply_markup=_deposit_amount_kb(),
        )
        return
    try:
        amount = float(str(args[0]).replace(",", "."))
        cents = int(round(amount * 100))
    except ValueError:
        await update.message.reply_text("Укажите сумму: /topup 10 или /topup 10.5")
        return
    if cents < MIN_DEPOSIT_CENTS or cents > MAX_DEPOSIT_CENTS:
        await update.message.reply_text(
            f"Сумма от {_format_usd(MIN_DEPOSIT_CENTS)} до {_format_usd(MAX_DEPOSIT_CENTS)} USD."
        )
        return
    await _create_and_send_crypto_invoice(
        uid=uid,
        amount_cents=cents,
        bot=context.bot,
        reply_chat_id=reply_chat_id,
        reply_thread_id=reply_thread_id,
        edit_query=None,
    )


async def cmd_link_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rc = await _get_reply_and_credit_or_deny(update)
    if rc is None:
        return
    reply_chat_id, credit_user_id = rc
    if get_login_token_by_phone_async is None:
        await update.message.reply_text(
            f"Привязка по номеру недоступна. Установите зависимости: pip install msgpack\n({_register_account_error})"
        )
        return
    if not await _require_balance_before_token_flow(
        context.application, credit_user_id, reply_chat_id, _get_message_thread_id(update)
    ):
        return
    context.user_data["phone_flow"] = {
        "step": "phone",
        "reply_chat_id": reply_chat_id,
        "credit_user_id": credit_user_id,
        "reply_thread_id": _get_message_thread_id(update),
        "actor_user_id": update.effective_user.id if update.effective_user else None,
    }
    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена", callback_data="phone_cancel")],
    ])
    await update.message.reply_text(
        "Привязка по номеру телефона. Отправьте номер: +79001234567, 79001234567, 9001234567 или 8 900 123 45 67",
        reply_markup=cancel_kb,
    )


async def _send_and_wait_sms_code(
    chat_id: int,
    app: Application,
    message_thread_id: int | None = None,
) -> str:
    """Отправляет «Введите код из SMS» с кнопками отмены и ждёт ответ в чат."""
    kw = _send_kwargs(chat_id, message_thread_id)
    thread_key = message_thread_id or 0  # для callback_data: 0 = без топика
    sms_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Кода нет — пропустить", callback_data=f"skip_sms:{chat_id}:{thread_key}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"skip_sms:{chat_id}:{thread_key}"),
        ],
    ])
    await app.bot.send_message(**kw, text="Введите код из SMS", reply_markup=sms_kb)
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    key = (chat_id, message_thread_id)
    _password_waiters[key] = fut
    try:
        return await asyncio.wait_for(fut, timeout=120.0)
    finally:
        _password_waiters.pop(key, None)


async def _send_and_wait_password(
    chat_id: int,
    app: Application,
    hint: str,
    message_thread_id: int | None = None,
) -> str:
    """Отправляет запрос пароля (2FA) с кнопкой отмены и ждёт ответ в чат."""
    kw = _send_kwargs(chat_id, message_thread_id)
    text = "Включён 2FA. Введите пароль от аккаунта MAX."
    if hint:
        text += f"\nПодсказка: {hint}"
    thread_key = message_thread_id or 0
    pwd_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена", callback_data=f"password_cancel:{chat_id}:{thread_key}")],
    ])
    await app.bot.send_message(**kw, text=text, reply_markup=pwd_kb)
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    key = (chat_id, message_thread_id)
    _password_waiters[key] = fut
    try:
        return await asyncio.wait_for(fut, timeout=120.0)
    finally:
        _password_waiters.pop(key, None)


async def _pymax_auth_or_register(
    phone: str,
    get_sms_code: Callable[[], Awaitable[str]],
    get_password: Callable[[str], Awaitable[str]],
    first_name: str,
    last_name: str,
    proxy_for_ws: dict | None = None,
) -> str:
    """Тестовый auth/register через pymax. Возвращает готовый auth token."""
    if not _PYMAX_AVAILABLE or MaxClient is None or UserAgentPayload is None:
        raise RuntimeError("pymax не установлен. Установите maxapi-python.")

    ua = UserAgentPayload(
        device_type="WEB",
        app_version="25.21.0",
        os_version="Windows 10",
        locale="ru",
        device_locale="ru",
        timezone="Europe/Moscow",
        build_number=40490,
        client_session_id=random.randint(1, 100),
    )
    proxy_url: str | None = None
    if proxy_for_ws:
        server = (proxy_for_ws.get("server") or "").strip()
        if server:
            username = (proxy_for_ws.get("username") or "").strip()
            password = (proxy_for_ws.get("password") or "").strip()
            if username and password and "@" not in server and "://" in server:
                scheme, rest = server.split("://", 1)
                proxy_url = f"{scheme}://{username}:{password}@{rest}"
            else:
                proxy_url = server

    client = MaxClient(
        phone=phone,
        headers=ua,
        reconnect=False,
        work_dir=".pymax_cache_link",
        proxy=proxy_url,
    )

    # Runtime-патч для pymax: у websocket-ветки бывают несоответствия expected_cmd/opcode
    # в pending-таблице, из-за чего ответы на AUTH_REQUEST отбрасываются и уходят в timeout.
    def _patched_handle_pending(self, seq: int | None, data: dict) -> bool:
        if seq is None:
            return False
        pending = self._pending.get(seq)
        if not pending:
            return False
        fut = pending[0] if isinstance(pending, tuple) else pending
        if fut is None:
            return False
        if not fut.done():
            fut.set_result(data)
        return True

    client._handle_pending = types.MethodType(_patched_handle_pending, client)  # type: ignore[attr-defined]
    try:
        await client.connect(ua)
        temp_token = await client.request_code(phone)
        sms_code = (await get_sms_code()).strip()
        resp = await client._send_code(sms_code, temp_token)  # type: ignore[attr-defined]

        token_attrs = (resp.get("tokenAttrs") or {})
        login_token = ((token_attrs.get("LOGIN") or {}).get("token"))
        if login_token:
            return str(login_token)

        password_challenge = resp.get("passwordChallenge")
        if password_challenge and not login_token:
            track_id = password_challenge.get("trackId")
            hint = password_challenge.get("hint", "")
            if not track_id:
                raise RuntimeError("passwordChallenge без trackId")
            password = (await get_password(hint)).strip()
            if not password:
                raise RuntimeError("Пароль 2FA не введён")
            checked = await client._check_password(password, track_id)  # type: ignore[attr-defined]
            token = client._get_token_from_attrs(checked or {})  # type: ignore[attr-defined]
            if token:
                return str(token)
            raise RuntimeError("pymax: токен после 2FA не получен")

        reg_token = ((token_attrs.get("REGISTER") or {}).get("token"))
        if reg_token:
            reg_resp = await client._submit_reg_info(first_name, last_name, str(reg_token))  # type: ignore[attr-defined]
            auth_token = (reg_resp.get("token") if isinstance(reg_resp, dict) else None)
            if auth_token:
                return str(auth_token)
            raise RuntimeError("pymax: REGISTER завершился без auth token")

        raise RuntimeError(f"pymax: непредвиденный ответ auth: {resp}")
    finally:
        try:
            # В pymax close() только ставит stop_event; для чистого shutdown
            # дополнительно дожимаем _cleanup_client(), чтобы не оставлять pending task.
            await client.close()  # type: ignore[attr-defined]
            cleanup = getattr(client, "_cleanup_client", None)
            if cleanup is not None:
                try:
                    await asyncio.wait_for(cleanup(), timeout=2.5)
                except Exception:
                    pass
        except Exception:
            pass


async def _legacy_auth_or_register(
    phone: str,
    get_sms_code: Callable[[], Awaitable[str]],
    get_password_async: Callable[[str], Awaitable[str]],
    proxy: dict | None,
    client_profile: dict | None,
) -> tuple[str, str]:
    """Проверенный auth/register через register_account. Возвращает (auth_token, mode)."""
    kind, token_value = await get_login_token_by_phone_async(
        phone,
        get_sms_code=get_sms_code,
        proxy=proxy,
        get_password_async=get_password_async,
        client_profile=client_profile,
    )
    if kind == "register":
        first_name, last_name = get_random_russian_name()
        auth_token = await complete_registration_async(
            token_value, first_name, last_name, proxy=proxy, client_profile=client_profile
        )
        return auth_token, "register"
    return token_value, "login"


async def run_full_link_phone(
    reply_chat_id: int,
    credit_user_id: int,
    app: Application,
    phone: str,
    reply_thread_id: int | None = None,
    actor_user_id: int | None = None,
) -> None:
    """Полный флоу: вход по номеру (SMS) → при необходимости регистрация → веб-токен → блок в чат.
    Сообщения шлёт в reply_chat_id (в топик reply_thread_id при наличии), токены зачисляет credit_user_id.
    actor_user_id — кто запустил (для статистики по группе)."""
    kw = _send_kwargs(reply_chat_id, reply_thread_id)
    if await is_phone_used(phone):
        await app.bot.send_message(
            **kw,
            text="По этому номеру токен уже был получен. Используйте другой номер или получите токен через QR.",
            reply_markup=LINK_PHONE_RETRY_KB,
        )
        return
    use_proxy_for_api = os.getenv("USE_PROXY_FOR_API", "true").strip().lower() not in ("0", "false", "no")
    tried_proxies: set[str] = set()
    API_RETRY_ATTEMPTS = 3
    API_RETRY_DELAY = 2.5
    use_pymax_auth = os.getenv("LINK_PHONE_USE_PYMAX", "false").strip().lower() in ("1", "true", "yes")
    pymax_no_playwright = os.getenv("LINK_PHONE_PYMAX_NO_PLAYWRIGHT", "false").strip().lower() in ("1", "true", "yes")
    # Фиксируем профиль клиента на весь флоу /link_phone:
    # один device + deviceId + locale/deviceLocale для SMS -> register/login -> QR auth.
    client_profile = make_client_profile() if make_client_profile else None

    while True:
        proxy, cleanup_proxy, upstream_key = await get_proxy_for_request_async(tried_proxies)
        if not proxy:
            await app.bot.send_message(
                **kw,
                text=PROXY_EXHAUSTED_MSG + LINK_PHONE_RETRY_SUFFIX,
                reply_markup=LINK_PHONE_RETRY_KB,
            )
            return
        api_proxy = proxy if use_proxy_for_api else None
        last_api_error: Exception | None = None
        for attempt in range(1, API_RETRY_ATTEMPTS + 1):
            try:
                if use_pymax_auth:
                    if not _PYMAX_AVAILABLE:
                        raise RuntimeError("LINK_PHONE_USE_PYMAX=true, но библиотека pymax не установлена")
                    try:
                        first_name, last_name = get_random_russian_name()
                        auth_token = await _pymax_auth_or_register(
                            phone=phone,
                            get_sms_code=lambda: _send_and_wait_sms_code(reply_chat_id, app, reply_thread_id),
                            get_password=lambda hint: _send_and_wait_password(reply_chat_id, app, hint, reply_thread_id),
                            first_name=first_name,
                            last_name=last_name,
                            proxy_for_ws=proxy,
                        )
                        await app.bot.send_message(**kw, text="Вход/регистрация через pymax выполнены. Готовлю сессию…")
                    except Exception as pymax_exc:
                        msg = str(pymax_exc).lower()
                        if "auth.request.forbidden" in msg or "auth forbidden" in msg or "auth_request" in msg:
                            auth_token, mode = await _legacy_auth_or_register(
                                phone=phone,
                                get_sms_code=lambda: _send_and_wait_sms_code(reply_chat_id, app, reply_thread_id),
                                get_password_async=lambda hint: _send_and_wait_password(
                                    reply_chat_id, app, hint, reply_thread_id
                                ),
                                proxy=api_proxy,
                                client_profile=client_profile,
                            )
                        else:
                            raise
                else:
                    auth_token, mode = await _legacy_auth_or_register(
                        phone=phone,
                        get_sms_code=lambda: _send_and_wait_sms_code(reply_chat_id, app, reply_thread_id),
                        get_password_async=lambda hint: _send_and_wait_password(reply_chat_id, app, hint, reply_thread_id),
                        proxy=api_proxy,
                        client_profile=client_profile,
                    )
                    await app.bot.send_message(
                        **kw,
                        text=("Вход выполнен. Готовлю сессию…" if mode == "login" else "Регистрация выполнена. Готовлю сессию…"),
                    )

                if use_pymax_auth and pymax_no_playwright:
                    await run_link_phone_web_token(
                        reply_chat_id=reply_chat_id,
                        credit_user_id=credit_user_id,
                        app=app,
                        login_token=auth_token,
                        reply_thread_id=reply_thread_id,
                        actor_user_id=actor_user_id,
                        phone=phone,
                    )
                    if cleanup_proxy:
                        cleanup_proxy()
                    return

                last_err: Exception | None = None
                for _ in range(3):
                    try:
                        await run_link_phone_playwright(
                            reply_chat_id, credit_user_id, app, auth_token,
                            proxy_for_chain=proxy,
                            api_proxy=api_proxy,
                            reply_thread_id=reply_thread_id,
                            actor_user_id=actor_user_id,
                            phone=phone,
                            password=None,
                            client_profile=client_profile,
                        )
                        if cleanup_proxy:
                            cleanup_proxy()
                        return
                    except Exception as e:
                        last_err = e
                        logging.exception("Ошибка получения блока в run_link_phone_playwright")
                        continue
                try:
                    details = f"\nПоследняя ошибка: {last_err}" if last_err else ""
                    await app.bot.send_message(
                        **kw,
                        text="Не удалось получить блок после нескольких попыток. Попробуйте позже."
                        + details
                        + LINK_PHONE_RETRY_SUFFIX,
                        reply_markup=LINK_PHONE_RETRY_KB,
                    )
                except Exception:
                    pass
                if cleanup_proxy:
                    cleanup_proxy()
                return
            except (TimeoutError, asyncio.TimeoutError, OSError, ConnectionError, ConnectionResetError) as e:
                last_api_error = e
                if attempt < API_RETRY_ATTEMPTS:
                    await asyncio.sleep(API_RETRY_DELAY)
                    continue
                tried_proxies.add(upstream_key or "?")
                if cleanup_proxy:
                    cleanup_proxy()
                try:
                    await app.bot.send_message(
                        **kw,
                        text="Сервер ответил с задержкой или сеть недоступна. Попробуйте позже или проверьте интернет/прокси."
                        + LINK_PHONE_RETRY_SUFFIX,
                        reply_markup=LINK_PHONE_RETRY_KB,
                    )
                except Exception:
                    pass
                return
            except SkipAuthorizationError:
                if cleanup_proxy:
                    cleanup_proxy()
                return
            except Exception as e:
                if cleanup_proxy:
                    cleanup_proxy()
                try:
                    if isinstance(e, TimeoutError) or isinstance(e, asyncio.TimeoutError):
                        await app.bot.send_message(
                            **kw,
                            text="Сервер ответил с задержкой (таймаут). Попробуйте ещё раз или проверьте интернет/прокси.\n"
                            f"Подробность: {e}"
                            + LINK_PHONE_RETRY_SUFFIX,
                            reply_markup=LINK_PHONE_RETRY_KB,
                        )
                    else:
                        await app.bot.send_message(
                            **kw,
                            text=f"Ошибка: {e}"
                            + LINK_PHONE_RETRY_SUFFIX,
                            reply_markup=LINK_PHONE_RETRY_KB,
                        )
                except Exception:
                    pass
                return


async def run_link_phone_playwright(
    reply_chat_id: int,
    credit_user_id: int,
    app: Application,
    auth_token: str,
    proxy_for_chain: dict | None = None,
    api_proxy: dict | None = None,
    reply_thread_id: int | None = None,
    actor_user_id: int | None = None,
    phone: str | None = None,
    password: str | None = None,
    client_profile: dict | None = None,
) -> None:
    """После SMS: браузер получает блок сессии (QR). Сообщения в reply_chat_id, токены — credit_user_id.
    Если передан password (аккаунт уже с 2FA), пытается ввести пароль на веб-странице."""
    kw = _send_kwargs(reply_chat_id, reply_thread_id)

    gen = run_max_qr_flow_with_auth(
        auth_token,
        poll_interval=2.0,
        timeout=120.0,
        preferred_proxy=proxy_for_chain,
        password=password,
        client_profile=client_profile,
    )
    try:
        block = await gen.__anext__()
        if not block:
            raise RuntimeError("Сессия на странице не появилась (таймаут ожидания блока).")
        if not await _can_save_token_to_vault(
            app, credit_user_id, reply_chat_id, reply_thread_id
        ):
            if reply_chat_id < 0 and actor_user_id is not None and actor_user_id != credit_user_id:
                await app.bot.send_message(
                    **_send_kwargs(reply_chat_id, reply_thread_id),
                    text=(
                        "⚠️ У владельца недостаточно средств — токен не сохранён в «Мои токены». "
                        "Файл с сессией ниже."
                    ),
                )
            folder_name = f"max_{_phone_to_filename_prefix(phone) if phone else 'session'}"
            await _send_tokens_as_txt_file(
                reply_chat_id,
                app,
                [block],
                filename_prefix=folder_name,
                caption="Токен (не сохранён в боте — не хватило баланса)",
                message_thread_id=reply_thread_id,
            )
            if phone:
                await add_used_phone(phone)
            return
        await inc_tokens(credit_user_id)
        if reply_chat_id < 0 and actor_user_id is not None:
            await inc_support_group_token_stats(reply_chat_id, actor_user_id)
        await add_user_token(credit_user_id, block, _phone_to_filename_prefix(phone) if phone else "phone")
        if phone:
            await add_used_phone(phone)
        if reply_chat_id < 0 and actor_user_id is not None and actor_user_id != credit_user_id:
            await app.bot.send_message(
                **_send_kwargs(reply_chat_id, reply_thread_id),
                text="✅ Токен успешно добавлен владельцу группы. Он может запросить его через «Мои токены».",
            )
        else:
            folder_name = f"max_{_phone_to_filename_prefix(phone) if phone else 'session'}"
            await _send_tokens_as_txt_file(
                reply_chat_id,
                app,
                [block],
                filename_prefix=folder_name,
                caption="Токен",
                message_thread_id=reply_thread_id,
            )
    finally:
        await gen.aclose()


async def run_link_phone_web_token(
    reply_chat_id: int,
    credit_user_id: int,
    app: Application,
    login_token: str,
    reply_thread_id: int | None = None,
    actor_user_id: int | None = None,
    phone: str | None = None,
) -> None:
    kw = _send_kwargs(reply_chat_id, reply_thread_id)

    async def get_password(hint: str) -> str:
        await app.bot.send_message(
            **kw,
            text=f"Включён 2FA. Введите пароль (подсказка: {hint})",
        )
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        key = (reply_chat_id, reply_thread_id)
        _password_waiters[key] = fut
        try:
            return await asyncio.wait_for(fut, timeout=120.0)
        finally:
            _password_waiters.pop(key, None)

    try:
        device_id, token = await get_web_token_via_qr_async(
            login_token, get_password_async=get_password
        )
        block = make_session_block(device_id, token)
        if not await _can_save_token_to_vault(
            app, credit_user_id, reply_chat_id, reply_thread_id
        ):
            if reply_chat_id < 0 and actor_user_id is not None and actor_user_id != credit_user_id:
                await app.bot.send_message(
                    **kw,
                    text=(
                        "⚠️ У владельца недостаточно средств — токен не сохранён в «Мои токены». "
                        "Файл с сессией ниже."
                    ),
                )
            file_prefix = _phone_to_filename_prefix(phone) if phone else "phone"
            await _send_tokens_as_txt_file(
                reply_chat_id,
                app,
                [block],
                filename_prefix=file_prefix,
                caption="Токен (не сохранён в боте — не хватило баланса)",
                message_thread_id=reply_thread_id,
            )
            if phone:
                await add_used_phone(phone)
            return
        await inc_tokens(credit_user_id)
        if reply_chat_id < 0 and actor_user_id is not None:
            await inc_support_group_token_stats(reply_chat_id, actor_user_id)
        await add_user_token(credit_user_id, block, _phone_to_filename_prefix(phone) if phone else "phone")
        if phone:
            await add_used_phone(phone)
        if reply_chat_id < 0 and actor_user_id is not None and actor_user_id != credit_user_id:
            await app.bot.send_message(
                **kw,
                text="✅ Токен успешно добавлен владельцу группы. Он может запросить его через «Мои токены».",
            )
        else:
            file_prefix = _phone_to_filename_prefix(phone) if phone else "phone"
            await _send_tokens_as_txt_file(
                reply_chat_id,
                app,
                [block],
                filename_prefix=file_prefix,
                caption="Токен",
                message_thread_id=reply_thread_id,
            )
    except asyncio.TimeoutError:
        await app.bot.send_message(
            **kw,
            text="Время ожидания вышло. Отправьте /link_phone и попробуйте снова."
            + LINK_PHONE_RETRY_SUFFIX,
            reply_markup=LINK_PHONE_RETRY_KB,
        )
    except Exception as e:
        if isinstance(e, TimeoutError):
            await app.bot.send_message(
                **kw,
                text="Сервер ответил с задержкой (таймаут). Попробуйте ещё раз или проверьте интернет/прокси."
                + LINK_PHONE_RETRY_SUFFIX,
                reply_markup=LINK_PHONE_RETRY_KB,
            )
        else:
            await app.bot.send_message(
                **kw,
                text=f"Ошибка: {e}"
                + LINK_PHONE_RETRY_SUFFIX,
                reply_markup=LINK_PHONE_RETRY_KB,
            )


# --- Админка ---

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ-панель: статистика, пользователи, прокси."""
    user = update.effective_user
    if not user or not await is_admin(user.id):
        await update.message.reply_text("Недостаточно прав.")
        return
    args = (context.args or [])
    if not args:
        current_price_cents = await _token_creation_price_cents()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton("👥 Список пользователей", callback_data="admin:users")],
            [InlineKeyboardButton("🧩 Прокси", callback_data="admin:proxies")],
            [InlineKeyboardButton(f"💵 Цена токена: {_format_usd(current_price_cents)}", callback_data="admin:price")],
            [InlineKeyboardButton("📣 Рассылка", callback_data="admin:broadcast")],
        ])
        await update.message.reply_text(
            "Админ-панель:\n\n"
            "/admin stats — сводка\n"
            "/admin users — список\n"
            "/admin allow <user_id> — разрешить\n"
            "/admin deny <user_id> — запретить\n"
            "/admin balance <user_id> <USD> — задать баланс пользователя в USD\n"
            "/admin price [USD] — показать/установить цену создания токена\n"
            "/admin proxies — список прокси\n"
            "/admin proxy_add <ip:port:user:pass> — добавить прокси\n"
            "/admin proxy_del <index|строка> — удалить прокси",
            reply_markup=kb,
        )
        return
    sub = args[0].lower()
    if sub == "stats":
        s = await stats_summary()
        await update.message.reply_text(
            f"📊 Статистика:\n"
            f"Пользователей: {s['total']}\n"
            f"С доступом: {s['allowed']}\n"
            f"Успешных токенов: {s['tokens']}"
        )
    elif sub == "users":
        users = await list_users()
        lines = []
        for u in users[:50]:
            a = "✓" if u.get("allowed") or u.get("is_admin") else "✗"
            adm = " [админ]" if u.get("is_admin") else ""
            lines.append(f"{a} {u['user_id']} @{u.get('username') or '-'}{adm} — токенов: {u.get('tokens_count', 0)}")
        text = "\n".join(lines) if lines else "Нет пользователей."
        if len(users) > 50:
            text += f"\n\n... и ещё {len(users) - 50}"
        await update.message.reply_text(f"👥 Пользователи:\n\n{text}")
    elif sub == "allow" and len(args) >= 2:
        try:
            uid = int(args[1])
            ok = await set_allowed(uid, True)
            await update.message.reply_text(f"Пользователь {uid} разрешён." if ok else f"Пользователь {uid} не найден.")
        except ValueError:
            await update.message.reply_text("Укажите ID: /admin allow 123456")
    elif sub == "deny" and len(args) >= 2:
        try:
            uid = int(args[1])
            ok = await set_allowed(uid, False)
            await update.message.reply_text(f"Пользователь {uid} заблокирован." if ok else f"Пользователь {uid} не найден.")
        except ValueError:
            await update.message.reply_text("Укажите ID: /admin deny 123456")
    elif sub == "balance":
        if len(args) < 3:
            await update.message.reply_text(
                "Задать баланс пользователя в USD:\n"
                "/admin balance <user_id> <сумма>\n\n"
                "Пример: /admin balance 123456789 10.5",
            )
        else:
            try:
                uid = int(args[1])
            except ValueError:
                await update.message.reply_text("Неверный user_id. Пример: /admin balance 123456789 10")
                return
            try:
                usd = float(str(args[2]).replace(",", "."))
                cents = int(round(usd * 100))
            except ValueError:
                await update.message.reply_text("Сумма должна быть числом, например 10 или 10.5")
                return
            u = await get_user(uid)
            await ensure_user(uid, (u.get("username") or "") if u else "")
            await admin_set_balance_cents(uid, cents)
            await update.message.reply_text(
                f"Баланс пользователя <code>{uid}</code> установлен: <b>{_format_usd(cents)} USD</b>.",
                parse_mode="HTML",
            )
    elif sub == "price":
        if len(args) < 2:
            cents = await _token_creation_price_cents()
            await update.message.reply_text(
                "Цена создания токена:\n"
                f"<b>{_format_usd(cents)} USD</b>\n\n"
                "Изменить: /admin price 0.25",
                parse_mode="HTML",
            )
        else:
            try:
                usd = float(str(args[1]).replace(",", "."))
                cents = max(0, int(round(usd * 100)))
            except ValueError:
                await update.message.reply_text("Сумма должна быть числом, например 0.2 или 1")
                return
            await set_token_price_cents(cents)
            await update.message.reply_text(
                f"Новая цена создания токена: <b>{_format_usd(cents)} USD</b>.",
                parse_mode="HTML",
            )
    elif sub == "proxies":
        await _admin_show_proxies(update)
    elif sub == "proxy_refresh":
        sync_fn, ProxySellerErr = _get_proxy_seller_sync()
        if sync_fn is None:
            await update.message.reply_text(
                "Синхронизация с Proxy-Seller недоступна: не установлен proxy_seller_user_api.\n\n"
                "Установите: pip install proxy-seller-user-api\n"
                "Затем перезапустите бота или нажмите «Обновить прокси» снова."
            )
        else:
            try:
                await update.message.reply_text("Обновляю прокси из Proxy-Seller…")
                added, removed = await sync_fn(merge=False)
                await update.message.reply_text(
                    f"Синхронизация завершена.\n"
                    f"Добавлено: {added}\n"
                    f"Удалено (нет в API): {removed}"
                )
            except ProxySellerErr as e:
                await update.message.reply_text(f"Ошибка Proxy-Seller: {e}")
            except Exception as e:
                await update.message.reply_text(f"Не удалось обновить прокси: {e}")
    elif sub == "proxy_add" and len(args) >= 2:
        proxy_line = " ".join(args[1:]).strip()
        await _admin_add_proxy(update, proxy_line)
    elif sub == "proxy_del" and len(args) >= 2:
        target = " ".join(args[1:]).strip()
        await _admin_delete_proxy(update, target)
    else:
        await update.message.reply_text("Неизвестная команда. /admin — справка.")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Рассылка от админа в личку всем пользователям с доступом.

    Использование:
      /broadcast текст сообщения
      или ответом на сообщение: /broadcast
    """
    user = update.effective_user
    if not user or not await is_admin(user.id):
        await (update.message or update.effective_message).reply_text("Недостаточно прав.")
        return

    # Текст берём либо из аргументов, либо из сообщения, на которое ответили
    text = " ".join(context.args).strip()
    if not text and update.message and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""
    if not text:
        await update.message.reply_text(
            "Укажите текст рассылки: /broadcast текст\n"
            "Или отправьте /broadcast в ответ на сообщение, которое нужно разослать."
        )
        return

    users = await list_users()
    # Только тем, у кого есть доступ (allowed=1 или is_admin=1)
    recipient_ids = [u["user_id"] for u in users if u.get("allowed") or u.get("is_admin")]
    if not recipient_ids:
        await update.message.reply_text("Нет пользователей с доступом для рассылки.")
        return

    sent = 0
    failed = 0
    for uid in recipient_ids:
        try:
            await context.application.bot.send_message(chat_id=uid, text=text)
            sent += 1
            # Немного замедлимся, чтобы не словить лимиты
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
            continue

    await update.message.reply_text(
        f"Рассылка завершена.\nУспешно: {sent}\nНе удалось отправить: {failed}"
    )


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("admin:"):
        return
    user = update.effective_user
    if not user or not await is_admin(user.id):
        await query.answer("Недостаточно прав.")
        return
    await query.answer()
    sub = query.data.split(":", 1)[1]
    if sub == "menu":
        current_price_cents = await _token_creation_price_cents()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton("👥 Пользователи", callback_data="admin:users")],
            [InlineKeyboardButton("🧩 Прокси", callback_data="admin:proxies")],
            [InlineKeyboardButton(f"💵 Цена токена: {_format_usd(current_price_cents)}", callback_data="admin:price")],
            [InlineKeyboardButton("📣 Рассылка", callback_data="admin:broadcast")],
            [InlineKeyboardButton("◀ Назад", callback_data="menu:refresh")],
        ])
        await query.edit_message_text(
            "⚙️ <b>Админ-панель</b>\n\n"
            "/admin allow 123456 — разрешить\n"
            "/admin deny 123456 — запретить\n"
            "/admin price 0.25 — цена токена",
            parse_mode="HTML",
            reply_markup=kb,
        )
    elif sub == "broadcast":
        # Включаем режим ввода текста для рассылки: следующее сообщение админа пойдёт в broadcast
        context.user_data["broadcast_mode"] = "await_text"
        await query.edit_message_text(
            "📣 Режим рассылки.\n\n"
            "Отправьте одно сообщение с текстом, который нужно разослать всем пользователям с доступом.\n"
            "Чтобы отменить, отправьте «отмена».",
        )
    elif sub == "price":
        current_price_cents = await _token_creation_price_cents()
        context.user_data["admin_token_price_mode"] = "await_value"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀ Назад", callback_data="admin:menu")],
        ])
        await query.edit_message_text(
            "💵 <b>Изменение цены токена</b>\n\n"
            f"Текущая цена: <b>{_format_usd(current_price_cents)} USD</b>\n"
            "Отправьте новую цену в USD (например: <code>0.25</code>).\n"
            "Для отмены отправьте «отмена».",
            parse_mode="HTML",
            reply_markup=kb,
        )
    elif sub == "proxies":
        fake_update = Update(update.update_id, message=update.effective_message)
        await _admin_show_proxies(fake_update, bot=context.application.bot)
    elif sub == "proxy_refresh":
        sync_fn, ProxySellerErr = _get_proxy_seller_sync()
        if sync_fn is None:
            await query.edit_message_text(
                "Синхронизация с Proxy-Seller недоступна: не установлен proxy_seller_user_api.\n\n"
                "Установите: pip install proxy-seller-user-api и перезапустите бота.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="admin:proxies")]]),
            )
        else:
            await query.edit_message_text("Обновляю прокси из Proxy-Seller…")
            try:
                added, removed = await sync_fn(merge=False)
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("↻ Обновить снова", callback_data="admin:proxy_refresh")],
                    [InlineKeyboardButton("◀ К списку прокси", callback_data="admin:proxies")],
                ])
                await query.edit_message_text(
                    f"Синхронизация завершена.\nДобавлено: {added}\nУдалено (нет в API): {removed}",
                    reply_markup=kb,
                )
            except ProxySellerErr as e:
                await query.edit_message_text(
                    f"Ошибка Proxy-Seller: {e}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="admin:proxies")]]),
                )
            except Exception as e:
                await query.edit_message_text(
                    f"Не удалось обновить прокси: {e}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="admin:proxies")]]),
                )
    elif sub == "stats":
        s = await stats_summary()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀ Назад", callback_data="admin:menu"),
        ]])
        await query.edit_message_text(
            f"📊 <b>Статистика</b>\n\n"
            f"Пользователей: {s['total']}\n"
            f"С доступом: {s['allowed']}\n"
            f"Успешных токенов: {s['tokens']}",
            parse_mode="HTML",
            reply_markup=kb,
        )
    elif sub == "users" or sub.startswith("users:"):
        users = await list_users()
        total = len(users)
        try:
            page = int(sub.split(":")[-1]) if sub.startswith("users:") and ":" in sub else 0
        except ValueError:
            page = 0
        page = max(0, min(page, (total - 1) // ADMIN_USERS_PAGE_SIZE) if total else 0)
        total_pages = max(1, (total + ADMIN_USERS_PAGE_SIZE - 1) // ADMIN_USERS_PAGE_SIZE)
        start = page * ADMIN_USERS_PAGE_SIZE
        page_users = users[start : start + ADMIN_USERS_PAGE_SIZE]

        lines = []
        for u in page_users:
            a = "✓" if u.get("allowed") or u.get("is_admin") else "✗"
            adm = " [админ]" if u.get("is_admin") else ""
            lines.append(
                f"{a} {u['user_id']} @{u.get('username') or '-'}{adm} — {u.get('tokens_count', 0)} ткн."
            )
        text = "\n".join(lines) if lines else "Нет пользователей на этой странице."

        rows = []
        for u in page_users:
            uid = u["user_id"]
            if u.get("is_admin"):
                rows.append([InlineKeyboardButton(f"👑 {uid} (админ)", callback_data="admin:menu")])
                continue
            allowed_flag = bool(u.get("allowed"))
            if allowed_flag:
                rows.append([InlineKeyboardButton(f"🚫 Запретить {uid}", callback_data=f"admin:deny:{uid}:{page}")])
            else:
                rows.append([InlineKeyboardButton(f"✅ Разрешить {uid}", callback_data=f"admin:allow:{uid}:{page}")])

        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀ Пред", callback_data=f"admin:users:{page - 1}"))
        nav_buttons.append(InlineKeyboardButton(f"Стр. {page + 1}/{total_pages}", callback_data=f"admin:users:{page}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("След ▶", callback_data=f"admin:users:{page + 1}"))
        rows.append(nav_buttons)
        rows.append([InlineKeyboardButton("◀ Назад", callback_data="admin:menu")])

        kb = InlineKeyboardMarkup(rows)
        header = (
            f"👥 <b>Пользователи</b> (всего {total})\n\n"
            + text
            + (f"\n\nКнопками ниже можно разрешить или запретить доступ на этой странице." if users else "")
        )
        await _edit_or_resend_text(query, header, context.bot, parse_mode="HTML", reply_markup=kb)
    elif sub.startswith("allow:") or sub.startswith("deny:"):
        parts = sub.split(":")
        try:
            uid = int(parts[1])
            return_page = int(parts[2]) if len(parts) > 2 else 0
        except (ValueError, IndexError):
            await query.answer("Некорректный ID пользователя.", show_alert=True)
            return
        allow_flag = sub.startswith("allow:")
        ok = await set_allowed(uid, allow_flag)
        if not ok:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ К списку пользователей", callback_data="admin:users")]]
            )
            await query.edit_message_text("Пользователь не найден.", reply_markup=kb)
            return
        status = "разрешён" if allow_flag else "заблокирован"
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("◀ К списку пользователей", callback_data=f"admin:users:{return_page}")]]
        )
        await query.edit_message_text(f"Пользователь {uid} {status}.", reply_markup=kb)
    elif sub == "proxy":
        await query.edit_message_text("Проверяю IP…")
        try:
            ip, used_proxy, proxy_desc = await check_proxy_ip()
            if proxy_desc == PROXY_EXHAUSTED_MSG:
                text = PROXY_EXHAUSTED_MSG
            elif used_proxy and proxy_desc:
                text = (
                    f"IP: {ip}\n\n"
                    "Сравните с IP в приложении: Настройки → Устройства."
                )
            else:
                text = (
                    f"IP: {ip}\n\n"
                    "Сравните с IP в приложении: Настройки → Устройства."
                )
        except Exception as e:
            text = f"Ошибка: {e}"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀ Назад", callback_data="admin:menu"),
        ]])
        await query.edit_message_text(text, reply_markup=kb)


async def handle_qr_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка кнопки «Отмена» под сообщением с QR: отменяет ожидание сканирования."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("qr_cancel"):
        return
    await query.answer()
    flow_id: str | None = None
    if ":" in query.data:
        try:
            flow_id = query.data.split(":", 1)[1]
        except Exception:
            flow_id = None
    if not flow_id:
        # Старый формат кнопок без flow_id: пробуем отменить последнюю активную задачу.
        flow_id = next(reversed(_qr_tasks), None) if _qr_tasks else None
    task = _qr_tasks.pop(flow_id, None) if flow_id else None
    if task and not task.done():
        task.cancel()
    chat_id = query.message.chat_id if query.message else None
    if chat_id is None:
        return
    try:
        await query.edit_message_caption(
            caption="QR-авторизация отменена. Отправьте /qr или нажмите «📱 QR-код», чтобы начать заново.",
            reply_markup=None,
        )
    except Exception:
        try:
            thread_id = getattr(query.message, "message_thread_id", None) if query.message else None
            kw = _send_kwargs(chat_id, thread_id)
            await context.bot.send_message(
                **kw,
                text="QR-авторизация отменена. Отправьте /qr или нажмите «📱 QR-код», чтобы начать заново.",
            )
        except Exception:
            pass


async def handle_skip_sms_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка кнопки «Кода нет — пропустить» и «Отмена»: отменяет ожидание кода."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("skip_sms:"):
        return
    await query.answer()
    parts = query.data.split(":")
    try:
        chat_id = int(parts[1])
        thread_id = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return
    thread_id = None if thread_id == 0 else thread_id
    key = (chat_id, thread_id)
    fut = _password_waiters.pop(key, None)
    if fut and not fut.done():
        fut.set_exception(SkipAuthorizationError())
    try:
        await query.edit_message_text("Авторизация отменена. Отправьте /link_phone для другого номера.")
    except Exception:
        try:
            msg_thread_id = getattr(query.message, "message_thread_id", None) if query.message else None
            kw = _send_kwargs(chat_id, msg_thread_id)
            await context.bot.send_message(
                **kw,
                text="Авторизация отменена. Отправьте /link_phone для другого номера.",
            )
        except Exception:
            pass


async def handle_phone_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка кнопки «Отмена» на шаге «Отправьте номер»: выходит из флоу по номеру."""
    query = update.callback_query
    if not query or not query.data or query.data != "phone_cancel":
        return
    await query.answer()
    context.user_data.pop("phone_flow", None)
    try:
        await query.edit_message_text("Привязка по номеру отменена. Нажмите «📋 Меню» для выбора действия.")
    except Exception:
        try:
            thread_id = getattr(query.message, "message_thread_id", None) if query.message else None
            kw = _send_kwargs(query.message.chat_id, thread_id)
            await context.bot.send_message(
                **kw,
                text="Привязка по номеру отменена. Нажмите «📋 Меню» для выбора действия.",
            )
        except Exception:
            pass


async def handle_password_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка кнопки «Отмена» при запросе пароля 2FA: отменяет ожидание пароля."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("password_cancel:"):
        return
    await query.answer()
    parts = query.data.split(":")
    try:
        chat_id = int(parts[1])
        thread_id = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return
    thread_id = None if thread_id == 0 else thread_id
    key = (chat_id, thread_id)
    fut = _password_waiters.pop(key, None)
    if fut and not fut.done():
        fut.set_exception(SkipAuthorizationError())
    try:
        await query.edit_message_text("Ввод пароля отменён. Отправьте /link_phone для другого номера.")
    except Exception:
        try:
            msg_thread_id = getattr(query.message, "message_thread_id", None) if query.message else None
            kw = _send_kwargs(chat_id, msg_thread_id)
            await context.bot.send_message(
                **kw,
                text="Ввод пароля отменён. Отправьте /link_phone для другого номера.",
            )
        except Exception:
            pass


async def handle_group_support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки в группе: group:stats — статистика, group_revoke:user_id — забрать доступ (только владелец)."""
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    if not data.startswith("group:") and not data.startswith("group_revoke:") and not data.startswith("group_tokens:") and not data.startswith("group_stats_reset"):
        return
    chat = query.message.chat if query.message else None
    user = update.effective_user
    if not chat or chat.type not in ("group", "supergroup") or not user:
        return
    group_id = chat.id
    g = await get_support_group(group_id)
    if not g:
        await query.answer("Группа не зарегистрирована.", show_alert=True)
        return
    owner_id = g["owner_id"]
    if user.id != owner_id:
        await query.answer("Только владелец группы может управлять доступом.", show_alert=True)
        return
    await query.answer()

    if data == "group:stats":
        stats = await get_support_group_stats(group_id)
        if not stats:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ К списку саппортов", callback_data="group:supportlist")],
            ])
            await query.edit_message_text("📊 В этой группе пока никто не сделал токенов.", reply_markup=kb)
            return
        lines = ["📊 <b>Токены по группе</b>\n"]
        for s in stats:
            uid = s["user_id"]
            cnt = s["tokens_count"]
            username = s.get("username")
            name = f"@{username}" if username else str(uid)
            lines.append(f"• {name}: {cnt}")
        rows = [[InlineKeyboardButton("◀ К списку саппортов", callback_data="group:supportlist")]]
        rows.append([InlineKeyboardButton("🔄 Обнулить статистику", callback_data="group_stats_reset")])
        kb = InlineKeyboardMarkup(rows)
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
        return

    if data == "group_stats_reset":
        await query.edit_message_text(
            "Вы точно хотите обнулить статистику по группе?\n\n"
            "Все счётчики токенов будут сброшены.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Да", callback_data="group_stats_reset_confirm:1"),
                    InlineKeyboardButton("❌ Нет", callback_data="group_stats_reset_confirm:0"),
                ],
            ]),
        )
        return

    if data.startswith("group_stats_reset_confirm:"):
        try:
            confirm = data.split(":", 1)[1] == "1"
        except IndexError:
            return
        if confirm:
            await reset_support_group_stats(group_id)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ К списку саппортов", callback_data="group:supportlist")],
            ])
            await query.edit_message_text("✅ Статистика по группе обнулена.", reply_markup=kb)
        else:
            # Вернуться к экрану статистики
            stats = await get_support_group_stats(group_id)
            if not stats:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ К списку саппортов", callback_data="group:supportlist")],
                ])
                await query.edit_message_text("📊 В этой группе пока никто не сделал токенов.", reply_markup=kb)
            else:
                lines = ["📊 <b>Токены по группе</b>\n"]
                for s in stats:
                    uid = s["user_id"]
                    cnt = s["tokens_count"]
                    username = s.get("username")
                    name = f"@{username}" if username else str(uid)
                    lines.append(f"• {name}: {cnt}")
                rows = [[InlineKeyboardButton("◀ К списку саппортов", callback_data="group:supportlist")]]
                rows.append([InlineKeyboardButton("🔄 Обнулить статистику", callback_data="group_stats_reset")])
                await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "group:supportlist":
        members = await list_support_members(group_id)
        stats = await get_support_group_stats(group_id)
        stats_by_uid = {s["user_id"]: s["tokens_count"] for s in stats}
        lines = [f"👑 Владелец: {owner_id}"]
        if owner_id in stats_by_uid:
            lines.append(f"   └ токенов: {stats_by_uid[owner_id]}")
        lines.append("")
        for m in members:
            uid = m["user_id"]
            token_ok = m.get("token_access", False)
            row = await get_user(uid)
            name = f"@{row['username']}" if row and row.get("username") else str(uid)
            cnt = stats_by_uid.get(uid, 0)
            tok = "✅ токены" if token_ok else "❌ без токенов"
            lines.append(f"• {name} (ID: {uid}) — токенов: {cnt}, {tok}")
        if not members:
            lines.append("Саппортов нет. /addsupport — добавить.")
        text = "👥 Саппорты группы:\n\n" + "\n".join(lines)
        rows = [[InlineKeyboardButton("📊 Статистика по группе", callback_data="group:stats")]]
        for m in members:
            uid = m["user_id"]
            token_ok = m.get("token_access", False)
            row = await get_user(uid)
            name = (row and row.get("username")) and f"@{row['username']}" or str(uid)
            if len(name) > 25:
                name = str(uid)
            rows.append([
                InlineKeyboardButton(f"🚫 Забрать — {name}", callback_data=f"group_revoke:{uid}"),
                InlineKeyboardButton(
                    "✅ Токены" if token_ok else "❌ Токены",
                    callback_data=f"group_tokens:{uid}:{'0' if token_ok else '1'}",
                ),
            ])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("group_revoke:"):
        try:
            target_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        removed = await remove_support_member(group_id, target_id)
        if removed:
            row = await get_user(target_id)
            name = f"@{row['username']}" if row and row.get("username") else str(target_id)
            await query.edit_message_text(f"✅ У {name} (ID: {target_id}) забран доступ к боту в этой группе.\n\n/supportlist — обновить список.")
        else:
            await query.answer("Пользователь не был в саппортах.", show_alert=True)
        return

    if data.startswith("group_tokens:"):
        # group_tokens:user_id:0|1 — выдать/забрать доступ к токенам (только владелец)
        try:
            parts = data.split(":", 2)
            if len(parts) != 3:
                return
            target_id = int(parts[1])
            token_access = parts[2] == "1"
        except (ValueError, IndexError):
            return
        updated = await set_support_member_token_access(group_id, target_id, token_access)
        if updated:
            row = await get_user(target_id)
            name = f"@{row['username']}" if row and row.get("username") else str(target_id)
            action = "выдан" if token_access else "забран"
            await query.answer(f"Доступ к токенам {action} для {name}.", show_alert=False)
            # Обновить список саппортов (тот же блок, что group:supportlist)
            members = await list_support_members(group_id)
            stats = await get_support_group_stats(group_id)
            stats_by_uid = {s["user_id"]: s["tokens_count"] for s in stats}
            lines = [f"👑 Владелец: {owner_id}"]
            if owner_id in stats_by_uid:
                lines.append(f"   └ токенов: {stats_by_uid[owner_id]}")
            lines.append("")
            for m in members:
                uid = m["user_id"]
                token_ok = m.get("token_access", False)
                row = await get_user(uid)
                name = f"@{row['username']}" if row and row.get("username") else str(uid)
                cnt = stats_by_uid.get(uid, 0)
                tok = "✅ токены" if token_ok else "❌ без токенов"
                lines.append(f"• {name} (ID: {uid}) — токенов: {cnt}, {tok}")
            if not members:
                lines.append("Саппортов нет. /addsupport — добавить.")
            text = "👥 Саппорты группы:\n\n" + "\n".join(lines)
            rows = [[InlineKeyboardButton("📊 Статистика по группе", callback_data="group:stats")]]
            for m in members:
                uid = m["user_id"]
                token_ok = m.get("token_access", False)
                row = await get_user(uid)
                name = (row and row.get("username")) and f"@{row['username']}" or str(uid)
                if len(name) > 25:
                    name = str(uid)
                rows.append([
                    InlineKeyboardButton(f"🚫 Забрать — {name}", callback_data=f"group_revoke:{uid}"),
                    InlineKeyboardButton(
                        "✅ Токены" if token_ok else "❌ Токены",
                        callback_data=f"group_tokens:{uid}:{'0' if token_ok else '1'}",
                    ),
                ])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
        else:
            await query.answer("Не удалось изменить доступ (пользователь не в саппортах).", show_alert=True)


async def handle_tokens_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка инлайн-кнопок выдачи токенов (tokens:get:N). В группе — токены владельца."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("tokens:"):
        return
    await query.answer()
    rc = await _get_reply_and_credit_or_deny(update)
    if rc is None:
        return
    reply_chat_id, credit_user_id = rc
    chat = query.message.chat if query.message else None
    user = query.from_user
    if chat and user and not await _can_access_tokens_in_group(chat, user.id):
        await query.edit_message_text(
            "Доступ к токенам в этой группе только у владельца. Владелец может выдать вам доступ в /supportlist."
        )
        return
    reply_thread_id = _get_message_thread_id(update)

    data = query.data
    if data == "tokens:zip_toggle":
        if credit_user_id in _zip_mode_users:
            _zip_mode_users.discard(credit_user_id)
        else:
            _zip_mode_users.add(credit_user_id)
        # Обновим экран токенов
        total_unused = await get_user_tokens_count(credit_user_id, only_unused=True)
        total_all = await get_user_tokens_count(credit_user_id, only_unused=False)
        zip_on = credit_user_id in _zip_mode_users
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Выдать 1", callback_data="tokens:get:1"),
                    InlineKeyboardButton("Выдать 3", callback_data="tokens:get:3"),
                ],
                [
                    InlineKeyboardButton("Выдать 5", callback_data="tokens:get:5"),
                    InlineKeyboardButton(
                        "ZIP: вкл" if zip_on else "ZIP: выкл",
                        callback_data="tokens:zip_toggle",
                    ),
                ],
                [
                    InlineKeyboardButton("Ввести количество", callback_data="tokens:input"),
                    InlineKeyboardButton("◀ Назад", callback_data="menu:refresh"),
                ],
            ]
        )
        await query.edit_message_text(
            "📦 <b>Ваши токены</b>\n\n"
            f"• Доступно к выдаче: <b>{total_unused}</b>\n"
            f"• Всего сохранено: <b>{total_all}</b>\n\n"
            "Нажмите кнопку ниже, чтобы выдать нужное количество токенов.\n"
            "Каждый токен придёт в виде .txt-файла (при включённом ZIP — в одном архиве).",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    if data == "tokens:input":
        context.user_data["tokens_input"] = {
            "reply_chat_id": reply_chat_id,
            "credit_user_id": credit_user_id,
            "reply_thread_id": reply_thread_id,
        }
        await query.edit_message_text(
            "Введите количество токенов, которое нужно выдать (целое число).\n"
            "Чтобы отменить, отправьте «отмена».",
        )
        return

    parts = data.split(":", 2)
    if len(parts) != 3 or parts[1] != "get":
        return
    try:
        count = int(parts[2])
    except ValueError:
        await query.edit_message_text("Неверный формат запроса токенов.")
        return

    if count <= 0:
        await query.edit_message_text("Количество должно быть положительным числом.")
        return

    tokens = await pop_user_tokens(credit_user_id, count)
    if not tokens:
        await query.edit_message_text("У вас нет доступных токенов для выдачи.")
        return

    zip_on = credit_user_id in _zip_mode_users
    await _send_tokens_as_txt_file(
        reply_chat_id,
        context.application,
        [],
        filename_prefix="max_tokens",
        caption=(
            f"✅ Выдано токенов: {len(tokens)}.\n"
            "Откройте файл, скопируйте нужный блок и вставьте в нужном месте для переноса сессии."
        ),
        message_thread_id=reply_thread_id,
        blocks_with_prefixes=tokens,
        zip_mode=zip_on,
    )
    try:
        await query.delete_message()
    except Exception:
        pass


async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатий кнопок главного меню."""
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    rc = await _get_reply_and_credit_or_deny(update)
    if rc is None:
        return
    reply_chat_id, credit_user_id = rc
    if text == BTN_MENU:
        show_admin = await is_admin(credit_user_id)
        uid = update.effective_user.id if update.effective_user else credit_user_id
        welcome_html = await _welcome_caption_html(uid)
        photo_path = _menu_photo_path()
        if photo_path:
            try:
                with open(photo_path, "rb") as f:
                    await update.message.reply_photo(
                        photo=f,
                        caption=welcome_html,
                        parse_mode="HTML",
                        reply_markup=_menu_inline_kb(show_admin),
                    )
            except Exception as e:
                logging.warning("Не удалось отправить фото меню: %s", e)
                await update.message.reply_text(
                    welcome_html,
                    parse_mode="HTML",
                    reply_markup=_menu_inline_kb(show_admin),
                )
        else:
            await update.message.reply_text(
                welcome_html,
                parse_mode="HTML",
                reply_markup=_menu_inline_kb(show_admin),
            )
        return
    if text == BTN_HELP:
        await update.message.reply_text(
            _help_guide_text(),
            parse_mode="HTML",
        )
        return
    if text == BTN_QR:
        if _qr_active_count.get(reply_chat_id, 0) >= 5:
            await update.message.reply_text(
                "У вас уже запущено 5 операций QR одновременно. "
                "Дождитесь завершения текущих перед запуском новых."
            )
            return
        if not await _require_balance_before_token_flow(
            context.application, credit_user_id, reply_chat_id, _get_message_thread_id(update)
        ):
            return
        await update.message.reply_text(
            "Готовлю QR… Подождите до минуты."
        )
        _start_qr_flow(
            reply_chat_id, credit_user_id, context.application,
            _get_message_thread_id(update),
            update.effective_user.id if update.effective_user else None,
        )
        return
    if text == BTN_PHONE:
        if get_login_token_by_phone_async is None:
            await update.message.reply_text(
                f"Привязка по номеру недоступна. Установите: pip install msgpack websockets\n({_register_account_error})"
            )
            return
        if not await _require_balance_before_token_flow(
            context.application, credit_user_id, reply_chat_id, _get_message_thread_id(update)
        ):
            return
        context.user_data["phone_flow"] = {
            "step": "phone",
            "reply_chat_id": reply_chat_id,
            "credit_user_id": credit_user_id,
            "reply_thread_id": _get_message_thread_id(update),
            "actor_user_id": update.effective_user.id if update.effective_user else None,
        }
        phone_cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data="phone_cancel")],
        ])
        await update.message.reply_text(
            "📞 <b>Привязка по номеру</b>\n\n"
            "Отправьте номер: <code>+79001234567</code>, <code>9001234567</code> или <code>8 900 123 45 67</code>",
            parse_mode="HTML",
            reply_markup=phone_cancel_kb,
        )
        return
    if text == BTN_PROXY:
        msg = await update.message.reply_text("Проверяю IP…")
        try:
            ip, used_proxy, proxy_desc = await check_proxy_ip()
            if proxy_desc == PROXY_EXHAUSTED_MSG:
                txt = proxy_desc
            elif used_proxy and proxy_desc:
                txt = f"✅ IP: <code>{ip}</code>\n\nСравните с IP в приложении: Настройки → Устройства."
            else:
                txt = f"IP: <code>{ip}</code>\n\nСравните с IP в приложении: Настройки → Устройства."
            await msg.edit_text(txt, parse_mode="HTML")
        except Exception as e:
            await msg.edit_text(f"Ошибка: {e}")
        return
    if text == "⚙️ Админка":
        if not await is_admin(credit_user_id):
            await update.message.reply_text("Недостаточно прав.")
            return
        current_price_cents = await _token_creation_price_cents()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton("👥 Пользователи", callback_data="admin:users")],
            [InlineKeyboardButton("🧩 Прокси", callback_data="admin:proxies")],
            [InlineKeyboardButton(f"💵 Цена токена: {_format_usd(current_price_cents)}", callback_data="admin:price")],
            [InlineKeyboardButton("📣 Рассылка", callback_data="admin:broadcast")],
        ])
        await update.message.reply_text(
            "⚙️ <b>Админ-панель</b>\n\n"
            "Разрешить: /admin allow 123456\n"
            "Запретить: /admin deny 123456\n"
            "Баланс: /admin balance 123456 10.5\n"
            "Цена токена: /admin price 0.25\n"
            "Прокси: /admin proxies",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка инлайн-кнопок меню."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("menu:"):
        return
    await query.answer()
    rc = await _get_reply_and_credit_or_deny(update)
    if rc is None:
        return
    reply_chat_id, credit_user_id = rc
    reply_thread_id = _get_message_thread_id(update)
    sub = query.data.split(":", 1)[1]
    if sub == "qr":
        if _qr_active_count.get(reply_chat_id, 0) >= 5:
            await _edit_or_resend_text(
                query,
                "У вас уже запущено 5 операций QR одновременно. "
                "Дождитесь завершения текущих перед запуском новых.",
                context.bot,
            )
        else:
            if not await _require_balance_before_token_flow(
                context.application,
                credit_user_id,
                reply_chat_id,
                reply_thread_id,
                edit_query=query,
            ):
                return
            await _edit_or_resend_text(
                query, "Готовлю QR… Подождите до минуты.", context.bot
            )
            _start_qr_flow(
                reply_chat_id, credit_user_id, context.application,
                reply_thread_id,
                update.effective_user.id if update.effective_user else None,
            )
    elif sub == "phone":
        if get_login_token_by_phone_async is None:
            await _edit_or_resend_text(
                query, f"Недоступно: {_register_account_error}", context.bot
            )
            return
        if not await _require_balance_before_token_flow(
            context.application,
            credit_user_id,
            reply_chat_id,
            reply_thread_id,
            edit_query=query,
        ):
            return
        context.user_data["phone_flow"] = {
            "step": "phone",
            "reply_chat_id": reply_chat_id,
            "credit_user_id": credit_user_id,
            "reply_thread_id": reply_thread_id,
            "actor_user_id": update.effective_user.id if update.effective_user else None,
        }
        phone_cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data="phone_cancel")],
        ])
        await _edit_or_resend_text(
            query,
            "📞 Отправьте номер: +79001234567 или 9001234567",
            context.bot,
            reply_markup=phone_cancel_kb,
        )
    elif sub == "help":
        await _edit_or_resend_text(
            query,
            _help_guide_text(),
            context.bot,
            parse_mode="HTML",
        )
    elif sub == "balance":
        uid = query.from_user.id if query.from_user else credit_user_id
        cents = await get_balance_cents(uid)
        await _edit_or_resend_text(
            query,
            f"💰 <b>Ваш баланс</b>\n\n{_format_usd(cents)} USD",
            context.bot,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ Меню", callback_data="menu:refresh")]]
            ),
        )
    elif sub == "deposit":
        if not is_cryptopay_configured():
            await _edit_or_resend_text(
                query,
                "Пополнение не настроено (нет CRYPTOPAY_API_TOKEN).",
                context.bot,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀ Меню", callback_data="menu:refresh")]]
                ),
            )
        else:
            await _edit_or_resend_text(
                query,
                "💳 Выберите сумму пополнения (USD):",
                context.bot,
                reply_markup=_deposit_amount_kb(),
            )
    elif sub == "tokens":
        chat = query.message.chat if query.message else None
        user = query.from_user
        if chat and user and not await _can_access_tokens_in_group(chat, user.id):
            await _edit_or_resend_text(
                query,
                "Доступ к токенам в этой группе только у владельца. Владелец может выдать вам доступ в /supportlist.",
                context.bot,
            )
            return
        total_unused = await get_user_tokens_count(credit_user_id, only_unused=True)
        total_all = await get_user_tokens_count(credit_user_id, only_unused=False)
        zip_on = credit_user_id in _zip_mode_users
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Выдать 1", callback_data="tokens:get:1"),
                    InlineKeyboardButton("Выдать 3", callback_data="tokens:get:3"),
                ],
                [
                    InlineKeyboardButton("Выдать 5", callback_data="tokens:get:5"),
                    InlineKeyboardButton(
                        "ZIP: вкл" if zip_on else "ZIP: выкл",
                        callback_data="tokens:zip_toggle",
                    ),
                ],
                [
                    InlineKeyboardButton("Ввести количество", callback_data="tokens:input"),
                    InlineKeyboardButton("◀ Назад", callback_data="menu:refresh"),
                ],
            ]
        )
        await _edit_or_resend_text(
            query,
            "📦 <b>Ваши токены</b>\n\n"
            f"• Доступно к выдаче: <b>{total_unused}</b>\n"
            f"• Всего сохранено: <b>{total_all}</b>\n\n"
            "Нажмите кнопку ниже, чтобы выдать нужное количество токенов.\n"
            "Каждый токен придёт в виде .txt-файла.",
            context.bot,
            parse_mode="HTML",
            reply_markup=kb,
        )
    elif sub == "refresh":
        show_admin = await is_admin(credit_user_id)
        uid = query.from_user.id if query.from_user else credit_user_id
        welcome_html = await _welcome_caption_html(uid)
        photo_path = _menu_photo_path()
        try:
            if photo_path:
                try:
                    await query.delete_message()
                    kw = _send_kwargs(reply_chat_id, reply_thread_id)
                    with open(photo_path, "rb") as f:
                        await context.bot.send_photo(
                            **kw,
                            photo=f,
                            caption=welcome_html,
                            parse_mode="HTML",
                            reply_markup=_menu_inline_kb(show_admin),
                        )
                except Exception as e:
                    logging.warning("Не удалось отправить фото меню: %s", e)
                    await _edit_or_resend_text(
                        query,
                        welcome_html,
                        context.bot,
                        parse_mode="HTML",
                        reply_markup=_menu_inline_kb(show_admin),
                    )
            else:
                await query.edit_message_text(
                    welcome_html,
                    parse_mode="HTML",
                    reply_markup=_menu_inline_kb(show_admin),
                )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    elif sub == "admin":
        if not await is_admin(credit_user_id):
            await query.answer("Недостаточно прав.")
            return
        current_price_cents = await _token_creation_price_cents()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton("👥 Пользователи", callback_data="admin:users")],
            [InlineKeyboardButton("🧩 Прокси", callback_data="admin:proxies")],
            [InlineKeyboardButton(f"💵 Цена токена: {_format_usd(current_price_cents)}", callback_data="admin:price")],
            [InlineKeyboardButton("📣 Рассылка", callback_data="admin:broadcast")],
            [InlineKeyboardButton("◀ Назад", callback_data="menu:refresh")],
        ])
        await _edit_or_resend_text(
            query,
            "⚙️ <b>Админ-панель</b>\n\n"
            "/admin allow 123456 — разрешить\n"
            "/admin deny 123456 — запретить\n"
            "/admin balance 123456 10.5 — баланс USD\n"
            "/admin price 0.25 — цена токена\n"
            "/admin proxies — список прокси",
            context.bot,
            parse_mode="HTML",
            reply_markup=kb,
        )


async def handle_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Создание счёта Crypto Pay (pay:amt:*) и проверка оплаты (pay:check:*)."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("pay:"):
        return

    async def _answer_once(text: str | None = None, *, show_alert: bool = False) -> None:
        """Ровно один ответ на callback (иначе Telegram отклоняет повторный answer)."""
        try:
            if text is None:
                await query.answer()
            else:
                await query.answer(text, show_alert=show_alert)
        except BadRequest:
            pass

    rc = await _get_reply_and_credit_or_deny(update)
    if rc is None:
        await _answer_once()
        return
    reply_chat_id, _credit_user_id = rc
    reply_thread_id = _get_message_thread_id(update)
    user = query.from_user
    if not user:
        await _answer_once()
        return
    uid = user.id
    parts = query.data.split(":")
    if len(parts) < 2:
        await _answer_once()
        return
    kind = parts[1]

    if kind == "amt":
        if len(parts) < 3:
            await _answer_once()
            return
        try:
            cents = int(parts[2])
        except ValueError:
            await _answer_once("Неверная сумма", show_alert=True)
            return
        if not is_cryptopay_configured():
            await _answer_once("Пополнение не настроено.", show_alert=True)
            return
        if cents < MIN_DEPOSIT_CENTS or cents > MAX_DEPOSIT_CENTS:
            await _answer_once(
                f"Сумма от {_format_usd(MIN_DEPOSIT_CENTS)} до {_format_usd(MAX_DEPOSIT_CENTS)}",
                show_alert=True,
            )
            return
        await _answer_once()
        await _create_and_send_crypto_invoice(
            uid=uid,
            amount_cents=cents,
            bot=context.bot,
            reply_chat_id=reply_chat_id,
            reply_thread_id=reply_thread_id,
            edit_query=query,
        )
        return

    if kind == "check":
        if len(parts) < 3:
            await _answer_once()
            return
        try:
            local_id = int(parts[2])
        except ValueError:
            await _answer_once("Неверный запрос", show_alert=True)
            return
        row = await get_crypto_invoice_local(local_id, uid)
        if not row:
            await _answer_once("Счёт не найден.", show_alert=True)
            return
        if row.get("status") == "paid":
            await _answer_once()
            await _edit_or_resend_text(
                query,
                "✅ По этому счёту баланс уже был пополнен ранее.",
                context.bot,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀ Меню", callback_data="menu:refresh")]]
                ),
            )
            return
        try:
            inv = await get_invoice_by_id(int(row["invoice_id"]))
        except Exception as e:
            logging.exception("get_invoice_by_id failed")
            await _answer_once(f"Ошибка API: {e}", show_alert=True)
            return
        if not inv or str(inv.get("status", "")).lower() != "paid":
            await _answer_once(
                "Оплата ещё не поступила. Откройте Crypto Bot и оплатите счёт.",
                show_alert=True,
            )
            return
        amt = int(row.get("amount_cents") or 0)
        ok = await finalize_paid_crypto_invoice(local_id, uid)
        row2 = await get_crypto_invoice_local(local_id, uid)
        if ok or (row2 and row2.get("status") == "paid"):
            await _answer_once()
            await _edit_or_resend_text(
                query,
                _text_balance_topped_up(amt),
                context.bot,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀ Меню", callback_data="menu:refresh")]]
                ),
            )
            await mark_crypto_invoice_notified(local_id)
        else:
            await _answer_once(
                "Не удалось зачислить. Повторите «Проверить оплату» или напишите в поддержку.",
                show_alert=True,
            )


async def handle_phone_flow_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    if user:
        await ensure_user(user.id, user.username or user.first_name or "")
    if update.effective_chat.type == "private" and not await is_allowed(user.id if user else 0):
        return
    text = update.message.text.strip()

    message_thread_id = getattr(update.message, "message_thread_id", None)
    waiter_key = (chat_id, message_thread_id)
    if waiter_key in _password_waiters:
        fut = _password_waiters.pop(waiter_key)
        if not fut.done():
            cancel_phrases = ("отмена", "cancel", "отменить", "не могу отменить", "выйти", "стоп")
            if text.lower() in cancel_phrases or (len(text) < 25 and "отмен" in text.lower()):
                fut.set_exception(SkipAuthorizationError())
                await update.message.reply_text(
                    "Авторизация отменена. Отправьте /link_phone для другого номера."
                )
            else:
                fut.set_result(text)
        return

    # Режим изменения цены токена для админа: следующее сообщение — новая цена в USD
    if context.user_data.get("admin_token_price_mode") == "await_value":
        if not user or not await is_admin(user.id):
            context.user_data.pop("admin_token_price_mode", None)
            return
        if text.lower() in ("отмена", "cancel", "стоп", "stop"):
            context.user_data.pop("admin_token_price_mode", None)
            await update.message.reply_text("Изменение цены токена отменено.")
            return
        try:
            usd = float(text.replace(",", "."))
            cents = max(0, int(round(usd * 100)))
        except ValueError:
            await update.message.reply_text(
                "Введите число в USD, например 0.25 или 1. "
                "Или отправьте «отмена»."
            )
            return
        await set_token_price_cents(cents)
        context.user_data.pop("admin_token_price_mode", None)
        await update.message.reply_text(
            f"✅ Цена создания токена обновлена: <b>{_format_usd(cents)} USD</b>.",
            parse_mode="HTML",
        )
        return

    # Режим рассылки для админа: следующее текстовое сообщение после нажатия кнопки «📣 Рассылка»
    if context.user_data.get("broadcast_mode") == "await_text":
        # Только в личке админа имеет смысл, но проверяем только права
        if not user or not await is_admin(user.id):
            context.user_data.pop("broadcast_mode", None)
            return
        if text.lower() in ("отмена", "cancel", "стоп", "stop"):
            context.user_data.pop("broadcast_mode", None)
            await update.message.reply_text("Рассылка отменена.")
            return

        context.user_data.pop("broadcast_mode", None)
        users = await list_users()
        recipient_ids = [u["user_id"] for u in users if u.get("allowed") or u.get("is_admin")]
        if not recipient_ids:
            await update.message.reply_text("Нет пользователей с доступом для рассылки.")
            return

        sent = 0
        failed = 0
        for uid in recipient_ids:
            try:
                await context.application.bot.send_message(chat_id=uid, text=text)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
                continue

        await update.message.reply_text(
            f"Рассылка завершена.\nУспешно: {sent}\nНе удалось отправить: {failed}"
        )
        return

    # Режим ввода количества токенов после кнопки «Ввести количество»
    tokens_input = context.user_data.get("tokens_input")
    if tokens_input:
        if text.lower() in ("отмена", "cancel", "стоп", "stop"):
            context.user_data.pop("tokens_input", None)
            await update.message.reply_text("Ввод количества токенов отменён.")
            return
        try:
            count = int(text)
        except ValueError:
            await update.message.reply_text("Укажите целое число токенов или напишите «отмена».")
            return
        if count <= 0:
            await update.message.reply_text("Количество должно быть положительным числом.")
            return

        context.user_data.pop("tokens_input", None)
        reply_chat_id = tokens_input.get("reply_chat_id", chat_id)
        credit_user_id = tokens_input.get("credit_user_id", user.id if user else chat_id)
        reply_thread_id = tokens_input.get("reply_thread_id")

        tokens = await pop_user_tokens(credit_user_id, count)
        if not tokens:
            await update.message.reply_text("У вас нет доступных токенов для выдачи.")
            return

        zip_on = credit_user_id in _zip_mode_users or len(tokens) > 15
        await _send_tokens_as_txt_file(
            reply_chat_id,
            context.application,
            [],
            filename_prefix="max_tokens",
            caption=(
                f"✅ Выдано токенов: {len(tokens)}.\n"
                "Откройте файл, скопируйте нужный блок и вставьте в нужном месте для переноса сессии."
            ),
            message_thread_id=reply_thread_id,
            blocks_with_prefixes=tokens,
            zip_mode=zip_on,
        )
        return

    flow = context.user_data.get("phone_flow")
    if not flow or flow.get("step") != "phone":
        return

    reply_chat_id = flow.get("reply_chat_id", chat_id)
    credit_user_id = flow.get("credit_user_id", chat_id)
    reply_thread_id = flow.get("reply_thread_id")
    actor_user_id = flow.get("actor_user_id")

    phone = _normalize_phone(text)
    if not phone:
        context.user_data.pop("phone_flow", None)
        await update.message.reply_text(
            "Не похоже на номер. Когда будете готовы ввести номер — нажмите «По номеру» или отправьте /link_phone.\n"
            "Примеры: +79001234567, 9001234567, 8 900 123 45 67"
        )
        return
    context.user_data.pop("phone_flow", None)
    await update.message.reply_text("Запрашиваю SMS-код…")
    _create_background_task(run_full_link_phone(
        reply_chat_id, credit_user_id, context.application, phone,
        reply_thread_id, actor_user_id,
    ))


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Задайте TELEGRAM_BOT_TOKEN в .env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    # Убираем логи каждого запроса к Telegram API (getUpdates и т.д.)
    for name in ("httpx", "httpcore", "telegram.request"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Python 3.10+: в MainThread нет event loop по умолчанию
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    async def post_init(app: Application) -> None:
        try:
            await init_db()
        except Exception:
            logging.exception("Ошибка инициализации БД (init_db)")
            raise
        _create_background_task(_proxy_seller_poll_loop(app))
        _create_background_task(_crypto_pay_poll_loop(app))

    async def post_shutdown(_app: Application) -> None:
        """Корректная остановка: отмена фоновых задач, затем короткая пауза для завершения."""
        if _background_tasks:
            for t in list(_background_tasks):
                t.cancel()
            await asyncio.gather(*_background_tasks, return_exceptions=True)
            await asyncio.sleep(0.3)

    app = Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()
    _MENU_TEXTS = {BTN_MENU, BTN_HELP}
    menu_filter = filters.Regex("^(?:" + "|".join(re.escape(s) for s in _MENU_TEXTS) + ")$")
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("addsupport", cmd_addsupport))
    app.add_handler(CommandHandler("delsupport", cmd_delsupport))
    app.add_handler(CommandHandler("supportlist", cmd_supportlist))
    app.add_handler(CommandHandler("groupstats", cmd_groupstats))
    app.add_handler(MessageHandler(menu_filter, handle_menu_button))
    app.add_handler(CallbackQueryHandler(handle_pay_callback, pattern=r"^pay:"))
    app.add_handler(CallbackQueryHandler(handle_menu_callback, pattern=r"^menu:"))
    app.add_handler(CommandHandler("qr", cmd_qr))
    app.add_handler(CommandHandler("check_proxy", cmd_check_proxy))
    app.add_handler(CommandHandler("tokens", cmd_tokens))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("topup", cmd_topup))
    app.add_handler(CommandHandler("link_phone", cmd_link_phone))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(handle_admin_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(handle_skip_sms_callback, pattern=r"^skip_sms:-?\d+(:\d+)?$"))
    app.add_handler(CallbackQueryHandler(handle_phone_cancel_callback, pattern=r"^phone_cancel$"))
    app.add_handler(CallbackQueryHandler(handle_password_cancel_callback, pattern=r"^password_cancel:-?\d+(:\d+)?$"))
    app.add_handler(CallbackQueryHandler(handle_qr_cancel_callback, pattern=r"^qr_cancel(?::[\w\-]+)?$"))
    app.add_handler(CallbackQueryHandler(handle_tokens_callback, pattern=r"^tokens:"))
    app.add_handler(CallbackQueryHandler(handle_group_support_callback, pattern=r"^group"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone_flow_message))

    async def on_error(_update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if isinstance(ctx.error, Conflict):
            logging.error(
                "Conflict: уже запущен другой экземпляр бота с этим токеном. "
                "Остановите все копии (Ctrl+C в других терминалах) и оставьте только один процесс."
            )
            ctx.application.stop()
        elif isinstance(ctx.error, (sqlite3.Error, OSError)):
            logging.exception("Ошибка БД или доступа к файлам: %s", ctx.error)
            if _update is not None and getattr(_update, "effective_chat", None) is not None:
                try:
                    await ctx.application.bot.send_message(
                        _update.effective_chat.id,
                        "Временная ошибка, попробуйте позже.",
                    )
                except Exception:
                    pass
        else:
            logging.exception("Необработанная ошибка: %s", ctx.error)

    app.add_error_handler(on_error)
    try:
        print("Бот запущен. Остановка: Ctrl+C")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        print("\nОстановка бота…")
    print("Бот остановлен. Данные в БД сохранены.")


if __name__ == "__main__":
    main()

# SAFE_DB_UPLOAD_PATCH

async def safe_replace_database(document, temp_name: str = "bot_new.db", target_name: str = "bot.db"):
    """
    Безопасная замена SQLite БД:
    - скачивание во временный файл
    - integrity_check
    - атомарная замена
    """
    import sqlite3
    import os

    await document.get_file().download_to_drive(temp_name)

    conn = sqlite3.connect(temp_name)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("SQLite integrity_check failed")
    finally:
        conn.close()

    os.replace(temp_name, target_name)


async def handle_admin_inline_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data or ""

    if not data.startswith("admin:"):
        return

    await query.answer()

    sub = data.split(":", 1)[1]

    if sub == "stats":
        await query.edit_message_text("📊 Статистика", reply_markup=_admin_panel_kb())

    elif sub == "users":
        await query.edit_message_text("👥 Пользователи", reply_markup=_admin_panel_kb())

    elif sub == "proxies":
        await query.edit_message_text("🔌 Прокси", reply_markup=_admin_panel_kb())

    elif sub == "price":
        await query.edit_message_text("💵 Прайс", reply_markup=_admin_panel_kb())

    elif sub == "broadcast":
        await query.edit_message_text("📢 Рассылка", reply_markup=_admin_panel_kb())

    elif sub == "settings":
        await query.edit_message_text("⚙️ Настройки", reply_markup=_admin_panel_kb())

    elif sub == "work":
        global WORK_MODE
        WORK_MODE = not WORK_MODE

        status = "✅ ВКЛ" if WORK_MODE else "❌ ВЫКЛ"

        await query.edit_message_text(
            f"WORK MODE: {status}",
            reply_markup=_admin_panel_kb(),
        )
