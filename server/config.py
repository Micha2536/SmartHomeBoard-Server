import json
import os
from pathlib import Path

DEFAULT_PORT = 8787
SETUP_PORT = 8400


def data_dir():
    path = Path(os.getenv("SHB_DATA_DIR", "/data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_server_config():
    path = data_dir() / "server-config.json"
    if not path.exists():
        return {"port": int(os.getenv("SHB_PORT", DEFAULT_PORT))}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {"port": min(65535, max(1024, int(payload.get("port", DEFAULT_PORT))))}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"port": DEFAULT_PORT}


def save_server_config(configuration):
    path = data_dir() / "server-config.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(configuration, indent=2), encoding="utf-8")
    temporary.replace(path)
