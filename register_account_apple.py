"""
MAX messenger — регистрация аккаунта, установка пароля, авторизация веб-версии по QR.
Использование: см. python register_account.py --help
"""

import asyncio
import base64
import json
import os
import ssl
import struct
import time
import uuid
import random
import sys
import argparse
import getpass
from typing import Dict, Any, Optional, Tuple, Callable, Awaitable
from urllib.parse import urlparse

try:
    import msgpack
except ImportError:
    raise ImportError("Установите зависимость: pip install msgpack")

try:
    from pyzbar.pyzbar import decode as pyzbar_decode
    from PIL import Image as PILImage

    _QR_AVAILABLE = True
except ImportError:
    _QR_AVAILABLE = False

try:
    import websockets

    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

try:
    import qrcode

    _QRCODE_PRINT_AVAILABLE = True
except ImportError:
    qrcode = None  # type: ignore[assignment]
    _QRCODE_PRINT_AVAILABLE = False

try:
    import socks as py_socks
    _SOCKS_AVAILABLE = True
except ImportError:
    py_socks = None
    _SOCKS_AVAILABLE = False

HOST = "api.oneme.ru"
PORT = 443
# Таймауты: отдельно на TLS, чтобы не висеть долго на одном прокси.
PROXY_CONNECT_TIMEOUT = 20  # общий таймаут на connect+CONNECT+TLS
TLS_HANDSHAKE_TIMEOUT = 12  # только TLS-рукопожатие через туннель (медленные прокси)

# Если False — к api.oneme.ru подключаемся напрямую (без прокси).
USE_PROXY_FOR_API = os.environ.get("USE_PROXY_FOR_API", "true").strip().lower() not in ("0", "false", "no")

WEBSOCKET_URI = "wss://ws-api.oneme.ru/websocket"
WEBSOCKET_ORIGIN = "https://web.max.ru"
OPCODE_SESSION_INIT = 6
OPCODE_GET_QR = 288
OPCODE_GET_QR_STATUS = 289
OPCODE_LOGIN_BY_QR = 291
OPCODE_AUTH_CHECK_PASSWORD = 115

DEFAULT_PASSWORD = "qwerty123)"
DEFAULT_HINT = "qw13)"

PROTO_VER = 10

# Таймаут ожидания ответа API (сек). При медленном прокси/сети увеличьте в .env: API_RESPONSE_TIMEOUT=60
API_RESPONSE_TIMEOUT = float(os.environ.get("API_RESPONSE_TIMEOUT", "50"))

# Устройства для handshake (deviceName, osVersion, screen, arch). IMEI в API не передаётся — только deviceId (random) и userAgent.
# Для iOS сервер ожидает screen в формате "1170x2532" (без dpi/xxhdpi), и pushDeviceType="APNS".
IOS_MODELS = [
    # iPhone 7—8 / SE (классическое разрешение)
    {"deviceName": "iPhone 7", "screen": "750x1334"},
    {"deviceName": "iPhone 8", "screen": "750x1334"},
    {"deviceName": "iPhone SE (2nd generation)", "screen": "750x1334"},
    {"deviceName": "iPhone SE (3rd generation)", "screen": "750x1334"},
    # iPhone X/XS/11 Pro (5.8")
    {"deviceName": "iPhone X", "screen": "1125x2436"},
    {"deviceName": "iPhone XS", "screen": "1125x2436"},
    {"deviceName": "iPhone 11 Pro", "screen": "1125x2436"},
    # XR/11 (6.1" LCD)
    {"deviceName": "iPhone XR", "screen": "828x1792"},
    {"deviceName": "iPhone 11", "screen": "828x1792"},
    # XS Max / 11 Pro Max
    {"deviceName": "iPhone XS Max", "screen": "1242x2688"},
    {"deviceName": "iPhone 11 Pro Max", "screen": "1242x2688"},
    # 12/13 mini
    {"deviceName": "iPhone 12 mini", "screen": "1080x2340"},
    {"deviceName": "iPhone 13 mini", "screen": "1080x2340"},
    # 12/13/14 (6.1")
    {"deviceName": "iPhone 12", "screen": "1170x2532"},
    {"deviceName": "iPhone 12 Pro", "screen": "1170x2532"},
    {"deviceName": "iPhone 13", "screen": "1170x2532"},
    {"deviceName": "iPhone 13 Pro", "screen": "1170x2532"},
    {"deviceName": "iPhone 14", "screen": "1170x2532"},
    # 12/13/14 Plus/Pro Max (6.7")
    {"deviceName": "iPhone 12 Pro Max", "screen": "1284x2778"},
    {"deviceName": "iPhone 13 Pro Max", "screen": "1284x2778"},
    {"deviceName": "iPhone 14 Plus", "screen": "1284x2778"},
    # 14 Pro / 14 Pro Max
    {"deviceName": "iPhone 14 Pro", "screen": "1179x2556"},
    {"deviceName": "iPhone 14 Pro Max", "screen": "1290x2796"},
    # 15 / 15 Plus / 15 Pro / 15 Pro Max
    {"deviceName": "iPhone 15", "screen": "1179x2556"},
    {"deviceName": "iPhone 15 Plus", "screen": "1290x2796"},
    {"deviceName": "iPhone 15 Pro", "screen": "1179x2556"},
    {"deviceName": "iPhone 15 Pro Max", "screen": "1290x2796"},
    # 16 линейка (разрешения близки к 15/15 Pro; серверу обычно достаточно правдоподобия)
    {"deviceName": "iPhone 16", "screen": "1179x2556"},
    {"deviceName": "iPhone 16 Plus", "screen": "1290x2796"},
    {"deviceName": "iPhone 16 Pro", "screen": "1206x2622"},
    {"deviceName": "iPhone 16 Pro Max", "screen": "1320x2868"},
    # 17e (как просили; используем правдоподобные значения 6.1")
    {"deviceName": "iPhone 17e", "screen": "1179x2556"},
]

IOS_OS_VERSIONS = [
    "15.7.9",
    "16.6",
    "16.7.5",
    "17.2.1",
    "17.4.1",
    "17.6.2",
]

IOS_APP_VERSIONS = [
    "26.6.1",
    "26.7.1",
    "26.8.0",
    "26.8.1",
]

IOS_DEVICE_ARCH = "arm64"

def generate_device_id() -> str:
    return "".join(f"{random.randint(0, 255):02x}" for _ in range(8))


def pack_packet(ver: int, cmd: int, seq: int, opcode: int, payload: dict) -> bytes:
    payload_bytes = msgpack.packb(payload, use_bin_type=True)
    payload_len = len(payload_bytes) & 0x00FFFFFF
    header = struct.pack(">BHBHI", ver, cmd, seq, opcode, payload_len)
    return header + payload_bytes


def lz4_decompress_block(src: bytes, max_output: int = 5 * 1024 * 1024) -> bytes:
    dst = bytearray()
    pos = 0

    while pos < len(src):
        token = src[pos]
        pos += 1

        lit_len = token >> 4
        if lit_len == 15:
            while pos < len(src):
                b = src[pos]
                pos += 1
                lit_len += b
                if b != 255:
                    break

        if lit_len > 0:
            if pos + lit_len > len(src):
                raise ValueError("LZ4: literal length out of bounds")
            dst.extend(src[pos : pos + lit_len])
            pos += lit_len
            if len(dst) > max_output:
                raise ValueError("LZ4: output too large")

        if pos >= len(src):
            break

        if pos + 1 >= len(src):
            raise ValueError("LZ4: incomplete offset")

        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2

        if offset == 0:
            raise ValueError("LZ4: zero offset")

        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 0x0F:
            while pos < len(src):
                b = src[pos]
                pos += 1
                match_len += b
                if b != 255:
                    break

        match_pos = len(dst) - offset
        if match_pos < 0:
            raise ValueError("LZ4: match out of bounds")

        for i in range(match_len):
            dst.append(dst[match_pos + (i % offset)])

        if len(dst) > max_output:
            raise ValueError("LZ4: output too large")

    return bytes(dst)


def decode_block_tokens(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): decode_block_tokens(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decode_block_tokens(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        try:
            decompressed = lz4_decompress_block(bytes(obj))
            nested = msgpack.unpackb(decompressed, raw=False)
            return decode_block_tokens(nested)
        except Exception:
            return obj
    return obj


def unpack_payload(payload_bytes: bytes) -> Any:
    if not payload_bytes:
        return None

    try:
        decompressed = lz4_decompress_block(payload_bytes)
        parsed = msgpack.unpackb(decompressed, raw=False)
        return decode_block_tokens(parsed)
    except Exception:
        pass

    try:
        parsed = msgpack.unpackb(payload_bytes, raw=False)
        return decode_block_tokens(parsed)
    except Exception:
        return None


def parse_packet(data: bytes) -> Optional[Dict[str, Any]]:
    if len(data) < 10:
        return None

    ver = data[0]
    cmd = struct.unpack_from(">H", data, 1)[0]
    seq = data[3]
    opcode = struct.unpack_from(">H", data, 4)[0]
    packed_len = struct.unpack_from(">I", data, 6)[0]
    payload_len = packed_len & 0x00FFFFFF

    if len(data) < 10 + payload_len:
        return None

    payload = unpack_payload(data[10 : 10 + payload_len])

    return {
        "ver": ver,
        "cmd": cmd,
        "seq": seq,
        "opcode": opcode,
        "payload": payload,
    }


class MaxClient:
    def __init__(self, ver: int = 11):
        self._ver = ver

        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._seq = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._buffer = bytearray()
        self._read_task: Optional[asyncio.Task] = None

        model = random.choice(IOS_MODELS)
        self.device = {
            "deviceName": model["deviceName"],
            "screen": model["screen"],
            "osVersion": random.choice(IOS_OS_VERSIONS),
            "arch": IOS_DEVICE_ARCH,
        }
        self.app_version = random.choice(IOS_APP_VERSIONS)
        self.device_id = generate_device_id()
        self.mt_instance_id = str(uuid.uuid4())
        self.client_session_id = random.randint(1, 100)

        self.auth_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self._push_waiters: Dict[int, asyncio.Future] = {}

    async def connect(self, proxy: Optional[Dict[str, str]] = None):
        """
        Подключение к api.oneme.ru. Если передан proxy (dict с ключом "server": "http://host:port"),
        соединение идёт через HTTP CONNECT прокси (тот же, что у Playwright/мост SOCKS5).
        Если передан proxy с ключом "_socks5_original" — подключаемся напрямую через SOCKS5 к api.oneme.ru
        и делаем TLS поверх сокета (обход зависания TLS через HTTP CONNECT мост).
        USE_PROXY_FOR_API=false в .env — всегда прямое подключение без прокси.
        """
        if not USE_PROXY_FOR_API:
            proxy = None
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        socks5_original = proxy.get("_socks5_original") if proxy else None
        if socks5_original and _SOCKS_AVAILABLE and py_socks:
            # Прямое подключение через SOCKS5 + TLS; при ошибке — откат на HTTP CONNECT через мост
            server = (socks5_original.get("server") or "").strip().replace("socks5://", "").split("@")[-1]
            if ":" in server:
                proxy_host, proxy_port_str = server.rsplit(":", 1)
                proxy_port = int(proxy_port_str)
            else:
                proxy_host, proxy_port = server, 1080
            proxy_user = socks5_original.get("username") or ""
            proxy_pass = socks5_original.get("password") or ""

            def _socks5_tls_sync():
                raw = py_socks.socksocket()
                raw.settimeout(PROXY_CONNECT_TIMEOUT)
                raw.set_proxy(
                    py_socks.SOCKS5,
                    proxy_host,
                    proxy_port,
                    username=proxy_user or None,
                    password=proxy_pass or None,
                )
                raw.connect((HOST, PORT))
                return ssl_ctx.wrap_socket(raw, server_hostname=HOST)

            loop = asyncio.get_running_loop()
            print(f"[*] Подключаемся к {HOST}:{PORT} напрямую через SOCKS5 {proxy_host}:{proxy_port} (TLS поверх сокета)...")
            try:
                ssl_sock = await asyncio.wait_for(
                    loop.run_in_executor(None, _socks5_tls_sync),
                    timeout=PROXY_CONNECT_TIMEOUT,
                )
                self.reader, self.writer = await asyncio.open_connection(sock=ssl_sock)
                print("[+] SOCKS5+TLS соединение установлено.")
                self._buffer = bytearray()
                self._seq = 0
                self._pending = {}
                self._read_task = asyncio.create_task(self._read_loop())
                print(f"[+] Соединение готово (устройство: {self.device['deviceName']})")
                return
            except Exception as e:
                print(f"[!] SOCKS5+TLS не удалось ({e}), пробуем через мост (HTTP CONNECT)...")
                pass

        if proxy and proxy.get("server"):
            server = (proxy.get("server") or "").strip()
            if not server:
                proxy = None
            else:
                parsed = urlparse(server if "://" in server else "http://" + server)
                proxy_host = parsed.hostname or "127.0.0.1"
                proxy_port = parsed.port or (443 if parsed.scheme == "https" else 80)
                proxy_user = proxy.get("username") or parsed.username
                proxy_pass = proxy.get("password") or parsed.password

        async def _do_connect():
            if proxy and proxy.get("server"):
                upstream = proxy.get("_upstream") or ""
                via = f" (прокси из файла: {upstream})" if upstream else ""
                print(f"[*] Подключаемся к {HOST}:{PORT} через прокси {proxy_host}:{proxy_port}{via}...")
                self.reader, self.writer = await asyncio.open_connection(proxy_host, proxy_port)
                print(f"[+] TCP соединение с прокси {proxy_host}:{proxy_port} установлено, отправляем CONNECT...")
                connect_req = (
                    f"CONNECT {HOST}:{PORT} HTTP/1.1\r\n"
                    f"Host: {HOST}:{PORT}\r\n"
                )
                if proxy_user and proxy_pass:
                    creds = base64.b64encode(f"{proxy_user}:{proxy_pass}".encode()).decode()
                    connect_req += f"Proxy-Authorization: Basic {creds}\r\n"
                connect_req += "\r\n"
                self.writer.write(connect_req.encode())
                await self.writer.drain()
                line = b""
                print("[*] Ждём ответ прокси на CONNECT...")
                while b"\r\n\r\n" not in line:
                    line += await self.reader.read(4096)
                    if not line or len(line) > 8192:
                        raise RuntimeError("Прокси не вернул ответ на CONNECT")
                head = line.split(b"\r\n\r\n", 1)[0].decode("utf-8", errors="replace")
                if "200" not in head.split("\r\n")[0]:
                    raise RuntimeError(f"Прокси отклонил CONNECT: {head.split(chr(10))[0]}")
                print("[+] Прокси подтвердил CONNECT, выполняем TLS-рукопожатие...")
                if hasattr(asyncio, "start_tls"):
                    # Дополнительный таймаут на TLS-рукопожатие, чтобы не висеть вечно на одном прокси.
                    self.reader, self.writer = await asyncio.wait_for(
                        asyncio.start_tls(
                            self.reader, self.writer, ssl_ctx, server_hostname=HOST
                        ),
                        timeout=TLS_HANDSHAKE_TIMEOUT,
                    )
                else:
                    loop = asyncio.get_running_loop()
                    transport = self.writer.transport
                    protocol = transport.get_protocol()
                    new_transport = await asyncio.wait_for(
                        loop.start_tls(
                            transport, protocol, ssl_ctx, server_hostname=HOST
                        ),
                        timeout=TLS_HANDSHAKE_TIMEOUT,
                    )
                    self.reader._transport = new_transport
                    self.writer._transport = new_transport
                print("[+] TLS-рукопожатие успешно, соединение готово.")
            else:
                print(f"[*] Подключаемся к {HOST}:{PORT}...")
                self.reader, self.writer = await asyncio.open_connection(
                    HOST, PORT, ssl=ssl_ctx
                )

        await asyncio.wait_for(_do_connect(), timeout=PROXY_CONNECT_TIMEOUT)
        self._buffer = bytearray()
        self._seq = 0
        self._pending = {}
        self._read_task = asyncio.create_task(self._read_loop())
        print(f"[+] Соединение установлено (устройство: {self.device['deviceName']})")

    async def disconnect(self):
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        self.reader = None
        self.writer = None

    async def _read_loop(self):
        try:
            while True:
                chunk = await self.reader.read(65536)
                if not chunk:
                    break
                self._buffer.extend(chunk)
                self._dispatch_packets()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[-] Ошибка чтения: {e}")

    def _dispatch_packets(self):
        while len(self._buffer) >= 10:
            packed_len = struct.unpack_from(">I", self._buffer, 6)[0]
            payload_len = packed_len & 0x00FFFFFF
            total = 10 + payload_len

            if len(self._buffer) < total:
                break

            packet_bytes = bytes(self._buffer[:total])
            del self._buffer[:total]

            parsed = parse_packet(packet_bytes)
            if not parsed:
                continue

            seq = parsed["seq"]
            opcode = parsed["opcode"]
            fut = self._pending.pop(seq, None)
            if fut and not fut.done():
                fut.set_result(parsed)
            push_fut = self._push_waiters.pop(opcode, None)
            if push_fut and not push_fut.done():
                push_fut.set_result(parsed)

    async def wait_for_opcode(
        self, opcode: int, timeout: float | None = None
    ) -> Dict[str, Any]:
        if timeout is None:
            timeout = API_RESPONSE_TIMEOUT
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._push_waiters[opcode] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._push_waiters.pop(opcode, None)
            raise TimeoutError(f"Timeout: opcode={opcode} не получен за {timeout}с")

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) % 256
        return seq

    def fire(self, opcode: int, payload: dict) -> None:
        seq = self._next_seq()
        packet = pack_packet(self._ver, 0, seq, opcode, payload)
        self.writer.write(packet)

    async def send(
        self,
        opcode: int,
        payload: dict,
        timeout: float | None = None,
    ) -> Dict[str, Any]:
        if timeout is None:
            timeout = API_RESPONSE_TIMEOUT
        seq = self._next_seq()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[seq] = fut

        packet = pack_packet(self._ver, 0, seq, opcode, payload)
        self.writer.write(packet)
        await self.writer.drain()

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(seq, None)
            raise TimeoutError(f"Timeout: opcode={opcode} не ответил за {timeout}с")

    def _user_agent(self) -> dict:
        return {
            "deviceType": "IOS",
            "appVersion": self.app_version,
            "osVersion": self.device["osVersion"],
            "timezone": "Europe/Moscow",
            "screen": self.device["screen"],
            "pushDeviceType": "APNS",
            "arch": self.device["arch"],
            "locale": "ru",
            "deviceLocale": "ru",
            "buildNumber": 6498,
            "deviceName": self.device["deviceName"],
        }

    async def handshake(self) -> None:
        payload = {
            "mt_instanceid": self.mt_instance_id,
            "clientSessionId": self.client_session_id,
            "deviceId": self.device_id,
            "userAgent": self._user_agent(),
        }

        print(f"[*] Handshake (opcode 6)...")
        resp = await self.send(6, payload)

        if resp["cmd"] == 0x300:
            raise RuntimeError(f"Handshake failed: {resp.get('payload')}")

        print("[+] Handshake успешен")

    async def start_auth(self, phone: str) -> str:
        payload = {"phone": phone, "type": "START_AUTH", "language": "ru"}

        print(f"[*] START_AUTH для {phone} (opcode 17)...")
        resp = await self.send(17, payload)

        if resp["cmd"] == 0x300:
            err = resp.get("payload") or {}
            raise RuntimeError(
                f"START_AUTH ошибка: {err.get('localizedMessage') or err.get('error') or err}"
            )

        token = (resp.get("payload") or {}).get("token")
        if not token:
            raise RuntimeError(f"Нет токена в ответе START_AUTH: {resp}")

        print(f"[+] SMS-токен получен: {token}")
        return str(token)

    async def check_code(
        self,
        sms_token: str,
        code: str,
        get_password_async: Optional[Callable[[str], Awaitable[str]]] = None,
    ) -> Tuple[str, str]:
        payload = {
            "verifyCode": code,
            "token": sms_token,
            "authTokenType": "CHECK_CODE",
        }

        print("[*] CHECK_CODE (opcode 18)...")
        resp = await self.send(18, payload)

        p = resp.get("payload") or {}

        if resp["cmd"] == 0x300:
            raise RuntimeError(
                f"CHECK_CODE ошибка: {p.get('localizedMessage') or p.get('error') or p}"
            )

        token_attrs = p.get("tokenAttrs") or {}

        if "LOGIN" in token_attrs:
            login_token = (token_attrs["LOGIN"] or {}).get("token")
            if login_token:
                print("[+] Аккаунт уже зарегистрирован — использован вход по SMS (LOGIN)")
                return "login", str(login_token)
            raise RuntimeError(
                "Аккаунт уже существует, но токен LOGIN не получен"
            )

        if "REGISTER" in token_attrs:
            reg_token = (token_attrs["REGISTER"] or {}).get("token")
            if reg_token:
                print("[+] Новый аккаунт — получен REGISTER-токен")
                return "register", str(reg_token)

        password_challenge = p.get("passwordChallenge")
        if password_challenge:
            track_id = password_challenge.get("trackId")
            config = password_challenge.get("config") or {}
            hint = config.get("hint", "") or ""
            if not track_id:
                raise RuntimeError("В ответе CHECK_CODE есть passwordChallenge, но нет trackId")
            if not get_password_async:
                raise RuntimeError(
                    "Сервер запросил пароль (2FA). Укажите get_password_async при вызове check_code или отключите 2FA в приложении MAX."
                )
            print("[*] Требуется пароль (2FA)...")
            password = (await get_password_async(hint)).strip()
            if not password:
                raise RuntimeError("Пароль не введён")
            print("[*] AUTH_CHECK_PASSWORD (opcode 115)...")
            pwd_resp = await self.send(OPCODE_AUTH_CHECK_PASSWORD, {"trackId": track_id, "password": password})
            pwd_payload = pwd_resp.get("payload") or {}
            if pwd_resp.get("cmd") == 0x300:
                raise RuntimeError(
                    f"Ошибка проверки пароля: {pwd_payload.get('localizedMessage') or pwd_payload.get('error') or pwd_payload}"
                )
            login_attrs = (pwd_payload.get("tokenAttrs") or {}).get("LOGIN") or {}
            login_token = login_attrs.get("token")
            if login_token:
                print("[+] Вход по SMS с 2FA выполнен (LOGIN)")
                return "login", str(login_token)
            raise RuntimeError("После ввода пароля токен LOGIN не получен")

        raise RuntimeError(f"Непредвиденный ответ CHECK_CODE: {resp}")

    async def complete_registration(
        self,
        reg_token: str,
        first_name: str = "User",
        last_name: str = "User",
    ) -> str:
        payload = {
            "lastName": last_name,
            "token": reg_token,
            "firstName": first_name,
            "tokenType": "REGISTER",
        }

        print("[*] REGISTER (opcode 23)...")
        resp = await self.send(23, payload)

        p = resp.get("payload") or {}

        if resp["cmd"] == 0x300:
            raise RuntimeError(f"REGISTER ошибка: {p.get('error') or p}")

        auth_token = p.get("token")
        if not auth_token:
            raise RuntimeError(f"Нет auth-токена в ответе REGISTER: {resp}")

        self.auth_token = str(auth_token)
        print(f"[+] Регистрация завершена! auth_token: {self.auth_token}")
        return self.auth_token

    async def auth_login(self, full_init: bool = True) -> None:
        if not self.auth_token:
            raise RuntimeError("auth_token не установлен")

        payload = {
            "chatsCount": 100,
            "chatsSync": 0,
            "contactsSync": 0,
            "draftsSync": 0,
            "interactive": True,
            "presenceSync": 0,
            "configHash": (
                "00000000-0000000000000000-00000000-"
                "0000000000000000-0000000000000000-0-"
                "0000000000000000-00000000"
            ),
            "token": self.auth_token,
            "userAgent": self._user_agent(),
        }

        print("[*] AUTH (opcode 19)...")
        resp = await self.send(19, payload)

        if resp["cmd"] == 0x300:
            p = resp.get("payload") or {}
            raise RuntimeError(
                f"AUTH ошибка: {p.get('error') or p.get('message') or p}"
            )

        p = resp.get("payload") or {}
        profile = p.get("profile") or {}
        contact = profile.get("contact") or {}
        if contact.get("id"):
            self.user_id = str(contact["id"])
            print(f"[+] AUTH успешен! user_id={self.user_id}")
        else:
            print("[+] AUTH успешен!")

        if not full_init:
            await asyncio.sleep(1.5)
            print("[+] Сессия готова (быстрая инициализация)")
            return

        now_ms = int(asyncio.get_event_loop().time() * 1000)
        session_id = now_ms

        if self.user_id:
            print("[*] NAV COLD_START (opcode 5)...")
            self.fire(
                5,
                {
                    "events": [
                        {
                            "type": "NAV",
                            "event": "COLD_START",
                            "userId": int(self.user_id),
                            "time": now_ms,
                            "params": {
                                "session_id": session_id,
                                "action_id": 1,
                                "screen_to": 150,
                                "source_id": 1,
                            },
                        }
                    ],
                },
            )
            await self.writer.drain()

        await asyncio.sleep(1.0)

        print("[*] Folder sync (opcode 272)...")
        self.fire(272, {"folderSync": 0})
        await self.writer.drain()
        await asyncio.sleep(0.5)

        print("[*] Стикеры (opcode 27)...")
        self.fire(27, {"sync": 0, "type": "STICKER"})
        await self.writer.drain()
        await asyncio.sleep(0.5)

        print("[*] Избранные стикеры (opcode 27)...")
        self.fire(27, {"sync": 0, "type": "FAVORITE_STICKER"})
        await self.writer.drain()
        await asyncio.sleep(0.5)

        print("[*] Recent media (opcode 79)...")
        self.fire(79, {"forward": False, "count": 100})
        await self.writer.drain()

        print("[*] Ожидаем инициализации сессии (5с)...")
        await asyncio.sleep(5.0)

        print("[*] Новые стикер-паки (opcode 26)...")
        self.fire(26, {"sectionId": "NEW_STICKER_SETS", "from": 5, "count": 100})
        await self.writer.drain()

        print("[+] Сессия полностью инициализирована!")

    async def authorize_qr(self, qr_link: str) -> None:
        print("[*] Ping (opcode 1)...")
        self.fire(1, {"interactive": True})
        await self.writer.drain()

        print("[*] Sessions (opcode 96)...")
        self.fire(96, {})
        await self.writer.drain()

        await asyncio.sleep(0.3)

        print(f"[*] QR авторизация (opcode 290): {qr_link[:60]}...")
        resp = await self.send(290, {"qrLink": qr_link})

        if resp["cmd"] == 0x300:
            p = resp.get("payload") or {}
            raise RuntimeError(
                f"QR авторизация ошибка: {p.get('localizedMessage') or p.get('error') or p}"
            )

        print("[+] QR авторизация успешна!")

    async def get_sessions(self) -> list:
        resp = await self.send(96, {})
        p = resp.get("payload") or {}
        return p.get("sessions") or []

    async def terminate_other_sessions(self) -> str:
        resp = await self.send(97, {})

        if resp["cmd"] == 0x300:
            p = resp.get("payload") or {}
            raise RuntimeError(
                f"Ошибка отзыва сессий: {p.get('localizedMessage') or p.get('error') or p}"
            )

        new_token = (resp.get("payload") or {}).get("token")
        if not new_token:
            raise RuntimeError(
                f"Сервер не вернул новый токен в ответе opcode 97: {resp}"
            )

        self.auth_token = str(new_token)
        print(f"[+] Сессии отозваны! Новый токен получен.")
        return self.auth_token

    async def setup_2fa(
        self,
        password: str,
        hint: str = "qw13)",
        email: Optional[str] = None,
    ) -> None:
        print("[*] Инициализация 2FA (opcode 112)...")
        resp = await self.send(112, {"type": 0})
        if resp["cmd"] == 0x300:
            p = resp.get("payload") or {}
            raise RuntimeError(
                f"2FA init ошибка: {p.get('localizedMessage') or p.get('error') or p}"
            )
        track_id = (resp.get("payload") or {}).get("trackId")
        if not track_id:
            raise RuntimeError(f"Нет trackId в ответе opcode 112: {resp}")
        print(f"[+] trackId: {track_id}")

        print("[*] Установка пароля 2FA (opcode 107)...")
        resp = await self.send(107, {"trackId": track_id, "password": password})
        if resp["cmd"] == 0x300:
            p = resp.get("payload") or {}
            raise RuntimeError(
                f"2FA password ошибка: {p.get('localizedMessage') or p.get('error') or p}"
            )
        print("[+] Пароль 2FA установлен")

        print("[*] Установка подсказки 2FA (opcode 108)...")
        resp = await self.send(108, {"trackId": track_id, "hint": hint})
        if resp["cmd"] == 0x300:
            p = resp.get("payload") or {}
            raise RuntimeError(
                f"2FA hint ошибка: {p.get('localizedMessage') or p.get('error') or p}"
            )
        print("[+] Подсказка 2FA установлена")

        capabilities = [0, 3]

        if email:
            print(f"[*] Установка email 2FA (opcode 109): {email}...")
            resp = await self.send(109, {"trackId": track_id, "email": email})
            if resp["cmd"] == 0x300:
                p = resp.get("payload") or {}
                raise RuntimeError(
                    f"2FA email ошибка: {p.get('localizedMessage') or p.get('error') or p}"
                )
            p = resp.get("payload") or {}
            code_len = p.get("codeLength", 6)
            print(f"[+] Email принят, ожидается код ({code_len} цифр)")

            print()
            email_code = input(f"  >>> Введите код из письма на {email}: ").strip()
            print()

            print("[*] Подтверждение email (opcode 110)...")
            resp = await self.send(110, {"trackId": track_id, "verifyCode": email_code})
            if resp["cmd"] == 0x300:
                p = resp.get("payload") or {}
                raise RuntimeError(
                    f"2FA email verify ошибка: {p.get('localizedMessage') or p.get('error') or p}"
                )
            print("[+] Email подтверждён")
            capabilities = [0, 3, 4]

        print("[*] Финальное подтверждение 2FA (opcode 111)...")
        resp = await self.send(
            111,
            {
                "expectedCapabilities": capabilities,
                "trackId": track_id,
                "password": password,
                "hint": hint,
            },
        )
        if resp["cmd"] == 0x300:
            p = resp.get("payload") or {}
            raise RuntimeError(
                f"2FA confirm ошибка: {p.get('localizedMessage') or p.get('error') or p}"
            )
        print("[+] 2FA пароль успешно установлен!")


async def login_by_phone(phone: str) -> str:
    async def _ask_password(hint: str) -> str:
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: input(f"  >>> Введите пароль 2FA (подсказка: {hint or '—'}): ").strip()
        )

    client = MaxClient(ver=10)
    try:
        await client.connect()
        await client.handshake()
        sms_token = await client.start_auth(phone)
        print()
        sms_code = input(f"  >>> Введите SMS-код для {phone}: ").strip()
        print()
        kind, token_value = await client.check_code(sms_token, sms_code, get_password_async=_ask_password)
        if kind == "login":
            return token_value
        raise RuntimeError(
            "Этот номер не зарегистрирован. Сначала зарегистрируйте: "
            "python register_account.py <номер> <пароль>"
        )
    finally:
        await client.disconnect()


async def get_login_token_by_phone_async(
    phone: str,
    get_sms_code: Callable[[], Awaitable[str]],
    proxy: Optional[Dict[str, str]] = None,
    get_password_async: Optional[Callable[[str], Awaitable[str]]] = None,
) -> Tuple[str, str]:
    """
    Вход по номеру: get_sms_code() — async callback.
    Если включён 2FA, вызывается get_password_async(hint) для запроса пароля.
    Возвращает ("login", auth_token) для существующего аккаунта или ("register", reg_token) для нового.
    proxy — опционально, dict {"server": "http://host:port"} для HTTP CONNECT.
    """
    client = MaxClient(ver=10)
    try:
        await client.connect(proxy=proxy)
        await client.handshake()
        sms_token = await client.start_auth(phone)
        sms_code = (await get_sms_code()).strip()
        kind, token_value = await client.check_code(sms_token, sms_code, get_password_async=get_password_async)
        return (kind, token_value)
    finally:
        await client.disconnect()


async def complete_registration_async(
    reg_token: str,
    first_name: str = "User",
    last_name: str = "User",
    proxy: Optional[Dict[str, str]] = None,
) -> str:
    """Завершает регистрацию по reg_token из check_code; возвращает auth_token."""
    client = MaxClient(ver=10)
    try:
        await client.connect(proxy=proxy)
        await client.handshake()
        return await client.complete_registration(reg_token, first_name, last_name)
    finally:
        await client.disconnect()


async def register_account(
    phone: str,
    password: str,
    first_name: str = "User",
    last_name: str = "User",
    hint: str = DEFAULT_HINT,
    email: Optional[str] = None,
) -> Tuple[str, dict, str]:
    client = MaxClient(ver=10)
    auth_token: str

    async def _use_given_password(hint_arg: str) -> str:
        return password

    try:
        await client.connect()
        await client.handshake()

        sms_token = await client.start_auth(phone)

        print()
        sms_code = input(f"  >>> Введите SMS-код для {phone}: ").strip()
        print()

        kind, token_value = await client.check_code(
            sms_token, sms_code, get_password_async=_use_given_password
        )
        if kind == "login":
            auth_token = token_value
        else:
            auth_token = await client.complete_registration(
                token_value, first_name, last_name
            )
    finally:
        await client.disconnect()

    return auth_token, client.device, client.device_id


async def set_password_for_token(
    auth_token: str,
    password: str,
    hint: str = DEFAULT_HINT,
    email: Optional[str] = None,
    proxy: Optional[Dict[str, str]] = None,
) -> Tuple[dict, str]:
    raise RuntimeError(
        "Установка пароля/2FA временно отключена. "
        "Поддерживается только ввод пароля при запросе сервера."
    )


async def revoke_other_sessions(
    auth_token: str,
    proxy: Optional[Dict[str, str]] = None,
) -> Tuple[str, list, dict, str]:
    client = MaxClient(ver=11)
    client.auth_token = auth_token

    try:
        await client.connect(proxy=proxy)
        await client.handshake()
        await client.auth_login(full_init=False)

        print("[*] Получаем список активных сессий...")
        sessions = await client.get_sessions()

        other = [s for s in sessions if not s.get("current")]
        if not other:
            print("[i] Других активных сессий нет — ничего отзывать не нужно.")
            return auth_token, sessions, client.device, client.device_id

        print(f"[i] Найдено других сессий: {len(other)}")
        new_token = await client.terminate_other_sessions()

        await asyncio.sleep(0.5)
        sessions = await client.get_sessions()
    finally:
        await client.disconnect()

    return new_token, sessions, client.device, client.device_id


def decode_qr_from_image(image_path: str) -> str:
    if not _QR_AVAILABLE:
        raise ImportError(
            "Для чтения QR-кода установите зависимости:\n"
            "  pip install pyzbar Pillow\n"
            "  (macOS) brew install zbar"
        )

    with PILImage.open(image_path) as img:
        codes = pyzbar_decode(img)
    if not codes:
        raise ValueError(
            f"QR-код не найден на изображении: {image_path}\n"
            "Убедитесь, что скриншот содержит чёткий QR-код."
        )

    qr_value = codes[0].data.decode("utf-8")
    print(f"[+] QR-код прочитан: {qr_value[:80]}{'...' if len(qr_value) > 80 else ''}")
    return qr_value


def decode_qr_from_bytes(image_bytes: bytes) -> str:
    """Декодирует QR из байтов изображения (PNG/JPEG)."""
    if not _QR_AVAILABLE:
        raise ImportError("Для чтения QR из байтов установите: pip install pyzbar Pillow")
    from io import BytesIO
    with PILImage.open(BytesIO(image_bytes)) as img:
        codes = pyzbar_decode(img)
    if not codes:
        raise ValueError("QR-код не найден на изображении")
    return codes[0].data.decode("utf-8")


async def qr_web_login(auth_token: str, qr_link: str) -> Tuple[dict, str]:
    client = MaxClient(ver=11)
    client.auth_token = auth_token

    try:
        await client.connect()
        await client.handshake()
        await client.auth_login(full_init=False)
        await client.authorize_qr(qr_link)
    finally:
        await client.disconnect()

    return client.device, client.device_id


def _print_qr_link(qr_link: str) -> None:
    if _QRCODE_PRINT_AVAILABLE and qrcode is not None:
        qr = qrcode.QRCode(version=1, error_correction=qrcode.ERROR_CORRECT_L, box_size=1, border=1)
        qr.add_data(qr_link)
        qr.make(fit=True)
        qr.print_ascii()
    else:
        print(f"[*] Откройте ссылку или отсканируйте QR в приложении MAX:\n    {qr_link}\n")


async def get_web_token_via_qr(auth_token: str) -> str:
    """Возвращает только веб-токен (для CLI)."""
    _device_id, token = await get_web_token_via_qr_async(auth_token, get_password_async=None)
    return token


async def get_web_token_via_qr_async(
    auth_token: str,
    get_password_async: Optional[Callable[[str], Awaitable[str]]] = None,
    proxy: Optional[Dict[str, str]] = None,
) -> Tuple[str, str]:
    """
    Получает веб-токен через QR: WebSocket GET_QR, авторизация с iOS (opcode 290), LOGIN_BY_QR.
    Возвращает (device_id, token). Если включён 2FA, вызывается get_password_async(hint).
    proxy — опционально для iOS-клиента (authorize_qr).
    """
    if not _WS_AVAILABLE:
        raise ImportError("Для режима --web-token установите: pip install websockets")

    device_id = str(uuid.uuid4())
    user_agent = {
        "deviceType": "WEB",
        "appVersion": "25.12.14",
        "deviceName": "Chrome",
        "screen": "1920x1080 1.0x",
        "timezone": "Europe/Moscow",
        "locale": "ru",
        "deviceLocale": "ru",
        "osVersion": "Windows 10",
        "clientSessionId": 1,
        "buildNumber": 0x97CB,
    }
    user_agent_header = "Mozilla/5.0 (Windows NT 10.0; rv:91.0) Gecko/20100101 Firefox/91.0"

    pending: Dict[int, asyncio.Future] = {}
    seq = [0]

    def make_message(opcode: int, payload: dict) -> dict:
        seq[0] += 1
        return {"ver": 11, "cmd": 0, "seq": seq[0], "opcode": opcode, "payload": payload}

    async def recv_loop(ws: Any) -> None:
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                s = data.get("seq")
                if s is not None and s in pending:
                    fut = pending.pop(s)
                    if not fut.done():
                        fut.set_result(data)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def send_and_wait(ws: Any, opcode: int, payload: dict, timeout: float = 20.0) -> dict:
        msg = make_message(opcode, payload)
        s = msg["seq"]
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        pending[s] = fut
        await ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            pending.pop(s, None)
            raise RuntimeError(f"Таймаут ожидания ответа opcode={opcode}")
        finally:
            pending.pop(s, None)

    print("[*] Подключение к WebSocket API...")
    ws = await websockets.connect(
        WEBSOCKET_URI,
        origin=WEBSOCKET_ORIGIN,
        user_agent_header=user_agent_header,
    )
    recv_task = asyncio.create_task(recv_loop(ws))

    try:
        print("[*] Handshake (opcode 6)...")
        handshake_resp = await send_and_wait(
            ws, OPCODE_SESSION_INIT, {"deviceId": device_id, "userAgent": user_agent}
        )
        if handshake_resp.get("payload", {}).get("error"):
            raise RuntimeError(f"Handshake ошибка: {handshake_resp.get('payload')}")
        print("[+] Handshake успешен")

        print("[*] Запрос QR (opcode 288)...")
        qr_resp = await send_and_wait(ws, OPCODE_GET_QR, {})
        payload = qr_resp.get("payload") or {}
        if payload.get("error"):
            raise RuntimeError(f"GET_QR ошибка: {payload}")
        qr_link = payload.get("qrLink")
        track_id = payload.get("trackId")
        poll_interval = payload.get("pollingInterval", 2000)
        expires_at = payload.get("expiresAt", 0)
        if not qr_link or not track_id:
            raise RuntimeError("В ответе GET_QR нет qrLink или trackId")

        print("[*] Авторизация QR от имени iOS (opcode 290)...")
        ios = MaxClient(ver=11)
        ios.auth_token = auth_token
        await ios.connect(proxy=proxy)
        try:
            await ios.handshake()
            await ios.auth_login(full_init=False)
            await ios.authorize_qr(qr_link)
        finally:
            await ios.disconnect()
        print("[+] QR авторизован.")

        deadline_ms = int(expires_at)
        while True:
            if int(time.time() * 1000) >= deadline_ms:
                raise RuntimeError("QR-код истёк, начните заново")
            status_resp = await send_and_wait(
                ws, OPCODE_GET_QR_STATUS, {"trackId": track_id}, timeout=25.0
            )
            sp = (status_resp.get("payload") or {}).get("status") or {}
            if sp.get("loginAvailable"):
                break
            await asyncio.sleep(poll_interval / 1000.0)

        print("[+] Подтверждение получено, запрос веб-токена (opcode 291)...")
        login_resp = await send_and_wait(ws, OPCODE_LOGIN_BY_QR, {"trackId": track_id})
        login_payload = login_resp.get("payload") or {}

        token_attrs = login_payload.get("tokenAttrs") or {}
        login_data = token_attrs.get("LOGIN") or {}
        token = login_data.get("token")
        if token:
            return (device_id, str(token))

        password_challenge = login_payload.get("passwordChallenge")
        if password_challenge:
            track_id_2fa = password_challenge.get("trackId")
            hint = password_challenge.get("hint", "—")
            if not track_id_2fa:
                raise RuntimeError("В passwordChallenge нет trackId")
            if get_password_async:
                password = (await get_password_async(hint)).strip()
            else:
                print()
                password = input(f"Введите пароль (подсказка: {hint}): ").strip()
            if not password:
                raise RuntimeError("Пароль не введён")
            pwd_resp = await send_and_wait(
                ws, OPCODE_AUTH_CHECK_PASSWORD, {"trackId": track_id_2fa, "password": password}
            )
            pwd_payload = pwd_resp.get("payload") or {}
            if pwd_payload.get("error"):
                raise RuntimeError(f"Неверный пароль или ошибка: {pwd_payload}")
            login_attrs = (pwd_payload.get("tokenAttrs") or {}).get("LOGIN") or {}
            token = login_attrs.get("token")
            if token:
                return (device_id, str(token))
            raise RuntimeError("В ответе 2FA нет токена")

        raise RuntimeError(
            "В ответе нет токена (tokenAttrs.LOGIN.token) и нет запроса пароля (passwordChallenge)"
        )
    finally:
        recv_task.cancel()
        try:
            await recv_task
        except asyncio.CancelledError:
            pass
        await ws.close()


def main():
    parser = argparse.ArgumentParser(
        description="Регистрация / QR-авторизация веб-версии MAX messenger",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  # Регистрация нового аккаунта:\n"
            "  python register_account.py +79001234567 MyPassword123\n"
            '  python register_account.py +79001234567 MyPassword123 --name "Иван Петров"\n'
            "\n"
            "  # Авторизовать веб-версию MAX через QR-код со скриншота:\n"
            "  python register_account.py --token AN_Sx6HQ... --qr /path/to/screenshot.png\n"
            "\n"
            "  # Отозвать все сессии, кроме текущей:\n"
            "  python register_account.py --token AN_Sx6HQ... --revoke\n"
            "\n"
            "  # Получить веб-токен (по токену или по номеру — вход по SMS, затем авторизация QR):\n"
            "  python register_account.py --token AN_Sx6HQ... --web-token\n"
            "  python register_account.py +79001234567 --web-token\n"
        ),
    )
    parser.add_argument(
        "phone",
        nargs="?",
        help="Номер телефона в формате +79001234567 (для регистрации или для --web-token по номеру)",
    )
    parser.add_argument(
        "password",
        nargs="?",
        default=None,
        help=f"Пароль для аккаунта (по умолчанию: {DEFAULT_PASSWORD!r})",
    )
    parser.add_argument(
        "--hint",
        default=None,
        metavar="HINT",
        help=f"Подсказка для пароля (по умолчанию: {DEFAULT_HINT!r})",
    )
    parser.add_argument(
        "--token",
        metavar="AUTH_TOKEN",
        help="Токен существующего аккаунта",
    )
    parser.add_argument(
        "--qr",
        metavar="SCREENSHOT_PATH",
        help="Путь до скриншота с QR-кодом веб-версии MAX (используется вместе с --token)",
    )
    parser.add_argument(
        "--name",
        default="User User",
        help='Имя и фамилия через пробел (по умолчанию "User User")',
    )
    parser.add_argument(
        "--email",
        metavar="EMAIL",
        default=None,
        help="Email для восстановления 2FA (опционально, потребует код из письма)",
    )
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="Завершить все активные сессии, кроме текущей (используется вместе с --token)",
    )
    parser.add_argument(
        "--web-token",
        action="store_true",
        help="Получить веб-токен: укажите --token (iOS) или номер телефона. При номере — "
        "вход по SMS, затем скрипт авторизует QR веб-API от имени телефона и выведет веб-токен. Требует: pip install websockets.",
    )

    args = parser.parse_args()

    password = args.password or DEFAULT_PASSWORD
    hint = args.hint or DEFAULT_HINT

    if args.web_token:
        if not args.token and not args.phone:
            parser.error(
                "Для --web-token укажите --token AUTH_TOKEN или номер телефона (например +79001234567)"
            )
        if args.token and args.phone:
            parser.error("Для --web-token укажите либо --token, либо номер телефона, не оба")

        print()
        print("=" * 60)
        print("  MAX messenger — веб-токен через авторизацию QR с iOS")
        print("=" * 60)
        if args.phone:
            print("  Вход по номеру, затем скрипт получит QR из веб-API и авторизует его")
            print("  от имени телефона (opcode 290), затем выведет веб-токен.")
        else:
            print("  Токен iOS: скрипт получит QR из веб-API и авторизует его")
            print("  от имени телефона (opcode 290), затем выведет веб-токен.")
        print("=" * 60)
        print()

        async def _web_token_flow() -> str:
            if args.phone:
                print("[*] Шаг 1/2: Вход по номеру телефона...")
                auth_token = await login_by_phone(args.phone)
                print("[+] Вход выполнен.\n")
            else:
                auth_token = args.token
            print("[*] Шаг 2/2: Получение веб-токена (QR из веб-API + авторизация с iOS)...")
            return await get_web_token_via_qr(auth_token)

        try:
            token = asyncio.run(_web_token_flow())
            print()
            print("=" * 60)
            print("  ГОТОВО! Токен веб-версии:")
            print("=" * 60)
            print(f"  {token}")
            print("=" * 60)
        except ImportError as e:
            print(f"\n[!] {e}")
            sys.exit(1)
        except KeyboardInterrupt:
            print("\n[!] Прервано пользователем")
            sys.exit(1)
        except Exception as e:
            print(f"\n[!] Ошибка: {e}")
            sys.exit(1)
        return

    if args.revoke:
        if not args.token:
            parser.error("Для --revoke необходимо передать --token AUTH_TOKEN")

        print()
        print("=" * 60)
        print("  MAX messenger — отзыв всех сторонних сессий")
        print("=" * 60)
        print(f"  Токен : {args.token[:30]}...")
        print("=" * 60)
        print()

        def _fmt_time(ts: int) -> str:
            if not ts:
                return "—"
            from datetime import datetime

            dt = datetime.fromtimestamp(ts / 1000)
            return dt.strftime("%d.%m.%Y %H:%M")

        try:
            new_token, sessions, device, device_id = asyncio.run(
                revoke_other_sessions(args.token)
            )
            print()
            print("=" * 60)
            print("  ГОТОВО! Активные сессии после отзыва:")
            print("=" * 60)
            if sessions:
                for s in sessions:
                    marker = " [текущая]" if s.get("current") else ""
                    print(f"  • {s.get('client', '?')}{marker}")
                    print(
                        f"    {s.get('location', '—')}  |  {_fmt_time(s.get('time', 0))}"
                    )
            else:
                print("  (нет активных сессий)")
            print("-" * 60)
            print(f"  НОВЫЙ ТОКЕН : {new_token}")
            print(f"  Device ID   : {device_id}")
            print("=" * 60)
        except KeyboardInterrupt:
            print("\n[!] Прервано пользователем")
            sys.exit(1)
        except Exception as e:
            print(f"\n[!] Ошибка: {e}")
            sys.exit(1)
        return

    if args.qr:
        if not args.token:
            parser.error("Для QR-авторизации необходимо передать --token AUTH_TOKEN")

        print()
        print("=" * 60)
        print("  MAX messenger — авторизация веб-версии по QR-коду")
        print("=" * 60)
        print(f"  Токен      : {args.token[:30]}...")
        print(f"  Скриншот   : {args.qr}")
        print("=" * 60)
        print()

        try:
            qr_link = decode_qr_from_image(args.qr)
            device, device_id = asyncio.run(qr_web_login(args.token, qr_link))
            print()
            print("=" * 60)
            print("  ГОТОВО! Веб-сессия авторизована.")
            print("=" * 60)
            print(f"  AUTH TOKEN : {args.token}")
            print(f"  QR Link    : {qr_link[:60]}{'...' if len(qr_link) > 60 else ''}")
            print(f"  Устройство : {device['deviceName']}")
            print(f"  ОС         : {device['osVersion']}")
            print(f"  Экран      : {device['screen']}")
            print(f"  Device ID  : {device_id}")
            print("=" * 60)
        except KeyboardInterrupt:
            print("\n[!] Прервано пользователем")
            sys.exit(1)
        except Exception as e:
            print(f"\n[!] Ошибка: {e}")
            sys.exit(1)
        return

    if args.token and not args.qr:
        print("\n[!] Режим установки пароля/2FA временно отключён.")
        print("    Доступные режимы с --token: --qr, --revoke, --web-token")
        return

    if not args.phone:
        parser.error(
            "Укажите номер телефона, либо используйте --token + --qr "
            "для QR-авторизации веб-версии"
        )

    name_parts = args.name.split(maxsplit=1)
    first_name = name_parts[0] if name_parts else "User"
    last_name = name_parts[1] if len(name_parts) > 1 else "User"

    print()
    print("=" * 60)
    print("  MAX messenger — авторегистрация аккаунта")
    print("=" * 60)
    print(f"  Телефон  : {args.phone}")
    print(f"  Имя      : {first_name} {last_name}")
    print(f"  Пароль   : {password}")
    print(f"  Подсказка: {hint}")
    print("=" * 60)
    print()

    try:
        token, device, device_id = asyncio.run(
            register_account(
                args.phone,
                password,
                first_name,
                last_name,
                hint=hint,
                email=args.email,
            )
        )
        print()
        print("=" * 60)
        print("  ГОТОВО!")
        print("=" * 60)
        print(f"  AUTH TOKEN : {token}")
        print(f"  Устройство : {device['deviceName']}")
        print(f"  ОС         : {device['osVersion']}")
        print(f"  Экран      : {device['screen']}")
        print(f"  Device ID  : {device_id}")
        print("=" * 60)

    except KeyboardInterrupt:
        print("\n[!] Прервано пользователем")
        sys.exit(1)
    except Exception as e:
        print(f"\n[!] Ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
