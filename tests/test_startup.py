"""A missing transitive import must be caught before distributing a release."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class StartupTests(unittest.TestCase):
    def test_fresh_process_import_and_complete_application_lifecycle(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="shb-startup-test-") as folder:
            environment = dict(os.environ, SHB_DATA_DIR=folder, SHB_MODULE_DIR=str(root / "modules"),
                               SHB_DISABLE_DISPLAY_DISCOVERY="1", SHB_API_TOKEN="startup-test-only")
            script = """
from fastapi.testclient import TestClient
from server.main import app
with TestClient(app) as client:
    response = client.get('/api/v1/health', headers={'Authorization': 'Bearer startup-test-only'})
    assert response.status_code == 200, response.text
    assert response.json()['modules'] > 0, response.text
    print('STARTUP_AND_HEALTH_OK')
"""
            result = subprocess.run([sys.executable, "-c", script], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("STARTUP_AND_HEALTH_OK", result.stdout)
