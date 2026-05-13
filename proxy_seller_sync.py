import os
from pathlib import Path
from typing import List, Optional, Tuple

from proxy_seller_user_api import Api

from db import log_proxy_add, log_proxy_remove, list_proxies_meta


PROXIES_FILE = Path(__file__).with_name("proxies.txt")


class ProxySellerError(Exception):
    pass


def _get_api_key() -> str:
    key = os.getenv("PROXY_SELLER_API_KEY", "").strip()
    if not key:
        raise ProxySellerError("PROXY_SELLER_API_KEY не задан в .env")
    return key


def _parse_proxy_item(item: dict) -> Optional[str]:
    """Из элемента API (dict) собирает строку http://login:pass@ip:port или None."""
    if not isinstance(item, dict):
        return None
    ip = str(item.get("ip") or "").strip()
    login = str(item.get("login") or "").strip()
    password = (
        str(item.get("password") or item.get("pass") or item.get("passwd") or "").strip()
    )
    port = item.get("port_http") or item.get("port") or item.get("port_socks")
    try:
        port_int = int(port) if port is not None else None
    except (TypeError, ValueError):
        port_int = None
    if not (ip and login and password and port_int):
        return None
    return f"http://{login}:{password}@{ip}:{port_int}"


def _is_item_active(item: dict) -> bool:
    """Проверяет, что прокси помечен как активный (не истёкший, не рекомендация)."""
    status = (str(item.get("status") or "").strip()).upper()
    status_type = (str(item.get("status_type") or "").strip()).upper()
    return status in ("ACTIVE", "АКТИВЕН") or status_type == "ACTIVE"


def _extract_items_from_response(lists_raw) -> list:
    """Приводит ответ proxy/list к списку элементов (dict). Документация: data.items для типа ipv4, data.ipv4 для всех типов."""
    if isinstance(lists_raw, list):
        return lists_raw
    if isinstance(lists_raw, dict):
        # proxy/list/ipv4 → data: { "items": [ {...}, ... ] }
        if isinstance(lists_raw.get("items"), list):
            return lists_raw["items"]
        # proxy/list → data: { "ipv4": [ {...}, ... ], "ipv6": [], ... }
        if isinstance(lists_raw.get("ipv4"), list):
            return lists_raw["ipv4"]
        if isinstance(lists_raw.get("data"), list):
            return lists_raw["data"]
    return []


def _normalize_proxy_line(line: str) -> Optional[str]:
    """Приводит строку из export (login:password@ip:port или ip:port:login:pass) к http://login:password@ip:port."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        return line if line.startswith("http") else None
    # login:password@ip:port
    if "@" in line:
        rest = line
        if ":" in rest:
            pre, rest = rest.split("@", 1)
            if ":" in rest:
                ip, port = rest.rsplit(":", 1)
                if port.isdigit() and pre:
                    return f"http://{pre}@{ip}:{port}"
    # ip:port:login:password
    parts = line.split(":")
    if len(parts) >= 4:
        ip, port, login, password = parts[0], parts[1], parts[2], ":".join(parts[3:])
        if port.isdigit():
            return f"http://{login}:{password}@{ip}:{port}"
    return None


def _fetch_via_download(api: Api) -> List[str]:
    """
    Получение списка через proxy/download/ipv4 (Export IPs).
    По документации этот endpoint отдаёт список прокси для экспорта — как в ЛК, только активные.
    """
    try:
        raw = api.proxyDownload("ipv4", ext="txt")
    except Exception:
        return []
    lines: List[str] = []
    if isinstance(raw, str):
        lines = [s.strip() for s in raw.splitlines() if s.strip()]
    elif isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
            lines = [s.strip() for s in text.splitlines() if s.strip()]
        except Exception:
            return []
    elif isinstance(raw, dict) and isinstance(raw.get("data"), str):
        lines = [s.strip() for s in raw["data"].splitlines() if s.strip()]
    out: List[str] = []
    for line in lines:
        p = _normalize_proxy_line(line)
        if p:
            out.append(p)
    return out


async def fetch_proxies_from_api() -> List[str]:
    """
    Получает список активных прокси из Proxy-Seller.
    1) Сначала пробуем proxy/download/ipv4 (Export) — по доке это список для экспорта, как в ЛК.
    2) Если пусто — proxy/list/ipv4 с фильтром status/status_type == Active.
    Файл при синхронизации перезаписывается полностью.
    """
    api_key = _get_api_key()
    api = Api({"key": api_key})

    # 1. Приоритет: Export (proxy/download/ipv4) — только активные, как в ЛК
    all_proxies = _fetch_via_download(api)

    # 2. Fallback: proxy/list или proxy/list/ipv4, фильтр по status == Active
    if not all_proxies:
        for call in [lambda: api.proxyList("ipv4"), lambda: api.proxyList()]:
            try:
                lists_raw = call()
                if lists_raw is None:
                    continue
                items = _extract_items_from_response(lists_raw)
                for item in items:
                    if not isinstance(item, dict) or not _is_item_active(item):
                        continue
                    proxy_str = _parse_proxy_item(item)
                    if proxy_str:
                        all_proxies.append(proxy_str)
                if all_proxies:
                    break
            except Exception:
                continue

    if not all_proxies:
        raise ProxySellerError("API Proxy-Seller не вернул ни одного активного прокси.")

    # Опциональный лимит в .env: PROXY_SELLER_MAX_PROXIES=100
    try:
        max_prox = int(os.getenv("PROXY_SELLER_MAX_PROXIES", "0").strip() or "0")
        if max_prox > 0 and len(all_proxies) > max_prox:
            all_proxies = all_proxies[:max_prox]
    except ValueError:
        pass

    seen: set[str] = set()
    unique: List[str] = []
    for p in all_proxies:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


async def sync_proxies_with_file_and_db(merge: bool = True) -> Tuple[int, int]:
    """
    Синхронизирует прокси из Proxy-Seller с локальным proxies.txt и БД.

    merge=True  — добавляем новые прокси, старые не трогаем.
    merge=False — жёсткая перезапись: в файле остаётся только то, что вернул API (только активные).
                  Список из API полностью перезаписывает файл.

    Возвращает (added, removed).
    """
    api_proxies = await fetch_proxies_from_api()

    try:
        lines = PROXIES_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    current = [l.strip() for l in lines if l.strip() and not l.startswith("#")]

    current_set = set(current)
    api_set = set(api_proxies)

    added = [p for p in api_proxies if p not in current_set]
    removed: List[str] = []

    if not merge:
        removed = [p for p in current if p not in api_set]
        # Полная перезапись файла — только то, что вернул API
        new_file_list = list(api_proxies)
    else:
        new_file_list = list(current)
        for p in api_proxies:
            if p not in current_set:
                new_file_list.append(p)

    # Всегда перезаписываем файл целиком (не дополняем)
    body = "\n".join(new_file_list) + ("\n" if new_file_list else "")
    PROXIES_FILE.write_text(body, encoding="utf-8")

    # Логируем изменения в БД
    for p in added:
        await log_proxy_add(p)
    for p in removed:
        await log_proxy_remove(p)

    return len(added), len(removed)

