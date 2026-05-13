"""
Клиент Crypto Pay API (@CryptoBot): счета в фиате USD, проверка оплаты через getInvoices.
Документация: https://help.send.tg/ru/articles/10279948-crypto-pay-api
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)


def cryptopay_base_url() -> str:
    if os.getenv("CRYPTOPAY_TESTNET", "").strip().lower() in ("1", "true", "yes"):
        return "https://testnet-pay.crypt.bot"
    return "https://pay.crypt.bot"


def cryptopay_token() -> str:
    return os.getenv("CRYPTOPAY_API_TOKEN", "").strip()


def is_cryptopay_configured() -> bool:
    return bool(cryptopay_token())


def _api_sync(method: str, params: dict[str, Any]) -> dict[str, Any]:
    token = cryptopay_token()
    if not token:
        raise RuntimeError("CRYPTOPAY_API_TOKEN не задан в .env")
    url = f"{cryptopay_base_url()}/api/{method}"
    r = requests.post(
        url,
        headers={"Crypto-Pay-API-Token": token},
        json=params,
        timeout=45,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        err = data.get("error") or data
        raise RuntimeError(f"CryptoPay API error: {err}")
    return data.get("result")


async def api_call(method: str, **params: Any) -> Any:
    return await asyncio.to_thread(_api_sync, method, params)


async def create_invoice_usd(
    amount_usd: float,
    payload: str,
    description: str = "Пополнение баланса",
    expires_in: int = 3600,
    accepted_assets: str | None = "USDT",
) -> dict[str, Any]:
    """
    Создаёт счёт в USD (фиат). amount_usd — например 10.5
    Оплата только в USDT (сеть TRC-20 в Crypto Bot), если accepted_assets задан.
    Возвращает объект Invoice (dict): invoice_id, bot_invoice_url, ...
    """
    params: dict[str, Any] = dict(
        currency_type="fiat",
        fiat="USD",
        amount=f"{amount_usd:.2f}",
        description=description[:1024],
        payload=payload[:4096] if payload else "",
        expires_in=expires_in,
        allow_comments=True,
        allow_anonymous=True,
    )
    if accepted_assets:
        params["accepted_assets"] = accepted_assets
    return await api_call("createInvoice", **params)


async def get_invoice_by_id(invoice_id: int) -> dict[str, Any] | None:
    """Возвращает один инвойс по invoice_id или None."""
    res = await api_call("getInvoices", invoice_ids=str(invoice_id), count=1)
    if res is None:
        return None
    if isinstance(res, list):
        return res[0] if res else None
    if isinstance(res, dict):
        for key in ("items", "invoices", "result"):
            inner = res.get(key)
            if isinstance(inner, list) and inner:
                return inner[0]
        if "invoice_id" in res or "status" in res:
            return res
    return None


_app_name_cache: str | None = None


async def get_crypto_app_display_name() -> str:
    """Имя приложения из getMe (для текста «Получатель: …»)."""
    global _app_name_cache
    if _app_name_cache:
        return _app_name_cache
    try:
        me = await get_me()
        name = (me.get("name") or me.get("app_name") or "").strip()
        if not name:
            name = "Crypto Pay"
        _app_name_cache = name
        return name
    except Exception:
        return "Crypto Pay"


async def get_me() -> dict[str, Any]:
    """Проверка токена (getMe)."""
    return await api_call("getMe")
