import asyncio
import time


def manifest():
    return {
        "id": "demo", "name": "Demo-Gerät", "version": "1.0.0", "icon": "testtube.2",
        "description": "Testmodul für Server, Liveupdates und dynamische Formulare.",
        "supportsDiscovery": False, "supportsMultipleInstances": True,
        "fields": [
            {"key": "initial_value", "type": "number", "title": "Startwert", "default": 21.5},
            {"key": "poll_seconds", "type": "duration", "title": "Aktualisierung", "default": 5, "minimum": 2, "unit": "s"}
        ]
    }


def create(configuration, context):
    return DemoAdapter(configuration, context)


class DemoAdapter:
    def __init__(self, configuration, context):
        self.configuration, self.context = configuration, context
        self.value = float(configuration.get("initial_value", 21.5))
        self.node_id = context.stable_node_id("demo")
        self.task = None

    async def start(self):
        await self.publish()
        self.task = asyncio.create_task(self.loop())

    async def stop(self):
        if self.task: self.task.cancel()

    async def set_value(self, node_id, attribute_id, value):
        if node_id != self.node_id or attribute_id != self.node_id * 100 + 2:
            raise ValueError("Unbekanntes Demo-Attribut")
        self.value = value
        await self.publish()

    async def loop(self):
        while True:
            await asyncio.sleep(max(2, int(float(self.configuration.get("poll_seconds", 5)))))
            await self.publish()

    async def publish(self):
        now = time.time()
        await self.context.publish_node({
            "id": self.node_id, "integration_source": "server", "name": self.context.integration_name,
            "note": "Server · Demo-Modul", "state": 1, "profile": 3009, "protocol": 20,
            "image": "thermometer.medium", "state_changed": now,
            "attributes": [
                {"id": self.node_id * 100 + 1, "node_id": self.node_id, "type": 5, "name": "Temperatur", "unit": "°C", "current_value": self.value, "editable": False, "last_changed": now},
                {"id": self.node_id * 100 + 2, "node_id": self.node_id, "type": 6, "name": "Sollwert", "unit": "°C", "current_value": self.value, "target_value": self.value, "editable": True, "minimum": 5, "maximum": 35, "step_value": 0.5, "last_changed": now}
            ]
        })
