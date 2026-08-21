import asyncio
import hashlib
import logging
import time

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
        self.pending_commands = {}

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
        custom_names = self.database.setting("node_custom_names", {}) or {}
        custom_name = str(custom_names.get(str(node["id"]), "")).strip()
        if custom_name:
            node["name"] = custom_name
        node["dashboard_enabled"] = self.device_dashboard_enabled(node["id"])
        previous = next((item for item in self.database.nodes_for_integration(integration_id) if item["id"] == node["id"]), None)
        self._add_last_values(previous, node)
        self._record_observed_control_changes(previous, node)
        self.database.save_node(integration_id, node)
        count = len(self.database.nodes_for_integration(integration_id))
        if current:
            self.database.set_integration_state(integration_id, "Verbunden", None, count)
        self.sequence += 1
        self.database.set_setting("sequence", self.sequence)
        if node["dashboard_enabled"]:
            await self.broadcast({"type": "node", "sequence": self.sequence, "node": node})
        if self.automation_engine:
            await self.automation_engine.node_changed(previous, node)

    async def rename_node(self, node_id, name):
        node = self.database.node(int(node_id))
        if not node:
            raise KeyError("Servergerät wurde nicht gefunden")
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("Der Gerätename darf nicht leer sein")
        normalized_name = normalized_name[:120]
        integration_id = str(node.get("integration_id", ""))
        if not integration_id:
            raise KeyError("Servergerät besitzt keine Integrationszuordnung")
        custom_names = self.database.setting("node_custom_names", {}) or {}
        custom_names[str(node["id"])] = normalized_name
        self.database.set_setting("node_custom_names", custom_names)
        node["name"] = normalized_name
        self.database.save_node(integration_id, node)
        self.sequence += 1
        self.database.set_setting("sequence", self.sequence)
        await self.broadcast({"type": "node", "sequence": self.sequence, "node": node})
        return node

    def device_dashboard_enabled(self, node_id):
        settings = self.database.setting("dashboard_device_enabled", {}) or {}
        return bool(settings.get(str(int(node_id)), True))

    def visible_nodes(self):
        settings = self.database.setting("dashboard_device_enabled", {}) or {}
        return [node for node in self.database.nodes() if bool(settings.get(str(int(node["id"])), True))]

    async def set_device_dashboard_enabled(self, node_id, enabled):
        node = self.database.node(int(node_id))
        if not node:
            raise KeyError("Servergerät wurde nicht gefunden")
        settings = self.database.setting("dashboard_device_enabled", {}) or {}
        settings[str(int(node_id))] = bool(enabled)
        self.database.set_setting("dashboard_device_enabled", settings)
        node["dashboard_enabled"] = bool(enabled)
        self.database.save_node(str(node.get("integration_id", "")), node)
        self.sequence += 1
        self.database.set_setting("sequence", self.sequence)
        await self.broadcast_snapshot()
        return node

    async def set_value(self, node_id, attribute_id, value, source="server_api", source_detail="", client_id="", metadata=None):
        node = self.database.node(int(node_id))
        if not node:
            raise KeyError("Servergerät wurde nicht gefunden")
        integration_id = str(node.get("integration_id", ""))
        adapter = self.adapters.get(integration_id)
        if not adapter:
            raise KeyError("Gerät gehört zu keiner aktiven Serverintegration")
        attribute = next((item for item in node.get("attributes", []) if int(item.get("id", 0)) == int(attribute_id)), None)
        if not attribute:
            raise KeyError("Geräteattribut wurde nicht gefunden")
        previous = attribute.get("current_value", attribute.get("target_value"))
        audit = {
            "event_kind": "command", "status": "requested", "source": source,
            "source_detail": source_detail, "client_id": client_id,
            "node_id": node_id, "node_name": node.get("name", ""),
            "attribute_id": attribute_id, "attribute_name": attribute.get("name", ""),
            "integration_id": integration_id, "integration_module": node.get("integration_module", ""),
            "previous_value": previous, "requested_value": value, "metadata": metadata or {},
        }
        audit_id = self.database.add_command_audit(audit)
        key = (int(node_id), int(attribute_id))
        self.pending_commands[key] = {**audit, "audit_id": audit_id, "expires_at": time.monotonic() + 15}
        try:
            await adapter.set_value(node_id, attribute_id, value)
            if key in self.pending_commands:
                self.database.update_command_audit(audit_id, "sent")
        except Exception as error:
            self.pending_commands.pop(key, None)
            self.database.update_command_audit(audit_id, "failed", str(error))
            raise

    async def attribute_history(self, node_id, attribute_id, from_timestamp, till_timestamp):
        node = self.database.node(int(node_id))
        if not node:
            raise KeyError("Servergerät wurde nicht gefunden")
        integration_id = str(node.get("integration_id", ""))
        adapter = self.adapters.get(integration_id)
        if not adapter:
            raise KeyError("Serverintegration ist nicht aktiv")
        handler = getattr(adapter, "attribute_history", None)
        if not handler:
            raise ValueError("Diese Serverintegration stellt keinen Verlauf bereit")
        return await handler(node_id, attribute_id, from_timestamp, till_timestamp)

    async def integration_action(self, integration_id, action_id, payload=None):
        adapter = self.adapters.get(integration_id)
        if not adapter:
            raise KeyError("Serverintegration ist nicht aktiv")
        handler = getattr(adapter, "action", None)
        if not handler:
            raise ValueError("Dieses Servermodul bietet keine Aktionen an")
        return await handler(action_id, payload or {})

    async def broadcast_snapshot(self):
        await self.broadcast({"type": "snapshot", "sequence": self.sequence, "nodes": self.visible_nodes()})

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

    def _record_observed_control_changes(self, previous, node):
        if not previous:
            return
        old = {int(item.get("id", 0)): item for item in previous.get("attributes", [])}
        now = time.monotonic()
        self.pending_commands = {
            key: item for key, item in self.pending_commands.items()
            if float(item.get("expires_at", 0)) > now
        }
        for attribute in node.get("attributes", []):
            attribute_id = int(attribute.get("id", 0))
            prior = old.get(attribute_id)
            if not prior:
                continue
            before = prior.get("current_value")
            after = attribute.get("current_value")
            if before is None or after is None or before == after:
                continue
            is_control = bool(attribute.get("editable") or prior.get("editable")) or int(attribute.get("type", 0) or 0) in {1, 14, 15}
            if not is_control:
                continue
            key = (int(node.get("id", 0)), attribute_id)
            command = self.pending_commands.pop(key, None)
            source = command.get("source", "device_or_external") if command else "device_or_external"
            detail = command.get("source_detail", "Keine zugehörige SHB-Schaltanforderung erkannt") if command else "Keine zugehörige SHB-Schaltanforderung erkannt"
            self.database.add_command_audit({
                "event_kind": "state_change", "status": "observed", "source": source,
                "source_detail": detail, "client_id": command.get("client_id", "") if command else "",
                "node_id": node.get("id", 0), "node_name": node.get("name", ""),
                "attribute_id": attribute_id, "attribute_name": attribute.get("name", ""),
                "integration_id": node.get("integration_id", ""),
                "integration_module": node.get("integration_module", ""),
                "previous_value": before,
                "requested_value": command.get("requested_value") if command else None,
                "observed_value": after,
                "metadata": {"command_audit_id": command.get("audit_id")} if command else {},
            })
            if command:
                self.database.update_command_audit(command["audit_id"], "confirmed", observed_value=after)
