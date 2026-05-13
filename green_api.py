"""
Клиент GREEN-API v3 для мессенджера MAX.
Документация: https://green-api.com/v3/docs/
"""
import time
import requests


class GreenApiMax:
    BASE_PATH = "/v3/waInstance{id_instance}"

    def __init__(self, api_url: str, id_instance: str, api_token: str):
        self.api_url = api_url.rstrip("/")
        self.id_instance = id_instance
        self.api_token = api_token

    def _url(self, method: str) -> str:
        path = self.BASE_PATH.format(id_instance=self.id_instance)
        return f"{self.api_url}{path}/{method}/{self.api_token}"

    def get_qr(self) -> dict:
        """Получить QR-код. Ответ: { type: 'qrCode'|'error'|'already_registered', message: str }"""
        r = requests.get(
            self._url("qr"),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def get_state(self) -> dict:
        """Состояние инстанса. Ответ: { stateInstance: 'notAuthorized'|'authorized'|... }"""
        r = requests.get(
            self._url("getStateInstance"),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def get_qr_image_bytes(self) -> bytes | None:
        """Возвращает байты изображения QR или None, если QR недоступен."""
        data = self.get_qr()
        if data.get("type") == "qrCode" and data.get("message"):
            import base64
            return base64.b64decode(data["message"])
        return None
