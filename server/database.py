import json
import sqlite3
from pathlib import Path
from threading import RLock


class Database:
    def __init__(self, data_dir: str):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(Path(data_dir) / "smarthomeboard.sqlite3", check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        with self._connection:
            # API and setup portal run as two processes against the same file.
            # WAL plus a busy timeout keeps simultaneous app/web writes reliable.
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS integrations (
                    id TEXT PRIMARY KEY,
                    module_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    configuration TEXT NOT NULL,
                    status TEXT,
                    error TEXT,
                    device_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    id INTEGER PRIMARY KEY,
                    integration_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS displays (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    firmware_version TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    pairing_code TEXT NOT NULL,
                    device_token_hash TEXT NOT NULL,
                    configuration TEXT NOT NULL DEFAULT '{}',
                    configuration_version INTEGER NOT NULL DEFAULT 1,
                    last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)

    def integrations(self):
        with self._lock:
            rows = self._connection.execute("SELECT * FROM integrations ORDER BY name COLLATE NOCASE").fetchall()
        return [self._integration(row) for row in rows]

    def integration(self, integration_id):
        with self._lock:
            row = self._connection.execute("SELECT * FROM integrations WHERE id=?", (integration_id,)).fetchone()
        return self._integration(row) if row else None

    def save_integration(self, item):
        with self._lock, self._connection:
            self._connection.execute("""
                INSERT INTO integrations(id,module_id,name,enabled,configuration,status,error,device_count)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET module_id=excluded.module_id,name=excluded.name,
                enabled=excluded.enabled,configuration=excluded.configuration,status=excluded.status,
                error=excluded.error,device_count=excluded.device_count,updated_at=CURRENT_TIMESTAMP
            """, (item["id"], item["module_id"], item["name"], int(item.get("enabled", True)),
                  json.dumps(item.get("configuration", {})), item.get("status"), item.get("error"), item.get("device_count", 0)))
        return self.integration(item["id"])

    def delete_integration(self, integration_id):
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM nodes WHERE integration_id=?", (integration_id,))
            self._connection.execute("DELETE FROM integrations WHERE id=?", (integration_id,))
            self._connection.execute("DELETE FROM settings WHERE key=?", (f"module_state:{integration_id}",))
            self._connection.execute("DELETE FROM settings WHERE key LIKE ?", (f"module_secret:{integration_id}:%",))

    def set_integration_state(self, integration_id, status, error=None, device_count=None):
        current = self.integration(integration_id)
        if not current:
            return
        current["status"], current["error"] = status, error
        if device_count is not None:
            current["device_count"] = device_count
        self.save_integration(current)

    def nodes(self):
        with self._lock:
            rows = self._connection.execute("SELECT payload FROM nodes ORDER BY id").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def nodes_for_integration(self, integration_id):
        with self._lock:
            rows = self._connection.execute("SELECT payload FROM nodes WHERE integration_id=?", (integration_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_node(self, integration_id, node):
        with self._lock, self._connection:
            self._connection.execute("""
                INSERT INTO nodes(id,integration_id,payload) VALUES(?,?,?)
                ON CONFLICT(id) DO UPDATE SET integration_id=excluded.integration_id,payload=excluded.payload,updated_at=CURRENT_TIMESTAMP
            """, (node["id"], integration_id, json.dumps(node)))

    def remove_nodes(self, integration_id):
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM nodes WHERE integration_id=?", (integration_id,))

    def remove_node(self, integration_id, node_id):
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM nodes WHERE integration_id=? AND id=?", (integration_id, node_id))

    def setting(self, key, default=None):
        with self._lock:
            row = self._connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        with self._lock, self._connection:
            self._connection.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def displays(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM displays ORDER BY status DESC, name COLLATE NOCASE"
            ).fetchall()
        return [self._display(row) for row in rows]

    def display(self, display_id, include_credentials=False):
        with self._lock:
            row = self._connection.execute("SELECT * FROM displays WHERE id=?", (display_id,)).fetchone()
        return self._display(row, include_credentials) if row else None

    def register_display(self, item):
        with self._lock, self._connection:
            self._connection.execute("""
                INSERT INTO displays(
                    id,name,model,firmware_version,ip_address,pairing_code,device_token_hash
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    model=excluded.model,
                    firmware_version=excluded.firmware_version,
                    ip_address=excluded.ip_address,
                    last_seen=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                item["id"], item["name"], item["model"], item["firmware_version"],
                item["ip_address"], item["pairing_code"], item["device_token_hash"]
            ))
        return self.display(item["id"])

    def touch_display(self, display_id, ip_address="", firmware_version=""):
        assignments = ["last_seen=CURRENT_TIMESTAMP", "updated_at=CURRENT_TIMESTAMP"]
        values = []
        if ip_address:
            assignments.append("ip_address=?")
            values.append(ip_address)
        if firmware_version:
            assignments.append("firmware_version=?")
            values.append(firmware_version)
        values.append(display_id)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"UPDATE displays SET {','.join(assignments)} WHERE id=?",
                values,
            )
        return cursor.rowcount > 0

    def pair_display(self, display_id, name):
        with self._lock, self._connection:
            cursor = self._connection.execute("""
                UPDATE displays SET name=?,status='paired',pairing_code='',updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (name, display_id))
        return self.display(display_id) if cursor.rowcount else None

    def save_display_configuration(self, display_id, configuration):
        with self._lock, self._connection:
            cursor = self._connection.execute("""
                UPDATE displays SET configuration=?,configuration_version=configuration_version+1,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?
            """, (json.dumps(configuration), display_id))
        return self.display(display_id) if cursor.rowcount else None

    def rename_display(self, display_id, name):
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE displays SET name=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (name, display_id),
            )
        return self.display(display_id) if cursor.rowcount else None

    def delete_display(self, display_id):
        with self._lock, self._connection:
            cursor = self._connection.execute("DELETE FROM displays WHERE id=?", (display_id,))
        return cursor.rowcount > 0

    @staticmethod
    def _integration(row):
        return {"id": row["id"], "module_id": row["module_id"], "name": row["name"], "enabled": bool(row["enabled"]),
                "configuration": json.loads(row["configuration"]), "status": row["status"], "error": row["error"], "device_count": row["device_count"]}

    @staticmethod
    def _display(row, include_credentials=False):
        item = {
            "id": row["id"],
            "name": row["name"],
            "model": row["model"],
            "firmware_version": row["firmware_version"],
            "ip_address": row["ip_address"],
            "status": row["status"],
            "pairing_code": row["pairing_code"],
            "configuration": json.loads(row["configuration"]),
            "configuration_version": row["configuration_version"],
            "last_seen": row["last_seen"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_credentials:
            item["device_token_hash"] = row["device_token_hash"]
        return item
