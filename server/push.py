from __future__ import annotations

import base64
import json
import os
import time
import hashlib
import logging

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

log = logging.getLogger("smarthomeboard.push")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class PushService:
    def __init__(self, database):
        self.database = database
        self._jwt = ""
        self._jwt_created = 0.0

    def status(self):
        devices = self.database.setting("push_devices", []) or []
        return {
            "configured": self.configured,
            "device_count": len(devices),
            "devices": [
                {"id": self._device_id(item), "name": item.get("device_name") or "iPhone/iPad"}
                for item in devices
            ],
        }

    @property
    def configured(self):
        return all((self.team_id, self.key_id, self.bundle_id, self.key_path)) and os.path.isfile(self.key_path)

    @property
    def team_id(self):
        return os.getenv("SHB_APNS_TEAM_ID", "").strip()

    @property
    def key_id(self):
        return os.getenv("SHB_APNS_KEY_ID", "").strip()

    @property
    def bundle_id(self):
        return os.getenv("SHB_APNS_BUNDLE_ID", "").strip()

    @property
    def key_path(self):
        return os.getenv("SHB_APNS_KEY_PATH", "").strip()

    def register(self, device_token, environment, device_name):
        token = str(device_token).strip().lower()
        if len(token) < 32 or any(character not in "0123456789abcdef" for character in token):
            raise ValueError("Der APNs-Gerätetoken ist ungültig")
        item = {
            "id": hashlib.sha256(token.encode()).hexdigest()[:24],
            "device_token": token,
            "environment": "sandbox" if environment == "sandbox" else "production",
            "device_name": str(device_name or "iPhone/iPad")[:100],
            "updated_at": time.time(),
        }
        devices = [entry for entry in (self.database.setting("push_devices", []) or []) if entry.get("device_token") != token]
        devices.append(item)
        self.database.set_setting("push_devices", devices[-50:])
        return len(devices)

    async def send(self, title, message, recipient_ids=None):
        if not self.configured:
            raise ValueError("Apple Push ist noch nicht konfiguriert (SHB_APNS_TEAM_ID, KEY_ID, BUNDLE_ID und KEY_PATH)")
        all_devices = self.database.setting("push_devices", []) or []
        devices = all_devices
        recipients = {str(item) for item in (recipient_ids or []) if str(item)}
        if recipients:
            devices = [item for item in devices if self._device_id(item) in recipients]
        if not devices:
            raise ValueError("Keines der ausgewählten iOS-Geräte ist noch für Push registriert")
        payload = json.dumps({"aps": {"alert": {"title": title[:180], "body": message[:1500]}, "sound": "default"}}, ensure_ascii=False).encode()
        sent = 0
        invalid = set()
        failures = []
        headers = {
            "authorization": f"bearer {self._provider_token()}",
            "apns-topic": self.bundle_id,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        async with httpx.AsyncClient(http2=True, timeout=15, trust_env=False) as client:
            for device in devices:
                host = "api.sandbox.push.apple.com" if device.get("environment") == "sandbox" else "api.push.apple.com"
                try:
                    response = await client.post(
                        f"https://{host}/3/device/{device['device_token']}", headers=headers, content=payload
                    )
                except httpx.HTTPError as error:
                    failures.append(f"{device.get('device_name') or self._device_id(device)}: {error}")
                    continue
                if response.status_code == 200:
                    sent += 1
                    continue
                try:
                    reason = response.json().get("reason", "")
                except ValueError:
                    reason = ""
                if response.status_code in (400, 410):
                    if reason in ("BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"):
                        invalid.add(device["device_token"])
                failures.append(
                    f"{device.get('device_name') or self._device_id(device)}: "
                    f"HTTP {response.status_code}{f' ({reason})' if reason else ''}"
                )
        if invalid:
            self.database.set_setting("push_devices", [item for item in all_devices if item.get("device_token") not in invalid])
        if not sent:
            detail = "; ".join(failures[:4])
            raise ValueError(f"Apple Push konnte an kein registriertes Gerät zugestellt werden{': ' + detail if detail else ''}")
        if failures:
            log.warning("Push teilweise zugestellt (%s erfolgreich): %s", sent, "; ".join(failures))
        return sent

    @staticmethod
    def _device_id(item):
        token = str(item.get("device_token", ""))
        return str(item.get("id") or hashlib.sha256(token.encode()).hexdigest()[:24])

    def _provider_token(self):
        now = time.time()
        if self._jwt and now - self._jwt_created < 3000:
            return self._jwt
        with open(self.key_path, "rb") as key_file:
            private_key = serialization.load_pem_private_key(key_file.read(), password=None)
        header = _b64url(json.dumps({"alg": "ES256", "kid": self.key_id}, separators=(",", ":")).encode())
        claims = _b64url(json.dumps({"iss": self.team_id, "iat": int(now)}, separators=(",", ":")).encode())
        signing_input = f"{header}.{claims}".encode()
        der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der_signature)
        signature = _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        self._jwt = f"{header}.{claims}.{signature}"
        self._jwt_created = now
        return self._jwt
