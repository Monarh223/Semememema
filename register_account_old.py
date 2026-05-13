"""
MAX messenger — регистрация аккаунта, установка пароля, авторизация веб-версии по QR.
Использование: см. python register_account.py --help
"""

import asyncio
import json
import ssl
import struct
import time
import uuid
import random
import sys
import argparse
import getpass
from typing import Dict, Any, Optional, Tuple

try:
    import msgpack
except ImportError:
    print("Установите зависимость: pip install msgpack")
    sys.exit(1)

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

HOST = "api.oneme.ru"
PORT = 443

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

ANDROID_DEVICES = [
    {
        "deviceName": "Samsung Galaxy S23",
        "osVersion": "Android 14",
        "screen": "xxhdpi 480dpi 1080x2340",
        "arch": "arm64-v8a",
    },
    {
        "deviceName": "Samsung Galaxy S22 Ultra",
        "osVersion": "Android 13",
        "screen": "xxhdpi 500dpi 1440x3088",
        "arch": "arm64-v8a",
    },
    {
        "deviceName": "Google Pixel 8 Pro",
        "osVersion": "Android 14",
        "screen": "xxhdpi 480dpi 1344x2992",
        "arch": "arm64-v8a",
    },
    {
        "deviceName": "Xiaomi 13 Pro",
        "osVersion": "Android 13",
        "screen": "xxhdpi 440dpi 1440x3200",
        "arch": "arm64-v8a",
    },
    {
        "deviceName": "Xiaomi Redmi Note 12",
        "osVersion": "Android 13",
        "screen": "xxhdpi 395dpi 1080x2400",
        "arch": "arm64-v8a",
    },
    {
        "deviceName": "OnePlus 11",
        "osVersion": "Android 13",
        "screen": "xxhdpi 450dpi 1440x3216",
        "arch": "arm64-v8a",
    },
    {
        "deviceName": "POCO F5",
        "osVersion": "Android 13",
        "screen": "xxhdpi 420dpi 1080x2400",
        "arch": "arm64-v8a",
    },
    {
        "deviceName": "realme GT Neo 5",
        "osVersion": "Android 13",
        "screen": "xxhdpi 460dpi 1080x2772",
        "arch": "arm64-v8a",
    },
]

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

        self.device = random.choice(ANDROID_DEVICES)
        self.device_id = generate_device_id()
        self.mt_instance_id = str(uuid.uuid4())
        self.client_session_id = random.randint(1, 100)

        self.auth_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self._push_waiters: Dict[int, asyncio.Future] = {}

    async def connect(self):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        print(f"[*] Подключаемся к {HOST}:{PORT}...")
        self.reader, self.writer = await asyncio.open_connection(
            HOST, PORT, ssl=ssl_ctx
        )
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
        self, opcode: int, timeout: float = 15.0
    ) -> Dict[str, Any]:
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
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
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
            "deviceType": "ANDROID",
            "appVersion": "25.21.3",
            "osVersion": self.device["osVersion"],
            "timezone": "Europe/Moscow",
            "screen": self.device["screen"],
            "pushDeviceType": "GCM",
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

    async def check_code(self, sms_token: str, code: str) -> Tuple[str, str]:
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
        resp = await self.send(19, payload, timeout=30.0)

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
    client = MaxClient(ver=10)
    try:
        await client.connect()
        await client.handshake()
        sms_token = await client.start_auth(phone)
        print()
        sms_code = input(f"  >>> Введите SMS-код для {phone}: ").strip()
        print()
        kind, token_value = await client.check_code(sms_token, sms_code)
        if kind == "login":
            return token_value
        raise RuntimeError(
            "Этот номер не зарегистрирован. Сначала зарегистрируйте: "
            "python register_account.py <номер> <пароль>"
        )
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

    try:
        await client.connect()
        await client.handshake()

        sms_token = await client.start_auth(phone)

        print()
        sms_code = input(f"  >>> Введите SMS-код для {phone}: ").strip()
        print()

        kind, token_value = await client.check_code(sms_token, sms_code)
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
) -> Tuple[dict, str]:
    client = MaxClient(ver=11)
    client.auth_token = auth_token

    try:
        await client.connect()
        await client.handshake()
        await client.auth_login()
        await client.setup_2fa(password, hint=hint, email=email)
    finally:
        await client.disconnect()

    return client.device, client.device_id


async def revoke_other_sessions(auth_token: str) -> Tuple[str, list, dict, str]:
    client = MaxClient(ver=11)
    client.auth_token = auth_token

    try:
        await client.connect()
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

    img = PILImage.open(image_path)
    codes = pyzbar_decode(img)
    if not codes:
        raise ValueError(
            f"QR-код не найден на изображении: {image_path}\n"
            "Убедитесь, что скриншот содержит чёткий QR-код."
        )

    qr_value = codes[0].data.decode("utf-8")
    print(f"[+] QR-код прочитан: {qr_value[:80]}{'...' if len(qr_value) > 80 else ''}")
    return qr_value


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

        print("[*] Авторизация QR от имени Android (opcode 290)...")
        android = MaxClient(ver=11)
        android.auth_token = auth_token
        await android.connect()
        try:
            await android.handshake()
            await android.auth_login(full_init=False)
            await android.authorize_qr(qr_link)
        finally:
            await android.disconnect()
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
            return str(token)

        password_challenge = login_payload.get("passwordChallenge")
        if password_challenge:
            track_id_2fa = password_challenge.get("trackId")
            hint = password_challenge.get("hint", "—")
            if not track_id_2fa:
                raise RuntimeError("В passwordChallenge нет trackId")
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
                return str(token)
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
        description="Регистрация / установка пароля / QR-авторизация веб-версии MAX messenger",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  # Регистрация нового аккаунта:\n"
            "  python register_account.py +79001234567 MyPassword123\n"
            '  python register_account.py +79001234567 MyPassword123 --name "Иван Петров"\n'
            "\n"
            "  # Установить пароль/2FA на существующий аккаунт по токену:\n"
            "  python register_account.py --token AN_Sx6HQ... MyPassword123\n"
            "  python register_account.py --token AN_Sx6HQ... --setup-2fa MyPassword123\n"
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
        "--setup-2fa",
        action="store_true",
        help="Установить 2FA на аккаунт: после регистрации или с --token (пароль — аргумент PASSWORD)",
    )
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="Завершить все активные сессии, кроме текущей (используется вместе с --token)",
    )
    parser.add_argument(
        "--web-token",
        action="store_true",
        help="Получить веб-токен: укажите --token (Android) или номер телефона. При номере — "
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
        print("  MAX messenger — веб-токен через авторизацию QR с Android")
        print("=" * 60)
        if args.phone:
            print("  Вход по номеру, затем скрипт получит QR из веб-API и авторизует его")
            print("  от имени телефона (opcode 290), затем выведет веб-токен.")
        else:
            print("  Токен Android: скрипт получит QR из веб-API и авторизует его")
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
            print("[*] Шаг 2/2: Получение веб-токена (QR из веб-API + авторизация с Android)...")
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
        print()
        print("=" * 60)
        print("  MAX messenger — установка 2FA по токену")
        print("=" * 60)
        print(f"  Токен    : {args.token[:30]}...")
        print(f"  Пароль   : {password}")
        print(f"  Подсказка: {hint}")
        print("=" * 60)
        print()

        try:
            device, device_id = asyncio.run(
                set_password_for_token(
                    args.token, password, hint=hint, email=args.email
                )
            )
            print()
            print("=" * 60)
            print("  ГОТОВО!")
            print("=" * 60)
            print(f"  AUTH TOKEN : {args.token}")
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

    if not args.phone:
        parser.error(
            "Укажите номер телефона, используйте --token для установки пароля "
            "или --token + --qr для QR-авторизации веб-версии"
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

        if args.setup_2fa:
            print()
            print("=" * 60)
            print("  Установка 2FA-пароля для аккаунта")
            print("=" * 60)
            print(f"  Пароль   : {password}")
            print(f"  Подсказка: {hint}")
            print("=" * 60)
            print()

            device2, device_id2 = asyncio.run(
                set_password_for_token(
                    token,
                    password,
                    hint=hint,
                    email=args.email,
                )
            )

            print()
            print("=" * 60)
            print("  2FA успешно установлена.")
            print("=" * 60)
            print(f"  Устройство : {device2['deviceName']}")
            print(f"  ОС         : {device2['osVersion']}")
            print(f"  Экран      : {device2['screen']}")
            print(f"  Device ID  : {device_id2}")
            print("=" * 60)
    except KeyboardInterrupt:
        print("\n[!] Прервано пользователем")
        sys.exit(1)
    except Exception as e:
        print(f"\n[!] Ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
