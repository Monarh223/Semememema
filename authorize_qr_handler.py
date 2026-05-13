import asyncio
import logging
from io import BytesIO

from PIL import Image
from pyzbar.pyzbar import decode
from telegram import Update
from telegram.ext import CommandHandler, CallbackContext

from db import get_user_tokens_count, pop_user_tokens
from register_account import MaxClient

logger = logging.getLogger(__name__)


async def cmd_authorize_qr(update: Update, context: CallbackContext):
    """Обработчик команды /authorize_qr — принимает фото с QR‑кодом и авторизует сессию."""
    user = update.effective_user
    if not user:
        return

    if not update.message or not update.message.photo:
        await update.message.reply_text(
            "Отправьте фото QR-кода (не как файл, а как картинку) вместе с командой /authorize_qr."
        )
        return

    # Эти импорты уже есть в начале файла, но можно и так
    total = await get_user_tokens_count(user.id, only_unused=True)
    if total == 0:
        await update.message.reply_text(
            "У вас нет сохранённых токенов. Сначала войдите по номеру: /link_phone"
        )
        return

    msg = await update.message.reply_text("🔍 Распознаю QR-код…")

    try:
        # Скачиваем фото
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        # Декодируем QR
        img = Image.open(BytesIO(image_bytes))
        qr_codes = decode(img)

        if not qr_codes:
            await msg.edit_text("❌ QR-код не найден на фото. Попробуйте другой скриншот.")
            return

        qr_link = qr_codes[0].data.decode("utf-8")
        logger.info(f"QR decoded: {qr_link[:80]}...")

        # Берём токен пользователя
        tokens = await pop_user_tokens(user.id, 1)
        if not tokens:
            await msg.edit_text("❌ Не удалось получить токен. Попробуйте /link_phone заново.")
            return

        token, _ = tokens[0]

        # Авторизуем QR через существующий токен
        client = MaxClient(ver=11)
        client.auth_token = token

        await client.connect()
        await client.handshake()
        await client.auth_login(full_init=False)
        await client.authorize_qr(qr_link)
        await client.disconnect()

        await msg.edit_text("✅ QR успешно авторизован! Аккаунт привязан к устройству.")

    except Exception as e:
        logger.error(f"Ошибка авторизации QR: {e}")
        await msg.edit_text(f"❌ Ошибка: {str(e)[:200]}")


async def cmd_qr_from_token(update: Update, context: CallbackContext):
    """Генерация QR-кода из сохранённого токена."""
    user = update.effective_user
    if not user:
        return

    total = await get_user_tokens_count(user.id, only_unused=True)
    if total == 0:
        await update.message.reply_text(
            "У вас нет сохранённых токенов. Сначала войдите по номеру: /link_phone"
        )
        return

    tokens = await pop_user_tokens(user.id, 1)
    if not tokens:
        await update.message.reply_text("❌ Не удалось получить токен.")
        return

    token, _ = tokens[0]

    try:
        import qrcode as qr

        qr_img = qr.make(token)
        bio = BytesIO()
        qr_img.save(bio, format="PNG")
        bio.seek(0)
        bio.name = "qr_token.png"

        await update.message.reply_photo(
            photo=bio,
            caption="📱 Отсканируйте этот QR в приложении MAX: Профиль → Устройства → Войти по QR-коду.\n\nПосле сканирования вы войдёте в аккаунт."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка генерации QR: {e}")


def register_handlers(app):
    """Регистрирует новые обработчики в приложении."""
    app.add_handler(CommandHandler("authorize_qr", cmd_authorize_qr))
    app.add_handler(CommandHandler("qr_from_token", cmd_qr_from_token))
    logger.info("QR authorize handlers registered")
