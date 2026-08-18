import json
import unittest

from server.display_discovery import DISCOVERY_REQUEST, discovery_response


class DisplayDiscoveryTests(unittest.TestCase):
    def test_discovery_response_contains_registration_endpoint(self):
        self.assertEqual(DISCOVERY_REQUEST, b"SHB_DISCOVER_V1")
        payload = json.loads(discovery_response(8787, "server-123", "0.8.0"))
        self.assertEqual(payload["service"], "SmartHomeBoard")
        self.assertEqual(payload["protocol"], 1)
        self.assertEqual(payload["server_id"], "server-123")
        self.assertEqual(payload["api_port"], 8787)
        self.assertEqual(payload["registration_path"], "/api/v1/displays/register")


if __name__ == "__main__":
    unittest.main()
