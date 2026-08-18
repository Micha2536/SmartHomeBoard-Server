import asyncio
import json
import time
from urllib.parse import urlparse

import httpx
from server.mdns import resolve_ipv4


def manifest():
    return {
        "id": "go-e", "name": "go-e Wallbox", "version": "1.1.0", "icon": "ev.charger",
        "description": "Lokale go-e HTTP API v2. Liefert Ladeleistung, Energie, Strom, Spannung, Temperatur und Ladefreigabe.",
        "supportsDiscovery": False, "supportsMultipleInstances": True,
        "fields": [
            {"key": "host", "type": "text", "title": "IP-Adresse oder Hostname", "placeholder": "192.168.1.60", "required": True},
            {"key": "port", "type": "port", "title": "HTTP-Port", "default": 80, "minimum": 1, "maximum": 65535},
            {"key": "poll_seconds", "type": "duration", "title": "Abfrageintervall", "default": 10, "minimum": 3, "unit": "s"}
        ]
    }


def create(configuration, context):
    return GoEAdapter(configuration, context)


class GoEAdapter:
    STATUS_KEYS = [
        "fna", "sse", "fwv", "car", "alw", "frc", "amp", "ama", "acu",
        "nrg", "eto", "wh", "err", "tma", "fhz", "cbl", "typ", "var"
    ]

    def __init__(self, configuration, context):
        self.configuration, self.context = configuration, context
        self.task = None
        self.client = httpx.AsyncClient(timeout=8, trust_env=False)
        self.node_id = None
        self.status = None
        self.resolved_host = None

    async def start(self):
        await self._poll()
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
        await self.client.aclose()

    async def set_value(self, node_id, attribute_id, value):
        if node_id != self.node_id:
            raise KeyError("Unbekannte go-e-Wallbox")
        offset = attribute_id - self._attribute_base()
        if offset == 1:
            await self._set("frc", 2 if value >= 0.5 else 1)
        elif offset == 2:
            await self._set("amp", round(value))
        else:
            raise ValueError("Dieses go-e-Attribut ist nicht schreibbar")
        await self._poll()

    async def _loop(self):
        while True:
            await asyncio.sleep(max(3, int(float(self.configuration.get("poll_seconds", 10)))))
            try:
                await self._poll()
                await self.context.set_status("Verbunden")
            except asyncio.CancelledError:
                return
            except Exception as error:
                await self.context.set_status("Nicht erreichbar", str(error))

    async def _poll(self):
        status = await self._request_status()
        self.status = status
        serial = str(status.get("sse") or status.get("wcb") or self.configuration["host"])
        self.node_id = _go_e_node_id(serial)
        await self.context.publish_node(self._node(status, serial))

    async def _request_status(self):
        # Neue Firmware unterstützt eine kommaseparierte Filterliste.
        try:
            modern = await self._status_request(",".join(self.STATUS_KEYS))
            if self._is_status(modern):
                return modern
        except (httpx.HTTPError, ValueError):
            pass

        # Ältere Firmware akzeptiert nur kleine JSON-Filterlisten.
        legacy = {}
        for start in range(0, len(self.STATUS_KEYS), 9):
            try:
                values = await self._status_request(json.dumps(self.STATUS_KEYS[start:start + 9], separators=(",", ":")))
                legacy.update(values)
            except (httpx.HTTPError, ValueError):
                continue
        if self._is_status(legacy):
            return legacy

        # Als letzte Möglichkeit erfolgt ein Vollabruf.
        complete = await self._status_request(None)
        if not self._is_status(complete):
            raise ValueError("Keine gültige go-e API-v2-Antwort. Bitte lokale HTTP API v2 in der go-e-App aktivieren.")
        return complete

    async def _status_request(self, status_filter):
        params = {"filter": status_filter} if status_filter else None
        response = await self.client.get(f"{await self._base_url()}/api/status", params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("go-e hat kein JSON-Objekt geliefert")
        return payload

    @staticmethod
    def _is_status(payload):
        return any(key in payload for key in ("sse", "car", "fwv", "amp"))

    async def _set(self, key, value):
        response = await self.client.get(f"{await self._base_url()}/api/set", params={key: value})
        response.raise_for_status()
        payload = response.json()
        if payload.get(key) is False:
            raise ValueError(f"go-e hat {key} nicht übernommen")

    async def _base_url(self):
        host = str(self.configuration.get("host", "")).strip()
        if not host:
            raise ValueError("IP-Adresse oder Hostname fehlt")
        parsed = urlparse(host if "://" in host else f"http://{host}")
        if not parsed.hostname:
            raise ValueError("IP-Adresse oder Hostname ist ungültig")
        target = parsed.hostname
        if target.lower().endswith(".local"):
            if not self.resolved_host:
                self.resolved_host = await resolve_ipv4(target)
            target = self.resolved_host
        port = int(float(self.configuration.get("port", 80)))
        return f"{parsed.scheme}://{target}:{port}"

    def _node(self, status, serial):
        node = self.node_id
        energy = _numbers(status.get("nrg"))
        power = energy[11] if len(energy) > 11 else 0
        currents = energy[4:7] if len(energy) > 6 else []
        voltages = [v for v in energy[0:3] if v > 0]
        temperatures = _numbers(status.get("tma"))
        force = int(_number(status.get("frc")))
        enabled = 0 if force == 1 else (1 if force == 2 else _number(status.get("alw")))
        maximum = max(6, _number(status.get("ama") or status.get("var") or 32))
        now = time.time()
        def attr(offset, kind, name, value, unit="", editable=False, minimum=None, maximum_value=None, step=None, data=None):
            item = {"id": self._attribute_base() + offset, "node_id": node, "type": kind, "name": name, "unit": unit,
                    "current_value": value, "editable": editable, "last_changed": now}
            if editable: item["target_value"] = value
            if minimum is not None: item["minimum"] = minimum
            if maximum_value is not None: item["maximum"] = maximum_value
            if step is not None: item["step_value"] = step
            if data is not None: item["data"] = data
            return item
        car = int(_number(status.get("car")))
        errors = int(_number(status.get("err")))
        return {
            "id": node, "integration_source": "server", "name": str(status.get("fna") or self.context.integration_name),
            "note": f"Server · go-e · {serial}", "state": 1 if errors == 0 else 2, "profile": 0, "protocol": 20,
            "image": "ev.charger.fill", "state_changed": now,
            "attributes": [
                attr(1, 1, "Ladefreigabe", enabled, editable=True, minimum=0, maximum_value=1, step=1),
                attr(2, 22, "Ladestrom", _number(status.get("amp") or status.get("acu") or 6), "A", True, 6, maximum, 1),
                attr(3, 213, "Fahrzeugstatus", car, data={0: "Unbekannt", 1: "Bereit", 2: "Lädt", 3: "Wartet", 4: "Fertig"}.get(car, str(car))),
                attr(4, 3, "Ladeleistung", power, "W"),
                attr(5, 239, "Energie seit Anstecken", _number(status.get("wh")) / 1000, "kWh"),
                attr(6, 4, "Gesamtenergie", _number(status.get("eto")) / 1000, "kWh"),
                attr(7, 193, "Aktueller Strom", max([abs(v) for v in currents], default=_number(status.get("acu"))), "A"),
                attr(8, 195, "Spannung", sum(voltages) / len(voltages) if voltages else 0, "V"),
                attr(9, 92, "Gerätetemperatur", max(temperatures, default=0), "°C"),
                attr(10, 70, "Fehler", 0 if errors == 0 else 1, data="Kein Fehler" if errors == 0 else f"Fehler {errors}"),
                attr(11, 194, "Netzfrequenz", _number(status.get("fhz")), "Hz")
            ]
        }

    def _attribute_base(self):
        return (self.node_id - 1_700_000_000) * 16 + 1_700_000_000


def _number(value):
    try: return float(value or 0)
    except (TypeError, ValueError): return 0


def _numbers(value):
    return [_number(item) for item in value] if isinstance(value, list) else []


def _go_e_node_id(value):
    result = 2_166_136_261
    for byte in str(value).lower().encode():
        result = ((result ^ byte) * 16_777_619) & 0xFFFFFFFF
    return 1_700_000_000 + result % 5_000_000
