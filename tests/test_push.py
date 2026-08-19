import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from server.database import Database
from server.push import PushService


class FakeResponse:
    def __init__(self, status_code, reason=""):
        self.status_code = status_code
        self.reason = reason

    def json(self):
        return {"reason": self.reason}


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, headers, content):
        self.requests.append(url)
        return self.responses.pop(0)


class ConfiguredPushService(PushService):
    @property
    def configured(self):
        return True

    @property
    def bundle_id(self):
        return "Michael.SmartHomeBoard"

    def _provider_token(self):
        return "test-provider-token"


class PushTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(self.temp.name)
        self.service = ConfiguredPushService(self.database)
        self.invalid_token = "a" * 64
        self.valid_token = "b" * 64
        self.service.register(self.invalid_token, "sandbox", "Altes iPad")
        self.service.register(self.valid_token, "sandbox", "Aktuelles iPad")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_all_devices_continues_after_invalid_token(self):
        client = FakeClient([
            FakeResponse(410, "Unregistered"),
            FakeResponse(200),
        ])
        with patch("server.push.httpx.AsyncClient", return_value=client):
            sent = await self.service.send("Test", "Nachricht", recipient_ids=[])

        self.assertEqual(sent, 1)
        self.assertEqual(len(client.requests), 2)
        devices = self.database.setting("push_devices", [])
        self.assertEqual([item["device_token"] for item in devices], [self.valid_token])

    async def test_all_devices_continues_after_temporary_failure(self):
        client = FakeClient([
            FakeResponse(403, "InvalidProviderToken"),
            FakeResponse(200),
        ])
        with patch("server.push.httpx.AsyncClient", return_value=client):
            sent = await self.service.send("Test", "Nachricht", recipient_ids=[])

        self.assertEqual(sent, 1)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(len(self.database.setting("push_devices", [])), 2)

    async def test_all_devices_continues_after_connection_error(self):
        client = FakeClient([
            httpx.ConnectError("APNs vorübergehend nicht erreichbar"),
            FakeResponse(200),
        ])

        async def post(url, headers, content):
            client.requests.append(url)
            response = client.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        client.post = post
        with patch("server.push.httpx.AsyncClient", return_value=client):
            sent = await self.service.send("Test", "Nachricht", recipient_ids=[])

        self.assertEqual(sent, 1)
        self.assertEqual(len(client.requests), 2)


if __name__ == "__main__":
    unittest.main()
