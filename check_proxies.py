"""
Проверка прокси из proxies.txt — те же точки, что и в коде.

Откуда в коде коннектятся:
  1) web.max.ru (HTTPS)
     — browser_max.py: Playwright открывает MAX_WEB_URL = "https://web.max.ru"
     — Прокси: SOCKS5 -> мост socks5_bridge -> браузер коннектится по HTTP CONNECT к любому хосту
     — По сути: через SOCKS5 делается GET https://web.max.ru

  2) api.oneme.ru:443 (TLS)
     — register_account.py: MaxClient.connect(proxy): TCP к прокси -> CONNECT api.oneme.ru:443 ->
       ответ 200 -> asyncio.start_tls() (TLS-рукопожатие с api.oneme.ru). Таймаут PROXY_CONNECT_TIMEOUT = 10 сек.
     — Здесь бывает "застревание": прокси отдаёт 200 на CONNECT, но TLS-рукопожатие тянется или обрывается.

Проверяем оба хоста через тот же SOCKS5 (GET https://web.max.ru и GET https://api.oneme.ru).
Если WEB ок, а API таймаут — это и есть случай "застряло на TLS".
"""
import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from urllib.parse import urlparse

import requests

PROXIES_FILE = Path(__file__).with_name("proxies.txt")
# Точки из кода: браузер и API-клиент
URL_WEB = "https://web.max.ru"   # browser_max.MAX_WEB_URL
URL_API = "https://api.oneme.ru" # register_account.HOST
TIMEOUT = 10


@dataclass
class ProxyEntry:
    original_line: str
    label: str
    scheme: str
    host: str
    port: int
    username: str
    password: str


@dataclass
class EndpointResult:
    ok: bool
    time_sec: Optional[float] = None
    error: Optional[str] = None


@dataclass
class ProxyCheckResult:
    entry: ProxyEntry
    web: EndpointResult = field(default_factory=lambda: EndpointResult(ok=False))
    api: EndpointResult = field(default_factory=lambda: EndpointResult(ok=False))


def _parse_proxy_line(raw: str) -> Optional[ProxyEntry]:
    """Парсит host:port:user:pass и URL-форматы (http://user:pass@ip:port)."""
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    if "://" in raw:
        try:
            parsed = urlparse(raw)
            if not parsed.hostname:
                return None
            scheme = (parsed.scheme or "http").lower()
            port = parsed.port or (1080 if "socks" in scheme else 80)
            username = parsed.username or ""
            password = parsed.password or ""
            return ProxyEntry(
                original_line=raw,
                label=f"{scheme}://{parsed.hostname}:{port}",
                scheme=scheme,
                host=parsed.hostname,
                port=port,
                username=username,
                password=password,
            )
        except Exception:
            return None
    parts = raw.split(":", 3)
    if len(parts) != 4:
        return None
    host, port_str, username, password = parts[0], parts[1], parts[2], parts[3]
    try:
        port = int(port_str)
    except ValueError:
        return None
    return ProxyEntry(
        original_line=raw,
        label=f"socks5://{host}:{port}",
        scheme="socks5",
        host=host,
        port=port,
        username=username,
        password=password,
    )


def load_proxies() -> list[ProxyEntry]:
    entries: list[ProxyEntry] = []
    if not PROXIES_FILE.exists():
        raise SystemExit(f"Файл {PROXIES_FILE} не найден.")
    with PROXIES_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            entry = _parse_proxy_line(line)
            if entry:
                entries.append(entry)
    return entries


def _check_one_url(entry: ProxyEntry, url: str) -> EndpointResult:
    """Один запрос через SOCKS5 (GET url)."""
    auth_part = ""
    if entry.username:
        auth_part = quote(entry.username, safe="")
        if entry.password:
            auth_part += ":" + quote(entry.password, safe="")
        auth_part += "@"
    proxy_url = f"{entry.scheme}://{auth_part}{entry.host}:{entry.port}"
    proxies = {"http": proxy_url, "https": proxy_url}
    t0 = time.perf_counter()
    try:
        r = requests.get(url, proxies=proxies, timeout=TIMEOUT)
        dt = time.perf_counter() - t0
        ok = r.status_code < 500
        return EndpointResult(ok=ok, time_sec=dt if ok else None, error=None if ok else f"HTTP {r.status_code}")
    except Exception as e:
        return EndpointResult(ok=False, time_sec=None, error=str(e))


def _check_one_sync(entry: ProxyEntry) -> ProxyCheckResult:
    """Проверка WEB и API для одного прокси (как в коде: браузер + API TLS)."""
    web = _check_one_url(entry, URL_WEB)
    api = _check_one_url(entry, URL_API)
    return ProxyCheckResult(entry=entry, web=web, api=api)


async def probe_proxy(entry: ProxyEntry) -> ProxyCheckResult:
    return await asyncio.to_thread(_check_one_sync, entry)


async def main() -> None:
    print(f"Читаю {PROXIES_FILE}...")
    entries = load_proxies()
    if not entries:
        print("Список пустой или формат не host:port:user:pass.")
        return

    print("Точки из кода:")
    print(f"  WEB: {URL_WEB} (браузер, browser_max)")
    print(f"  API: {URL_API} (MaxClient.connect -> CONNECT + TLS, register_account)")
    print(f"Прокси: {len(entries)} шт., таймаут {TIMEOUT} с. Проверяю все параллельно...")

    tasks = [asyncio.create_task(probe_proxy(e)) for e in entries]
    results: list[ProxyCheckResult] = await asyncio.gather(*tasks)

    both_ok = [r for r in results if r.web.ok and r.api.ok]
    web_only = [r for r in results if r.web.ok and not r.api.ok]
    api_only = [r for r in results if not r.web.ok and r.api.ok]
    both_bad = [r for r in results if not r.web.ok and not r.api.ok]

    print()
    print("Результаты (WEB = web.max.ru, API = api.oneme.ru):")
    print()
    for r in sorted(both_ok, key=lambda x: (x.web.time_sec or 0) + (x.api.time_sec or 0)):
        w = f"{r.web.time_sec:.2f}s" if r.web.time_sec else "-"
        a = f"{r.api.time_sec:.2f}s" if r.api.time_sec else "-"
        print(f"  [OK]   {r.entry.label}  WEB={w}  API={a}")
    for r in web_only:
        w = f"{r.web.time_sec:.2f}s" if r.web.time_sec else "-"
        err = (r.api.error or "")[:50]
        print(f"  [WEB]  {r.entry.label}  WEB={w}  API=BAD ({err})  <- возможно застревание на TLS")
    for r in api_only:
        a = f"{r.api.time_sec:.2f}s" if r.api.time_sec else "-"
        err = (r.web.error or "")[:50]
        print(f"  [API]  {r.entry.label}  WEB=BAD ({err})  API={a}")
    for r in both_bad:
        w = (r.web.error or "")[:35]
        a = (r.api.error or "")[:35]
        print(f"  [BAD]  {r.entry.label}  WEB=({w})  API=({a})")

    print()
    print("Итого:")
    print(f"  WEB+API ок:    {len(both_ok)}  (норма для бота)")
    print(f"  только WEB:   {len(web_only)}  (QR откроется, коннект к API может зависнуть на TLS)")
    print(f"  только API:   {len(api_only)}")
    print(f"  оба BAD:      {len(both_bad)}")

    alive = both_ok
    if not alive and web_only:
        alive = web_only
    if not alive:
        print("\nФайл proxies.txt не изменён.")
        return

    backup_path = PROXIES_FILE.with_suffix(".txt.bak")
    PROXIES_FILE.replace(backup_path)
    print(f"\nОригинал сохранён как {backup_path.name}")

    def sort_key(r: ProxyCheckResult) -> float:
        tw = r.web.time_sec or 999.0
        ta = r.api.time_sec or 999.0
        return tw + ta

    with PROXIES_FILE.open("w", encoding="utf-8") as f:
        for r in sorted(alive, key=sort_key):
            f.write(r.entry.original_line + "\n")

    print(f"В {PROXIES_FILE.name} записано {len(alive)} прокси (приоритет: WEB+API ок, затем по скорости).")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОтмена.")
