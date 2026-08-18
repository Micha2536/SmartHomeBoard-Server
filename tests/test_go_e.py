import importlib.util
import sys
import unittest
from pathlib import Path

import httpx

MODULE_PATH = Path(__file__).parents[1] / "modules" / "go_e" / "module.py"
SPEC = importlib.util.spec_from_file_location("test_go_e_module", MODULE_PATH)
GO_E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GO_E)


class Context:
    integration_name = "Wallbox"


class GoEAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_hostname_is_resolved_with_mdns(self):
        async def resolve(_hostname):
            return "192.168.1.44"

        original = GO_E.resolve_ipv4
        GO_E.resolve_ipv4 = resolve
        adapter = GO_E.GoEAdapter({"host": "app.local", "port": 8080}, Context())
        try:
            self.assertEqual(await adapter._base_url(), "http://192.168.1.44:8080")
        finally:
            GO_E.resolve_ipv4 = original
            await adapter.stop()

    async def test_legacy_filter_fallback(self):
        requests = []

        def respond(request):
            requests.append(str(request.url))
            query = request.url.params.get("filter", "")
            if query and not query.startswith("["):
                return httpx.Response(400, json={"error": "unsupported filter"})
            if query:
                return httpx.Response(200, json={"sse": "123", "car": 1, "amp": 16})
            return httpx.Response(500)

        adapter = GO_E.GoEAdapter({"host": "192.0.2.10", "port": 80}, Context())
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=1)
        try:
            status = await adapter._request_status()
            self.assertEqual(status["sse"], "123")
            self.assertGreaterEqual(len(requests), 2)
        finally:
            await adapter.stop()


if __name__ == "__main__":
    unittest.main()
