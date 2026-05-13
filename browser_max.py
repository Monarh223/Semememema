"""
Playwright: открывает web.max.ru, отдаёт скриншот QR,
после сканирования сам читает localStorage и возвращает готовый блок.
Прокси: PROXY_LIST в .env или USE_FREE_EU_PROXIES=true — тогда подставляются бесплатные EU (DE, FI и др.).
"""
import asyncio
import logging
import os
import random
import time
from typing import AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import quote, urlparse

import requests
from playwright.async_api import async_playwright

try:
    from socks5_bridge import start_socks5_bridge, stop_socks5_bridge
except ImportError:
    start_socks5_bridge = None
    stop_socks5_bridge = None

try:
    from db import log_proxy_usage, get_proxy_stats  # type: ignore
    _PROXY_STATS_AVAILABLE = True
except Exception:
    log_proxy_usage = None  # type: ignore
    get_proxy_stats = None  # type: ignore
    _PROXY_STATS_AVAILABLE = False

MAX_WEB_URL = "https://web.max.ru"

_free_eu_proxy_cache: tuple[list[dict], float] | None = None
FREE_EU_CACHE_TTL = 1200  # 20 мин
# По умолчанию только Германия и Финляндия
DEFAULT_FREE_EU_COUNTRIES = "DE,FI"
# Проверка прокси перед использованием (web.max.ru)
PROXY_CHECK_TIMEOUT = 5
PROXY_CHECK_MAX_ALIVE = 30
PROXY_CHECK_MAX_PARALLEL = 12
# Проверяем только первые N прокси из списка, чтобы не зависать
PROXY_CHECK_MAX_TO_SCAN = 45
# Общий таймаут на этап "собрать + проверить прокси" (сек)
FETCH_AND_FILTER_PROXIES_TIMEOUT = 40
# Лимит использования одного прокси (по use_count в БД).
PROXY_MAX_USE_COUNT = 20

# Здоровье прокси для API (api.oneme.ru) кэшируем, чтобы не гонять проверки каждый раз
API_TEST_URL = "https://api.oneme.ru"
PROXY_API_HEALTH_TTL = 600  # 10 минут
_proxy_api_health_cache: dict[str, bool] = {}
_proxy_api_health_checked_at: float = 0.0

# В странице выполняем этот JS — возвращает готовый блок или null.
# Ищем не только исторические точные ключи, но и близкие варианты, т.к. веб-клиент MAX
# периодически меняет имена ключей в storage.
GET_BLOCK_SCRIPT = """
() => {
  function pickByExactOrHint(store, exactKey, hints) {
    var val = store.getItem(exactKey);
    if (val) return val;
    try {
      for (var i = 0; i < store.length; i++) {
        var k = store.key(i) || "";
        var lk = k.toLowerCase();
        var ok = true;
        for (var j = 0; j < hints.length; j++) {
          if (lk.indexOf(hints[j]) === -1) {
            ok = false;
            break;
          }
        }
        if (ok) {
          var candidate = store.getItem(k);
          if (candidate) return candidate;
        }
      }
    } catch (e) {}
    return null;
  }

  var d = pickByExactOrHint(localStorage, '__oneme_device_id', ['oneme', 'device', 'id'])
       || pickByExactOrHint(sessionStorage, '__oneme_device_id', ['oneme', 'device', 'id']);
  var a = pickByExactOrHint(localStorage, '__oneme_auth', ['oneme', 'auth'])
       || pickByExactOrHint(sessionStorage, '__oneme_auth', ['oneme', 'auth']);
  if (!d || !a) {
    return null;
  }
  return [
    "sessionStorage.clear();",
    "localStorage.clear();",
    "localStorage.setItem('__oneme_device_id', " + JSON.stringify(d) + ");",
    "localStorage.setItem('__oneme_auth', " + JSON.stringify(a) + ");",
    "window.location.reload();"
  ].join("\\n");
}
"""


async def _dismiss_max_bot_check_modal(page) -> None:
    """
    Закрывает модалку «Проверяем, что вы не робот» на web.max.ru — кнопка «Продолжить».
    Без клика QR может не появиться или страница остаётся перекрытой.
    """
    if os.getenv("MAX_SKIP_BOT_CHECK_DISMISS", "").strip().lower() in ("1", "true", "yes"):
        return

    try:
        # Короткое ожидание: если модалки нет — выходим быстро (~≤800 мс)
        hint = page.get_by_text("не робот", exact=False).first
        if not await hint.is_visible(timeout=800):
            return
        for btn in (
            page.get_by_role("button", name="Продолжить").first,
            page.locator('button:has-text("Продолжить")').first,
        ):
            try:
                if await btn.is_visible(timeout=1500):
                    await btn.click(timeout=5000)
                    await page.wait_for_timeout(700)
                    return
            except Exception:
                continue
    except Exception:
        pass


def _parse_one_proxy(part: str) -> dict | None:
    """Парсит одну запись: http://user:pass@host:port или host:port:user:pass."""
    part = part.strip()
    if not part:
        return None
    # Формат host:port:user:pass — по умолчанию SOCKS5 (например 154.196.71.202:64619:user:pass)
    if part.count(":") >= 3 and "@" not in part:
        parts = part.split(":", 3)
        if len(parts) == 4 and parts[1].isdigit():
            host, port, username, password = parts[0], parts[1], parts[2], parts[3]
            server = f"socks5://{host}:{port}"
            return {"server": server, "username": username, "password": password}
    # Формат URL
    try:
        parsed = urlparse(part)
        if not parsed.hostname:
            return None
        port = parsed.port or (1080 if "socks" in (parsed.scheme or "") else 80)
        server = f"{parsed.scheme}://{parsed.hostname}:{port}"
        entry = {"server": server}
        if parsed.username:
            entry["username"] = parsed.username
        if parsed.password:
            entry["password"] = parsed.password
        return entry
    except Exception:
        return None


def _get_proxy_list() -> list[dict]:
    """Читает прокси: PROXY_LIST и/или PROXY_LIST_FILE."""
    out: list[dict] = []
    seen: set[str] = set()

    # Базовый endpoint (например Decodo): один адрес + логин/пароль, ротация на стороне провайдера.
    # Пример:
    #   DECODO_PROXY_HOST=de.decodo.com
    #   DECODO_PROXY_PORT=20000
    #   DECODO_PROXY_USER=login
    #   DECODO_PROXY_PASSWORD=pass
    #   DECODO_PROXY_SCHEME=socks5
    base_host = os.getenv("DECODO_PROXY_HOST", "").strip()
    base_port = os.getenv("DECODO_PROXY_PORT", "").strip()
    base_user = os.getenv("DECODO_PROXY_USER", "").strip()
    base_pass = os.getenv("DECODO_PROXY_PASSWORD", "").strip()
    base_scheme = os.getenv("DECODO_PROXY_SCHEME", "socks5").strip().lower() or "socks5"
    if base_host and base_port.isdigit() and base_user and base_pass:
        if base_scheme not in ("socks5", "http", "https", "socks4"):
            base_scheme = "socks5"
        base_proxy = {
            "server": f"{base_scheme}://{base_host}:{base_port}",
            "username": base_user,
            "password": base_pass,
        }
        if base_proxy["server"] not in seen:
            # _raw используется в статистике/логах как стабильный ключ.
            base_proxy["_raw"] = f"{base_host}:{base_port}:{base_user}:{base_pass}"
            seen.add(base_proxy["server"])
            out.append(base_proxy)

    # Режим "только базовый endpoint" — игнорируем PROXY_LIST/PROXY_LIST_FILE и free-proxy источники.
    decodo_only = os.getenv("DECODO_PROXY_ONLY", "false").strip().lower() in ("1", "true", "yes")
    if decodo_only and out:
        return out

    # Из env (через запятую или перенос строки)
    raw = os.getenv("PROXY_LIST", "").strip()
    if raw:
        for part in raw.replace("\r", "").split(","):
            part = part.strip()
            if not part:
                continue
            for line in part.split("\n"):
                line = line.strip()
                if not line:
                    continue
                p = _parse_one_proxy(line)
                if p and p["server"] not in seen:
                    # Сохраняем исходную строку, чтобы можно было вести статистику по прокси.
                    p["_raw"] = line
                    seen.add(p["server"])
                    out.append(p)

    # Из файла (путь относительно текущей рабочей директории или абсолютный)
    file_path = os.getenv("PROXY_LIST_FILE", "").strip() or "proxies.txt"
    if file_path and os.path.isfile(file_path):
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    raw_line = line.strip()
                    if not raw_line:
                        continue
                    p = _parse_one_proxy(raw_line)
                    if p and p["server"] not in seen:
                        p["_raw"] = raw_line
                        seen.add(p["server"])
                        out.append(p)
        except Exception:
            pass

    return out


def _parse_proxy_line(line: str, default_scheme: str = "http") -> dict | None:
    """Из строки ip:port или url делает dict для Playwright."""
    line = line.strip()
    if not line or ":" not in line or line.startswith("#"):
        return None
    if line.startswith("http://") or line.startswith("socks5://") or line.startswith("socks4://"):
        try:
            parsed = urlparse(line)
            port = parsed.port or (1080 if "socks" in (parsed.scheme or "") else 80)
            server = f"{parsed.scheme}://{parsed.hostname}:{port}"
            return {"server": server}
        except Exception:
            return None
    parts = line.rsplit(":", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    return {"server": f"{default_scheme}://{line}"}


def _check_proxy_alive(proxy_dict: dict, url: str = MAX_WEB_URL, timeout: int = PROXY_CHECK_TIMEOUT) -> bool:
    """Проверяет, что через прокси можно открыть url. Синхронная, вызывать из to_thread."""
    server = proxy_dict.get("server")
    if not server:
        return False
    username = proxy_dict.get("username")
    password = proxy_dict.get("password")
    if username and password:
        part = quote(username, safe="") + ":" + quote(password, safe="")
        proxy_url = server.replace("://", f"://{part}@", 1)
    else:
        proxy_url = server
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        r = requests.get(url, proxies=proxies, timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


async def _ensure_api_health(proxy_list: list[dict]) -> None:
    """
    Проверяет, какие прокси годятся для API (https://api.oneme.ru).
    Результат кэшируется в _proxy_api_health_cache, ключ — upstream host:port без схемы и логина.
    """
    global _proxy_api_health_cache, _proxy_api_health_checked_at
    now = time.monotonic()
    # Если кэш свежий и не пустой — переиспользуем.
    if _proxy_api_health_cache and now - _proxy_api_health_checked_at < PROXY_API_HEALTH_TTL:
        return

    _proxy_api_health_cache = {}
    sem = asyncio.Semaphore(PROXY_CHECK_MAX_PARALLEL)

    async def check_one(p: dict) -> None:
        server = (p.get("server") or "").replace("socks5://", "").split("@")[-1]
        if not server:
            return
        async with sem:
            ok = await asyncio.to_thread(_check_proxy_alive, p, API_TEST_URL, PROXY_CHECK_TIMEOUT)
            _proxy_api_health_cache[server] = ok

    tasks = [asyncio.create_task(check_one(p)) for p in proxy_list]
    await asyncio.gather(*tasks, return_exceptions=True)
    _proxy_api_health_checked_at = time.monotonic()


async def _filter_alive_proxies(
    proxy_list: list[dict],
    timeout: int = PROXY_CHECK_TIMEOUT,
    max_alive: int = PROXY_CHECK_MAX_ALIVE,
    max_parallel: int = PROXY_CHECK_MAX_PARALLEL,
) -> list[dict]:
    """Параллельно проверяет прокси, возвращает только живые (до max_alive штук)."""
    if not proxy_list:
        return []
    # Не проверяем весь список — только первые N, иначе зависаем надолго
    to_check = proxy_list[:PROXY_CHECK_MAX_TO_SCAN]
    sem = asyncio.Semaphore(max_parallel)
    alive: list[dict] = []

    async def check_one(p: dict) -> dict | None:
        async with sem:
            ok = await asyncio.to_thread(_check_proxy_alive, p, MAX_WEB_URL, timeout)
            return p if ok else None

    tasks = [asyncio.create_task(check_one(p)) for p in to_check]
    try:
        for f in asyncio.as_completed(tasks):
            try:
                result = await f
                if result is not None:
                    alive.append(result)
                    if len(alive) >= max_alive:
                        break
            except asyncio.CancelledError:
                pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return alive


def _proxy_stats_key(proxy: dict) -> str:
    """Ключ прокси для статистики/лимитов."""
    return (
        proxy.get("_raw")
        or (proxy.get("server") or "").replace("socks5://", "").split("@")[-1]
        or ""
    )


async def _filter_by_use_limit(proxy_list: list[dict]) -> list[dict]:
    """
    Оставляет только прокси с use_count < PROXY_MAX_USE_COUNT.
    Лимит можно переопределить через PROXY_MAX_USE_COUNT в .env.
    """
    raw_limit = os.getenv("PROXY_MAX_USE_COUNT", str(PROXY_MAX_USE_COUNT)).strip()
    try:
        use_limit = int(raw_limit)
    except ValueError:
        use_limit = PROXY_MAX_USE_COUNT
    if use_limit <= 0:
        return proxy_list
    if not proxy_list or not _PROXY_STATS_AVAILABLE or get_proxy_stats is None:
        return proxy_list
    try:
        stats = await get_proxy_stats()
        out: list[dict] = []
        for p in proxy_list:
            key = _proxy_stats_key(p)
            use_count = int((stats.get(key) or {}).get("use_count") or 0)
            if use_count < use_limit:
                out.append(p)
        return out
    except Exception:
        return proxy_list


async def _fetch_free_eu_proxies() -> list[dict]:
    """
    Собирает бесплатные прокси DE/FI из трёх источников:
    - ProxyScrape API
    - Proxy-list.download API
    - freeproxy.world (txt)
    Кэш 20 мин.
    """
    global _free_eu_proxy_cache
    now = time.monotonic()
    if _free_eu_proxy_cache is not None:
        cached_list, cached_at = _free_eu_proxy_cache
        if now - cached_at < FREE_EU_CACHE_TTL and cached_list:
            return cached_list

    countries = (os.getenv("FREE_EU_COUNTRIES") or DEFAULT_FREE_EU_COUNTRIES).replace(" ", "").split(",")
    seen: set[str] = set()
    all_proxies: list[dict] = []

    # 1) ProxyScrape
    for country in countries:
        url = (
            "https://api.proxyscrape.com/v2/"
            "?request=displayproxies&protocol=http&timeout=8000&country=" + country.strip()
        )
        try:
            resp = await asyncio.to_thread(requests.get, url, timeout=15)
            resp.raise_for_status()
            for line in resp.text.strip().splitlines():
                p = _parse_proxy_line(line, "http")
                if p and p["server"] not in seen:
                    seen.add(p["server"])
                    all_proxies.append(p)
        except Exception:
            pass

    # 2) Proxy-list.download
    for country in countries:
        for ptype in ("http", "https"):
            url = f"https://www.proxy-list.download/api/v1/get?type={ptype}&country={country.strip()}"
            try:
                resp = await asyncio.to_thread(requests.get, url, timeout=15)
                resp.raise_for_status()
                for line in resp.text.strip().splitlines():
                    p = _parse_proxy_line(line, ptype)
                    if p and p["server"] not in seen:
                        seen.add(p["server"])
                        all_proxies.append(p)
            except Exception:
                pass

    # 3) freeproxy.world (txt)
    for country in countries:
        url = f"https://www.freeproxy.world/?format=txt&country={country.strip()}"
        try:
            resp = await asyncio.to_thread(requests.get, url, timeout=15)
            resp.raise_for_status()
            for line in resp.text.strip().splitlines():
                p = _parse_proxy_line(line, "http")
                if p and p["server"] not in seen:
                    seen.add(p["server"])
                    all_proxies.append(p)
        except Exception:
            pass

    random.shuffle(all_proxies)

    # Проверка живых прокси (можно отключить: PROXY_CHECK_ALIVE=false)
    if all_proxies and os.getenv("PROXY_CHECK_ALIVE", "true").strip().lower() not in ("0", "false", "no"):
        try:
            alive_list = await asyncio.wait_for(
                _filter_alive_proxies(all_proxies),
                timeout=FETCH_AND_FILTER_PROXIES_TIMEOUT,
            )
            if alive_list:
                all_proxies = alive_list
        except asyncio.TimeoutError:
            pass  # по таймауту оставляем нефильтрованный список
        # если ни один не живой — оставляем нефильтрованный список, fallback без прокси сработает в run_max_qr_flow

    _free_eu_proxy_cache = (all_proxies, now)
    return all_proxies


MAX_PROXY_ATTEMPTS = 4  # legacy; сейчас пробуем все из списка
PROXY_TIMEOUT = 10
PROXY_EXHAUSTED_MSG = "Все прокси из proxies.txt не подключились за 10 сек. Отправьте команду снова или проверьте список."

IP_CHECK_URL = "https://api.ipify.org"


async def get_proxy_for_request_async(
    tried_servers: set | None = None,
) -> tuple[dict | None, object, str]:
    """
    Возвращает (proxy_dict, cleanup, upstream_key) для MaxClient.connect(proxy).
    proxy_dict в формате {"server": "http://host:port"} — для HTTP CONNECT.
    SOCKS5 с авторизацией — через мост, cleanup() останавливает мост.
    tried_servers — адреса уже испробованных прокси (host:port), их пропускаем.
    upstream_key — строка для добавления в tried_servers при таймауте.
    """
    tried_servers = tried_servers or set()
    proxy_list = _get_proxy_list()
    proxy_list = await _filter_by_use_limit(proxy_list)
    # Сортируем прокси по количеству использований (если есть статистика), чтобы равномернее распределять нагрузку.
    if _PROXY_STATS_AVAILABLE and get_proxy_stats is not None and proxy_list:
        try:
            stats = await get_proxy_stats()

            def usage_key(p: dict) -> int:
                raw = _proxy_stats_key(p)
                s = stats.get(raw) or {}
                return int(s.get("use_count") or 0)

            proxy_list.sort(key=usage_key)
        except Exception:
            # При ошибке статистики просто продолжаем с исходным порядком.
            pass
    if not proxy_list:
        if os.getenv("USE_FREE_EU_PROXIES", "true").strip().lower() not in ("0", "false", "no"):
            proxy_list = await _fetch_free_eu_proxies()
            proxy_list = await _filter_by_use_limit(proxy_list)
    if not proxy_list:
        return (None, None, "")
    # Перед использованием для API проверяем, какие прокси реально умеют ходить к api.oneme.ru по HTTPS.
    await _ensure_api_health(proxy_list)
    available = [p for p in proxy_list if (p.get("server") or "").replace("socks5://", "").split("@")[-1] not in tried_servers]
    if not available:
        return (None, None, "")
    # Сначала берём только те, у кого API OK; если таких нет — падаем обратно на весь список.
    def upstream_key_for(p: dict) -> str:
        return (p.get("server") or "").replace("socks5://", "").split("@")[-1]

    api_ok_list = [p for p in available if _proxy_api_health_cache.get(upstream_key_for(p), True)]
    pool = api_ok_list or available
    proxy = random.choice(pool[: min(12, len(pool))])
    upstream_key = upstream_key_for(proxy)
    if (
        proxy
        and (proxy.get("server") or "").lower().startswith("socks5")
        and proxy.get("username")
        and proxy.get("password")
    ):
        if start_socks5_bridge:
            try:
                port, bridge_thread = start_socks5_bridge(proxy)
                server = (proxy.get("server") or "").replace("socks5://", "").split("@")[-1]
                actual = {
                    "server": f"http://127.0.0.1:{port}",
                    "_upstream": server,
                    "_socks5_original": proxy,
                }
                await asyncio.sleep(0.3)

                # Фиксируем использование исходного прокси.
                if _PROXY_STATS_AVAILABLE and log_proxy_usage is not None:
                    raw = proxy.get("_socks5_original", {}).get("_raw") or proxy.get("_raw") or server
                    try:
                        await log_proxy_usage(raw)
                    except Exception:
                        pass

                def cleanup():
                    if stop_socks5_bridge:
                        stop_socks5_bridge(bridge_thread)

                return (actual, cleanup, upstream_key if upstream_key else server)
            except Exception:
                pass
        return (None, None, upstream_key)
    if proxy and not (proxy.get("server") or "").lower().startswith("socks5"):
        # HTTP/HTTPS прокси: считаем использование.
        if _PROXY_STATS_AVAILABLE and log_proxy_usage is not None:
            raw = proxy.get("_raw") or upstream_key
            try:
                await log_proxy_usage(raw)
            except Exception:
                pass
        return (proxy, None, upstream_key)
    return (None, None, "")


async def check_proxy_ip() -> tuple[str, bool, str | None]:
    """
    Запускает браузер с той же конфигурацией прокси, что и /qr.
    Возвращает (ip, использовался_прокси, описание_прокси_или_None).
    При исчерпании прокси — (?, False, PROXY_EXHAUSTED_MSG).
    """
    async with async_playwright() as p:
        proxy_list = _get_proxy_list()
        proxy_list = await _filter_by_use_limit(proxy_list)
        if not proxy_list:
            if os.getenv("USE_FREE_EU_PROXIES", "true").strip().lower() not in ("0", "false", "no"):
                proxy_list = await _fetch_free_eu_proxies()
                proxy_list = await _filter_by_use_limit(proxy_list)
        attempts: list[dict | None] = list(proxy_list) if proxy_list else []
        attempts.append(None)

        for proxy in attempts:
            bridge_thread = None
            proxy_desc: str | None = None
            actual_proxy = proxy
            if (
                proxy
                and (proxy.get("server") or "").lower().startswith("socks5")
                and proxy.get("username")
                and proxy.get("password")
            ):
                if start_socks5_bridge:
                    try:
                        port, bridge_thread = start_socks5_bridge(proxy)
                        actual_proxy = {
                            "server": f"http://127.0.0.1:{port}",
                            "_upstream": (proxy.get("server") or "").replace("socks5://", "").split("@")[-1],
                        }
                        proxy_desc = f"socks5->127.0.0.1:{port}"
                        await asyncio.sleep(0.4)
                    except Exception:
                        actual_proxy = None
                        proxy_desc = None
                else:
                    actual_proxy = None
            elif proxy:
                proxy_desc = (proxy.get("server") or "?").replace("socks5://", "").split("@")[-1]
            if actual_proxy and (actual_proxy.get("server") or "").lower().startswith("socks5") and (actual_proxy.get("username") or ""):
                actual_proxy = None
            browser = None
            try:
                browser = await p.firefox.launch(headless=True, proxy=actual_proxy)
                page = await browser.new_page(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                )
                await page.goto(IP_CHECK_URL, wait_until="domcontentloaded", timeout=PROXY_TIMEOUT * 1000)
                ip = (
                    (await page.evaluate("() => document.body?.innerText || ''")).strip()
                    or (await page.content()).strip()
                    or "?"
                )
                await browser.close()
                return (ip, proxy_desc is not None, proxy_desc)
            except Exception:
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                continue
            finally:
                if bridge_thread and stop_socks5_bridge:
                    stop_socks5_bridge(bridge_thread)

        return ("?", False, PROXY_EXHAUSTED_MSG)


def _decode_qr_from_bytes(image_bytes: bytes) -> str:
    """Декодирует QR из PNG/JPEG байтов. Требует pyzbar + Pillow."""
    try:
        from register_account import decode_qr_from_bytes
        return decode_qr_from_bytes(image_bytes)
    except ImportError:
        raise ImportError("Для флоу по номеру через Playwright установите: pip install pyzbar Pillow")


async def run_max_qr_flow_with_auth(
    auth_token: str,
    poll_interval: float = 2.0,
    timeout: float = 120.0,
    preferred_proxy: dict | None = None,
    on_waiting_session: Optional[Callable[[int], Awaitable[None]]] = None,
    password: Optional[str] = None,
    client_profile: dict | None = None,
) -> AsyncIterator[str | None]:
    """
    После авторизации по SMS: открывает web.max.ru в Playwright, достаёт ссылку из QR,
    авторизует её через opcode 290 с auth_token, ждёт появления сессии на странице.
    Если на аккаунте включён 2FA (password передан), после 290 на странице может появиться
    форма ввода пароля — заполняем её и отправляем, затем ждём блок.
    Отдаёт готовый блок для localStorage или None при таймауте.
    Если preferred_proxy задан — один и тот же прокси для браузера и API (цепочка по номеру).
    """
    from register_account import MaxClient

    async with async_playwright() as p:
        if preferred_proxy is not None:
            attempts = [preferred_proxy]
            proxy_list = []
        else:
            proxy_list = _get_proxy_list()
            proxy_list = await _filter_by_use_limit(proxy_list)
            if not proxy_list:
                if os.getenv("USE_FREE_EU_PROXIES", "true").strip().lower() not in ("0", "false", "no"):
                    proxy_list = await _fetch_free_eu_proxies()
                    proxy_list = await _filter_by_use_limit(proxy_list)
            attempts = []
            if proxy_list:
                attempts.extend(random.sample(proxy_list, len(proxy_list)))
            attempts.append(None)

        page = None
        browser = None
        last_error: Exception | None = None
        bridge_thread = None
        actual_proxy_used: dict | None = None

        for proxy in attempts:
            if browser:
                await browser.close()
            if bridge_thread and stop_socks5_bridge:
                stop_socks5_bridge(bridge_thread)
                bridge_thread = None
            actual_proxy = proxy
            if preferred_proxy is not None:
                actual_proxy = preferred_proxy
                actual_proxy_used = preferred_proxy
            elif (
                proxy
                and (proxy.get("server") or "").lower().startswith("socks5")
                and proxy.get("username")
                and proxy.get("password")
            ):
                if start_socks5_bridge:
                    try:
                        port, bridge_thread = start_socks5_bridge(proxy)
                        server = (proxy.get("server") or "").replace("socks5://", "").split("@")[-1]
                        actual_proxy = {
                            "server": f"http://127.0.0.1:{port}",
                            "_upstream": server,
                        }
                        await asyncio.sleep(0.4)
                    except Exception:
                        bridge_thread = None
                        last_error = RuntimeError("Не удалось запустить мост SOCKS5")
                        continue
                else:
                    actual_proxy = None
            if actual_proxy and (actual_proxy.get("server") or "").lower().startswith("socks5") and (actual_proxy.get("username") or ""):
                actual_proxy = None
            if preferred_proxy is None:
                actual_proxy_used = actual_proxy
            browser = await p.firefox.launch(headless=True, proxy=actual_proxy)
            try:
                page = await browser.new_page(
                    viewport={"width": 400, "height": 700},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                )
                await page.goto(MAX_WEB_URL, wait_until="domcontentloaded", timeout=10000)
                await _dismiss_max_bot_check_modal(page)
                break
            except Exception as e:
                last_error = e
                if preferred_proxy is not None:
                    raise
                continue

        if page is None:
            if bridge_thread and stop_socks5_bridge:
                stop_socks5_bridge(bridge_thread)
            if browser:
                await browser.close()
            err_msg = PROXY_EXHAUSTED_MSG if proxy_list else "Не удалось открыть страницу"
            raise RuntimeError(err_msg)

        try:
            # Пытаемся несколько раз считать QR, если он ещё не успел появиться.
            qr_link: str | None = None
            last_qr_error: Exception | None = None
            for _ in range(3):
                await _dismiss_max_bot_check_modal(page)
                await page.wait_for_timeout(2500)
                try:
                    canvas = await page.query_selector("canvas")
                    if not canvas:
                        qr_bytes = await page.screenshot(type="png")
                    else:
                        qr_bytes = await canvas.screenshot(type="png")
                    qr_link = _decode_qr_from_bytes(qr_bytes)
                    break
                except ValueError as e:
                    # QR ещё не прогрузился на странице — пробуем ещё раз.
                    last_qr_error = e
                    continue
            if qr_link is None:
                raise last_qr_error or ValueError("QR-код не найден на изображении страницы")

            client = MaxClient(ver=11, client_profile=client_profile)
            client.auth_token = auth_token
            use_proxy_for_api = os.getenv("USE_PROXY_FOR_API", "true").strip().lower() not in ("0", "false", "no")
            api_proxy = (preferred_proxy if preferred_proxy is not None else None) if use_proxy_for_api else None
            if api_proxy is not None:
                await client.connect(api_proxy)
                await client.handshake()
                await client.auth_login(full_init=False)
                await client.authorize_qr(qr_link)
            elif not use_proxy_for_api:
                await client.connect(None)
                await client.handshake()
                await client.auth_login(full_init=False)
                await client.authorize_qr(qr_link)
            else:
                api_tried: set[str] = set()
                api_connect_done = False
                while not api_connect_done:
                    api_proxy_loop, api_cleanup, api_key = await get_proxy_for_request_async(api_tried)
                    use_proxy = api_proxy_loop if api_proxy_loop is not None else actual_proxy_used
                    if use_proxy is None or (api_proxy_loop is None and api_tried):
                        raise RuntimeError(PROXY_EXHAUSTED_MSG)
                    try:
                        await client.connect(use_proxy)
                        await client.handshake()
                        await client.auth_login(full_init=False)
                        await client.authorize_qr(qr_link)
                        api_connect_done = True
                    except asyncio.TimeoutError:
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                        if api_cleanup:
                            api_cleanup()
                        key = api_key or (use_proxy.get("_upstream") or use_proxy.get("server") or "")
                        api_tried.add(key)
                        max_tries = len(proxy_list) if proxy_list else 1
                        if len(api_tried) >= max_tries:
                            raise RuntimeError(PROXY_EXHAUSTED_MSG)

            try:
                await client.disconnect()
            except Exception:
                pass

            await _dismiss_max_bot_check_modal(page)

            # Если на аккаунте 2FA, на веб-странице после 290 может появиться форма пароля — заполняем
            if password and page:
                await page.wait_for_timeout(2500)
                try:
                    pw_input = page.locator('input[type="password"]').first
                    await pw_input.wait_for(state="visible", timeout=6000)
                    await pw_input.fill(password)
                    submit = page.locator('button[type="submit"], input[type="submit"]').first
                    await submit.wait_for(state="visible", timeout=3000)
                    await submit.click()
                    await page.wait_for_timeout(3000)
                except Exception:
                    pass

            deadline = time.monotonic() + timeout
            last_notify = 0.0
            logging.getLogger(__name__).info("Ожидаю сессию на странице (localStorage), таймаут %d с", int(timeout))
            while time.monotonic() < deadline:
                block = await page.evaluate(GET_BLOCK_SCRIPT)
                if block and isinstance(block, str):
                    # Успешный токен через этот прокси: увеличиваем счётчик token_count.
                    if _PROXY_STATS_AVAILABLE and log_proxy_usage is not None and preferred_proxy is not None:
                        raw = preferred_proxy.get("_raw") or (preferred_proxy.get("_upstream") or "")
                        if raw:
                            try:
                                await log_proxy_usage(raw, tokens_delta=1)
                            except Exception:
                                pass
                    yield block
                    return
                elapsed = int(time.monotonic() - (deadline - timeout))
                if on_waiting_session and elapsed - last_notify >= 15:
                    last_notify = elapsed
                    try:
                        await on_waiting_session(elapsed)
                    except Exception:
                        pass
                await asyncio.sleep(poll_interval)
            logging.getLogger(__name__).warning("Таймаут: сессия на странице не появилась за %d с", int(timeout))
            yield None
        finally:
            if bridge_thread and stop_socks5_bridge:
                stop_socks5_bridge(bridge_thread)
            await browser.close()


async def run_max_qr_flow(
    poll_interval: float = 2.0,
    timeout: float = 300.0,
) -> AsyncIterator[bytes | str | None]:
    """
    Запускает браузер, открывает web.max.ru.
    Сначала отдаёт bytes — PNG скриншот страницы с QR.
    После сканирования отдаёт str — готовый блок для вставки в консоль.
    При таймауте отдаёт None.
    При ошибке прокси — перебирает другие, затем пробует без прокси.
    """
    async with async_playwright() as p:
        proxy_list = _get_proxy_list()
        proxy_list = await _filter_by_use_limit(proxy_list)
        if not proxy_list:
            if os.getenv("USE_FREE_EU_PROXIES", "true").strip().lower() not in ("0", "false", "no"):
                proxy_list = await _fetch_free_eu_proxies()
                proxy_list = await _filter_by_use_limit(proxy_list)
        # Очередь попыток: все прокси (в случайном порядке), затем без прокси
        attempts: list[dict | None] = []
        if proxy_list:
            attempts.extend(random.sample(proxy_list, len(proxy_list)))
        attempts.append(None)  # fallback без прокси

        page = None
        browser = None
        last_error: Exception | None = None
        bridge_thread = None
        used_proxy_source: dict | None = None

        for proxy in attempts:
            if browser:
                await browser.close()
            if bridge_thread and stop_socks5_bridge:
                stop_socks5_bridge(bridge_thread)
                bridge_thread = None
            actual_proxy = proxy
            # Playwright не поддерживает SOCKS5 с авторизацией — только через локальный HTTP-мост
            if (
                proxy
                and (proxy.get("server") or "").lower().startswith("socks5")
                and proxy.get("username")
                and proxy.get("password")
            ):
                if start_socks5_bridge:
                    try:
                        port, bridge_thread = start_socks5_bridge(proxy)
                        actual_proxy = {"server": f"http://127.0.0.1:{port}"}
                        await asyncio.sleep(0.4)
                    except Exception:
                        bridge_thread = None
                        last_error = RuntimeError("Не удалось запустить мост SOCKS5")
                        continue
                else:
                    # мост недоступен (не установлен PySocks?) — не передаём SOCKS5 в браузер
                    actual_proxy = None
            # На всякий случай: браузер не умеет SOCKS5+auth, не передаём такой прокси
            if actual_proxy and (actual_proxy.get("server") or "").lower().startswith("socks5") and actual_proxy.get("username"):
                actual_proxy = None
            browser = await p.firefox.launch(headless=True, proxy=actual_proxy)
            try:
                page = await browser.new_page(
                    viewport={"width": 400, "height": 700},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                )
                await page.goto(MAX_WEB_URL, wait_until="domcontentloaded", timeout=10000)
                await _dismiss_max_bot_check_modal(page)
                used_proxy_source = proxy
                break
            except Exception as e:
                last_error = e
                continue

        if page is None:
            if bridge_thread and stop_socks5_bridge:
                stop_socks5_bridge(bridge_thread)
            if browser:
                await browser.close()
            err_msg = PROXY_EXHAUSTED_MSG if proxy_list else "Не удалось открыть страницу"
            raise RuntimeError(err_msg)

        try:
            await _dismiss_max_bot_check_modal(page)
            await page.wait_for_timeout(2500)

            qr_bytes: bytes
            try:
                canvas = await page.query_selector("canvas")
                if canvas:
                    qr_bytes = await canvas.screenshot(type="png")
                else:
                    qr_bytes = await page.screenshot(type="png")
            except Exception:
                qr_bytes = await page.screenshot(type="png")

            yield qr_bytes

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                block = await page.evaluate(GET_BLOCK_SCRIPT)
                if block and isinstance(block, str):
                    # Для статистики: если браузер шёл через прокси, увеличиваем token_count.
                    if _PROXY_STATS_AVAILABLE and log_proxy_usage is not None and used_proxy_source is not None:
                        raw = used_proxy_source.get("_raw") or used_proxy_source.get("server") or ""
                        if raw:
                            try:
                                await log_proxy_usage(raw, tokens_delta=1)
                            except Exception:
                                pass
                    yield block
                    return
                await asyncio.sleep(poll_interval)
            yield None
        finally:
            if bridge_thread and stop_socks5_bridge:
                stop_socks5_bridge(bridge_thread)
            await browser.close()
