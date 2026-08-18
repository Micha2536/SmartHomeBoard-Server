import asyncio
import hashlib
import logging

from .modules import ModuleContext

log = logging.getLogger("smarthomeboard.runtime")


class Runtime:
    def __init__(self, database, registry):
        self.database = database
        self.registry = registry
        self.adapters = {}
        self.websockets = set()
        self.restart_tasks = {}
        self.pending_restarts = {}
        self.sequence = int(database.setting("sequence", 0))
        self.automation_engine = None

    async def start(self):
        self.registry.load()
        for instance in self.database.integrations():
            if instance["enabled"]:
                await self.start_instance(instance)

    async def shutdown(self):
        self.pending_restarts.clear()
        tasks = list(self.restart_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.restart_tasks.clear()
        for integration_id in list(self.adapters):
            await self.stop_instance(integration_id)

    def schedule_restart(self, instance):
        """Speichervorgänge sofort bestätigen und Neustarts je Integration bündeln."""
        integration_id = instance["id"]
        self.pending_restarts[integration_id] = instance
        current = self.restart_tasks.get(integration_id)
        if current and not current.done():
            return current
        self.database.set_integration_state(
            integration_id,
            "Verbindung wird aufgebaut" if instance.get("enabled") else "Deaktiviert",
            None,
        )
        task = asyncio.create_task(self._run_scheduled_restarts(integration_id))
        self.restart_tasks[integration_id] = task
        task.add_done_callback(lambda finished, key=integration_id: self._restart_finished(key, finished))
        return task

    async def _run_scheduled_restarts(self, integration_id):
        while integration_id in self.pending_restarts:
            instance = self.pending_restarts.pop(integration_id)
            await self.restart_instance(instance)

    def _restart_finished(self, integration_id, task):
        if self.restart_tasks.get(integration_id) is task:
            self.restart_tasks.pop(integration_id, None)
        if not task.cancelled() and task.exception():
            log.error("Geplanter Neustart von %s ist fehlgeschlagen", integration_id, exc_info=task.exception())

    async def cancel_scheduled_restart(self, integration_id):
        self.pending_restarts.pop(integration_id, None)
        task = self.restart_tasks.pop(integration_id, None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def restart_instance(self, instance):
        await self.stop_instance(instance["id"], remove_nodes=False)
        if instance["enabled"]:
            await self.start_instance(instance)
        else:
            self.database.remove_nodes(instance["id"])
            await self.broadcast_snapshot()

    async def start_instance(self, instance):
        context = ModuleContext(self, instance["id"], instance["name"])
        adapter = None
        try:
            adapter = self.registry.create(instance["module_id"], instance["configuration"], context)
            self.adapters[instance["id"]] = adapter
            await adapter.start()
            count = len(self.database.nodes_for_integration(instance["id"]))
            status = getattr(adapter, "startup_status", "Verbunden")
            error = getattr(adapter, "startup_error", None)
            self.database.set_integration_state(instance["id"], status, error, count)
        except Exception as error:
            self.adapters.pop(instance["id"], None)
            if adapter:
                try:
                    await adapter.stop()
                except Exception:
                    log.exception("Fehlgeschlagene Integration %s konnte nicht sauber geschlossen werden", instance["name"])
            self.database.set_integration_state(instance["id"], "Fehler", str(error), 0)
            log.exception("Integration %s konnte nicht gestartet werden", instance["name"])

    async def stop_instance(self, integration_id, remove_nodes=True):
        adapter = self.adapters.pop(integration_id, None)
        if adapter:
            try:
                await adapter.stop()
            except Exception:
                log.exception("Integration %s konnte nicht sauber gestoppt werden", integration_id)
        if remove_nodes:
            self.database.remove_nodes(integration_id)

    async def test_instance(self, instance):
        # Eine bereits laufende Integration darf für einen Verbindungstest nicht
        # ein zweites Mal mit derselben Gerätekennung und demselben Token gestartet
        # werden. Insbesondere homee kann dadurch die bestehende Sitzung und weitere
        # Anmeldungen desselben Benutzers blockieren.
        if instance["id"] in self.adapters:
            adapter = self.adapters[instance["id"]]
            health_check = getattr(adapter, "health_check", None)
            if health_check:
                await health_check()
            return
        context = ModuleContext(self, instance["id"], instance["name"])
        adapter = self.registry.create(instance["module_id"], instance["configuration"], context)
        try:
            await adapter.start()
            await asyncio.sleep(0.2)
        finally:
            await adapter.stop()

    async def publish_node(self, integration_id, node):
        node["integration_source"] = "server"
        current = self.database.integration(integration_id)
        node["integration_id"] = integration_id
        if current:
            node["integration_module"] = current.get("module_id")
            node["integration_name"] = current.get("name")
        previous = next((item for item in self.database.nodes_for_integration(integration_id) if item["id"] == node["id"]), None)
        self._add_last_values(previous, node)
        self.database.save_node(integration_id, node)
        count = len(self.database.nodes_for_integration(integration_id))
        if current:
            self.database.set_integration_state(integration_id, "Verbunden", None, count)
        self.sequence += 1
        self.database.set_setting("sequence", self.sequence)
        await self.broadcast({"type": "node", "sequence": self.sequence, "node": node})
        if self.automation_engine:
            await self.automation_engine.node_changed(previous, node)

    async def set_value(self, node_id, attribute_id, value):
        for integration_id, adapter in self.adapters.items():
            if any(node["id"] == node_id for node in self.database.nodes_for_integration(integration_id)):
                await adapter.set_value(node_id, attribute_id, value)
                return
        raise KeyError("Gerät gehört zu keiner aktiven Serverintegration")

    async def integration_action(self, integration_id, action_id, payload=None):
        adapter = self.adapters.get(integration_id)
        if not adapter:
            raise KeyError("Serverintegration ist nicht aktiv")
        handler = getattr(adapter, "action", None)
        if not handler:
            raise ValueError("Dieses Servermodul bietet keine Aktionen an")
        return await handler(action_id, payload or {})

    async def broadcast_snapshot(self):
        await self.broadcast({"type": "snapshot", "sequence": self.sequence, "nodes": self.database.nodes()})

    async def broadcast(self, payload):
        dead = []
        for websocket in self.websockets:
            try:
                await websocket.send_json(payload)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self.websockets.discard(websocket)

    @staticmethod
    def stable_id(integration_id, external_id, lower, upper):
        digest = hashlib.sha256(f"{integration_id}:{external_id}".encode()).digest()
        return lower + int.from_bytes(digest[:8], "big") % (upper - lower)

    @staticmethod
    def _add_last_values(previous, node):
        old = {item["id"]: item for item in (previous or {}).get("attributes", [])}
        for attribute in node.get("attributes", []):
            prior = old.get(attribute["id"])
            if prior and prior.get("current_value") != attribute.get("current_value"):
                attribute["last_value"] = prior.get("current_value")
            elif prior and "last_value" in prior:
                attribute["last_value"] = prior["last_value"]
