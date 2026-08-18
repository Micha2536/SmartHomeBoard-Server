import importlib.util
import logging
from pathlib import Path

log = logging.getLogger("smarthomeboard.modules")


class ModuleRegistry:
    def __init__(self, module_dir):
        self.module_dir = Path(module_dir)
        self.modules = {}

    def load(self):
        self.modules.clear()
        self.module_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.module_dir.glob("*/module.py")):
            try:
                name = f"shb_module_{path.parent.name}"
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                manifest = module.manifest()
                module_id = manifest["id"]
                if module_id in self.modules:
                    raise ValueError(f"doppelte Modul-ID {module_id}")
                self.modules[module_id] = {"manifest": manifest, "factory": module.create}
                log.info("Modul %s (%s) geladen", manifest.get("name"), manifest.get("version"))
            except Exception:
                log.exception("Modul aus %s konnte nicht geladen werden", path)
        return self.manifests()

    def manifests(self):
        return [entry["manifest"] for entry in self.modules.values()]

    def create(self, module_id, configuration, context):
        if module_id not in self.modules:
            raise KeyError(f"Modul {module_id} ist nicht installiert")
        return self.modules[module_id]["factory"](configuration, context)


class ModuleContext:
    def __init__(self, runtime, integration_id, integration_name):
        self.runtime = runtime
        self.integration_id = integration_id
        self.integration_name = integration_name

    async def publish_node(self, node):
        await self.runtime.publish_node(self.integration_id, node)

    async def set_status(self, status, error=None):
        self.runtime.database.set_integration_state(self.integration_id, status, error)
        await self.runtime.broadcast({"type": "integration", "integration_id": self.integration_id, "status": status, "error": error})

    def stable_node_id(self, external_id):
        return self.runtime.stable_id(self.integration_id, external_id, 1_700_000_000, 1_799_000_000)

    def load_state(self, default=None):
        """Persistent, module-private state for learned devices and similar data."""
        return self.runtime.database.setting(f"module_state:{self.integration_id}", default)

    def save_state(self, value):
        self.runtime.database.set_setting(f"module_state:{self.integration_id}", value)

    def load_secret(self, name, default=""):
        return self.runtime.database.setting(f"module_secret:{self.integration_id}:{name}", default)

    def save_secret(self, name, value):
        self.runtime.database.set_setting(f"module_secret:{self.integration_id}:{name}", value)

    def clear_configuration_value(self, key):
        """Remove a one-time configuration value without restarting the adapter."""
        current = self.runtime.database.integration(self.integration_id)
        if not current:
            return
        configuration = dict(current.get("configuration", {}))
        if not configuration.get(key):
            return
        configuration[key] = ""
        current["configuration"] = configuration
        self.runtime.database.save_integration(current)

    def nodes(self):
        """Return the last persisted snapshot owned by this integration."""
        return self.runtime.database.nodes_for_integration(self.integration_id)

    async def remove_node(self, node_id):
        self.runtime.database.remove_node(self.integration_id, node_id)
        await self.runtime.broadcast_snapshot()

    @staticmethod
    def attribute_id(node_id, offset):
        return node_id * 100 + offset
