import asyncio
import contextlib
import json
import logging
import time


log = logging.getLogger("smarthomeboard.roborock")


def manifest():
    return {
        "id": "roborock",
        "name": "Roborock",
        "version": "1.2.3",
        "icon": "fan",
        "description": (
            "Dauerhafte Roborock-Cloud-Verbindung mit E-Mail-Code-Anmeldung. "
            "Staubsauger, Status und Steuerung werden serverseitig gespeichert und an alle Apps verteilt."
        ),
        "supportsDiscovery": False,
        "supportsMultipleInstances": True,
        "fields": [
            {
                "key": "email",
                "type": "text",
                "title": "Roborock-E-Mail-Adresse",
                "placeholder": "name@example.com",
                "help": "Dieselbe E-Mail-Adresse wie in der Roborock-App.",
                "required": True,
            },
            {
                "key": "verification_code",
                "type": "text",
                "title": "Einmaliger Anmeldecode",
                "placeholder": "Code aus der E-Mail",
                "help": "Zuerst speichern und den Code anfordern. Danach den Code hier eintragen und erneut speichern.",
                "required": False,
            },
            {
                "key": "poll_seconds",
                "type": "duration",
                "title": "Aktualisierung",
                "default": 30,
                "minimum": 15,
                "maximum": 3600,
                "unit": "s",
            },
        ],
        "actions": [
            {"id": "request_code", "title": "Anmeldecode per E-Mail senden", "icon": "envelope.badge"},
            {"id": "refresh", "title": "Geräte jetzt aktualisieren", "icon": "arrow.clockwise"},
            {"id": "logout", "title": "Roborock-Anmeldung zurücksetzen", "icon": "rectangle.portrait.and.arrow.right", "role": "destructive"},
        ],
    }


def create(configuration, context):
    return RoborockAdapter(configuration, context)


class RoborockAdapter:
    CLEANING_STATES = {
        "cleaning", "spot_cleaning", "zoned_cleaning", "segment_cleaning",
        "robot_status_mopping", "clean_mop_cleaning", "clean_mop_mopping",
        "segment_mopping", "segment_clean_mop_cleaning", "segment_clean_mop_mopping",
        "zoned_mopping", "zoned_clean_mop_cleaning", "zoned_clean_mop_mopping",
        "sweeping", "mopping", "sweep_and_mop",
    }
    DOCK_STATES = {
        "returning_home", "docking", "charging", "charging_complete", "waiting_to_charge",
    }

    def __init__(self, configuration, context):
        self.configuration = configuration
        self.context = context
        self.manager = None
        self.devices = {}
        self.node_devices = {}
        self.controls = {}
        self.task = None
        self.operation_lock = asyncio.Lock()
        self.startup_status = "Anmeldung erforderlich"
        self.startup_error = None
        persisted = self.context.load_state({}) or {}
        self.selected_targets = (
            dict(persisted.get("selected_targets", {}))
            if isinstance(persisted, dict) and isinstance(persisted.get("selected_targets", {}), dict)
            else {}
        )

    async def start(self):
        email = self._email()
        if not email:
            self.startup_error = "Roborock-E-Mail-Adresse fehlt"
            return

        session = self.context.load_secret("session", {})
        code = str(self.configuration.get("verification_code", "")).strip()
        if code:
            await self._login(email, code)
            session = self.context.load_secret("session", {})

        if not self._session_matches(session, email):
            self.startup_status = "Anmeldung erforderlich"
            self.startup_error = "Bitte Anmeldecode anfordern und anschließend speichern."
            return

        await self._connect(session)
        await self._refresh()
        self.startup_status = "Verbunden"
        self.startup_error = None
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if self.manager:
            await self.manager.close()
            self.manager = None
        self.devices.clear()
        self.node_devices.clear()
        self.controls.clear()

    async def health_check(self):
        if not self.manager:
            raise ValueError(self.startup_error or "Roborock-Anmeldung ist noch nicht abgeschlossen")
        await self._refresh()

    async def action(self, action_id, payload):
        if action_id == "request_code":
            await self._request_code()
            return {"message": "Der Roborock-Anmeldecode wurde per E-Mail versendet."}
        if action_id == "refresh":
            if not self.manager:
                raise ValueError("Zuerst die Roborock-Anmeldung abschließen")
            for device in list(self.devices.values()):
                with contextlib.suppress(Exception):
                    await self._discover_controls(device)
            await self._refresh()
            return {"message": "Roborock-Geräte wurden aktualisiert."}
        if action_id == "logout":
            if self.task:
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
                self.task = None
            if self.manager:
                await self.manager.close()
                self.manager = None
            self.context.save_secret("session", {})
            self.context.save_secret("pending_login", {})
            for node in list(self.context.nodes()):
                await self.context.remove_node(node["id"])
            self.devices.clear()
            self.node_devices.clear()
            self.controls.clear()
            self.startup_status = "Anmeldung erforderlich"
            self.startup_error = "Roborock-Anmeldung wurde zurückgesetzt."
            await self.context.set_status(self.startup_status, self.startup_error)
            return {"message": "Roborock-Anmeldung wurde zurückgesetzt."}
        if action_id == "automation_cleaning":
            return await self._automation_cleaning(payload)
        raise ValueError("Unbekannte Roborock-Aktion")

    async def _automation_cleaning(self, payload):
        node_id = int(payload.get("nodeID", 0))
        device = self.node_devices.get(node_id)
        if not device:
            raise KeyError("Der Roborock der Automation ist nicht verbunden")
        target = str(payload.get("roborockTarget", "complete"))
        target_value = int(round(float(payload.get("roborockTargetValue", -1))))
        if target in {"room", "routine"} and target_value < 0:
            raise ValueError("Für die Roborock-Automation wurde kein Raum beziehungsweise keine Routine ausgewählt")

        async with self.operation_lock:
            settings = (
                ("roborockCleaningType", self._set_cleaning_type),
                ("roborockSuction", self._set_suction),
                ("roborockWater", self._set_water),
            )
            for key, setter in settings:
                value = payload.get(key)
                if value is None:
                    continue
                await setter(device, int(round(float(value))))
                await asyncio.sleep(0.2)

            if target == "complete":
                await self._set_cleaning(device, True, use_selected_target=False)
            elif target == "room":
                await self._clean_rooms(device, [target_value])
            elif target == "routine":
                await self._execute_routine(device, target_value)
            elif target == "spot":
                await self._spot_clean(device)
            else:
                raise ValueError("Unbekanntes Roborock-Reinigungsziel")

            await asyncio.sleep(0.8)
            await self._refresh_device(device)
        return {"message": "Roborock-Automation wurde vollständig ausgeführt."}

    async def set_value(self, node_id, attribute_id, value):
        device = self.node_devices.get(node_id)
        if not device:
            raise KeyError("Unbekannter Roborock")
        offset = attribute_id - self.context.attribute_id(node_id, 0)
        if offset == 1:
            await self._set_cleaning(device, float(value) >= 0.5)
        elif offset == 2:
            if float(value) < 0.5:
                raise ValueError("Die Rückkehr zur Ladestation kann nur gestartet werden")
            await self._return_to_dock(device)
        elif offset == 10:
            await self._set_cleaning_type(device, int(round(float(value))))
        elif offset == 11:
            await self._set_suction(device, int(round(float(value))))
        elif offset == 12:
            await self._set_water(device, int(round(float(value))))
        elif offset == 13:
            self._select_target(device, "room", int(round(float(value))))
        elif offset == 14:
            self._select_target(device, "routine", int(round(float(value))))
        elif offset == 15:
            if float(value) < 0.5:
                raise ValueError("Stop kann nur ausgelöst werden")
            await self._stop_cleaning(device)
        elif offset == 16:
            if float(value) < 0.5:
                raise ValueError("Punktreinigung kann nur gestartet werden")
            await self._spot_clean(device)
        else:
            raise ValueError("Dieses Roborock-Attribut ist nicht schreibbar")
        await asyncio.sleep(0.8)
        await self._refresh_device(device)

    async def _request_code(self):
        email = self._email()
        if not email or "@" not in email:
            raise ValueError("Bitte zuerst eine gültige Roborock-E-Mail-Adresse speichern")
        from roborock.web_api import RoborockApiClient

        client = RoborockApiClient(username=email)
        try:
            await client.request_code_v4()
            pending_login = {
                "email": email,
                "base_url": await client.base_url,
                "country": await client.country,
                "country_code": await client.country_code,
                "device_identifier": client._device_identifier,
            }
        except Exception as error:
            raise ValueError(_friendly_error(error)) from error
        self.context.save_secret("pending_login", pending_login)
        self.startup_status = "Code versendet"
        self.startup_error = "Code aus der Roborock-E-Mail eintragen und erneut speichern."
        await self.context.set_status(self.startup_status, self.startup_error)

    async def _login(self, email, code):
        from roborock.web_api import RoborockApiClient

        pending = self.context.load_secret("pending_login", {})
        if not isinstance(pending, dict) or pending.get("email") != email:
            pending = {}
        client = RoborockApiClient(username=email, base_url=pending.get("base_url") or None)
        if pending.get("device_identifier"):
            # Roborock ties the login attempt to the client identifier used when
            # requesting the mail code. Keep it stable across the server restart.
            client._device_identifier = str(pending["device_identifier"])
        try:
            user_data = await client.code_login_v4(
                code,
                country=pending.get("country"),
                country_code=pending.get("country_code"),
            )
            base_url = await client.base_url
        except Exception as error:
            raise ValueError(_friendly_error(error)) from error
        self.context.save_secret("session", {
            "email": email,
            "base_url": base_url,
            "user_data": user_data.as_dict(),
        })
        self.context.save_secret("pending_login", {})
        self.context.clear_configuration_value("verification_code")

    async def _connect(self, session):
        from roborock.data import UserData
        from roborock.devices.device_manager import UserParams, create_device_manager

        user_data = UserData.from_dict(session["user_data"])
        if not user_data:
            raise ValueError("Gespeicherte Roborock-Sitzung ist ungültig. Bitte Anmeldung zurücksetzen.")
        try:
            self.manager = await create_device_manager(
                UserParams(
                    username=self._email(),
                    user_data=user_data,
                    base_url=session.get("base_url") or None,
                ),
                prefer_cache=False,
            )
            discovered = await self.manager.get_devices()
        except Exception as error:
            if self.manager:
                with contextlib.suppress(Exception):
                    await self.manager.close()
                self.manager = None
            raise ValueError(_friendly_error(error)) from error
        self.devices = {str(device.duid): device for device in discovered}
        for device in discovered:
            try:
                await self._discover_controls(device)
            except Exception as error:
                log.info("Zusatzfunktionen von Roborock %s konnten nicht vollständig geladen werden: %s", device.name, error)

    async def _loop(self):
        while True:
            try:
                await asyncio.sleep(self._poll_seconds())
                await self._refresh()
                await self.context.set_status("Verbunden")
            except asyncio.CancelledError:
                return
            except Exception as error:
                log.warning("Roborock-Aktualisierung fehlgeschlagen: %s", error)
                await self.context.set_status("Verbindung unterbrochen", _friendly_error(error))

    async def _refresh(self):
        async with self.operation_lock:
            for device in list(self.devices.values()):
                try:
                    await self._refresh_device(device)
                except Exception as error:
                    log.warning("Roborock %s konnte nicht aktualisiert werden: %s", device.name, error)
                    await self.context.publish_node(self._node(device, None, error))

    async def _refresh_device(self, device):
        status = None
        if device.v1_properties:
            await device.v1_properties.status.refresh()
            status = device.v1_properties.status
        elif device.b01_q10_properties:
            await device.b01_q10_properties.refresh()
            await asyncio.sleep(0.35)
            status = device.b01_q10_properties.status
            self._update_q10_rooms(device)
        node = self._node(device, status)
        self.node_devices[node["id"]] = device
        await self.context.publish_node(node)

    async def _set_cleaning(self, device, enabled, use_selected_target=True):
        if enabled and use_selected_target:
            selected = self.selected_targets.get(str(device.duid), {})
            kind = str(selected.get("kind", "complete"))
            if kind == "room":
                await self._clean_rooms(device, [int(selected.get("room", -1))])
                return
            if kind == "routine":
                await self._execute_routine(device, int(selected.get("routine", -1)))
                return
        if device.v1_properties:
            command = "app_start" if enabled else "app_pause"
            await device.v1_properties.command.send(command)
            return
        if device.b01_q10_properties:
            vacuum = device.b01_q10_properties.vacuum
            if enabled:
                status_name = _enum_name(device.b01_q10_properties.status.status)
                if status_name == "paused":
                    await vacuum.resume_clean()
                else:
                    await vacuum.start_clean()
            else:
                await vacuum.pause_clean()
            return
        raise ValueError("Dieses Roborock-Modell unterstützt die Reinigungssteuerung noch nicht")

    async def _return_to_dock(self, device):
        if device.v1_properties:
            await device.v1_properties.command.send("app_charge")
            return
        if device.b01_q10_properties:
            await device.b01_q10_properties.vacuum.return_to_dock()
            return
        raise ValueError("Dieses Roborock-Modell unterstützt die Ladestationssteuerung noch nicht")

    async def _stop_cleaning(self, device):
        if device.v1_properties:
            await device.v1_properties.command.send("app_stop")
            return
        if device.b01_q10_properties:
            await device.b01_q10_properties.vacuum.stop_clean()
            return
        raise ValueError("Dieses Roborock-Modell unterstützt Stop noch nicht")

    async def _spot_clean(self, device):
        if device.v1_properties:
            await device.v1_properties.command.send("app_spot")
            return
        if device.b01_q10_properties:
            await device.b01_q10_properties.vacuum.spot_clean()
            return
        raise ValueError("Dieses Roborock-Modell unterstützt Punktreinigung noch nicht")

    async def _set_cleaning_type(self, device, choice):
        control = self._control(device)
        option = _option_by_value(control.get("cleaning_types", []), choice)
        if not option:
            raise ValueError("Diese Reinigungsart wird vom Gerät nicht angeboten")
        if device.v1_properties:
            await device.v1_properties.status.set_cleaning_mode(option["command"])
            return
        if device.b01_q10_properties:
            from roborock.data.b01_q10.b01_q10_code_mappings import YXCleanType

            mode = YXCleanType.from_code_optional(int(option["command"]))
            if mode is None:
                raise ValueError("Unbekannte Reinigungsart")
            await device.b01_q10_properties.vacuum.set_clean_mode(mode)
            return
        raise ValueError("Dieses Roborock-Modell unterstützt Reinigungsarten noch nicht")

    async def _set_suction(self, device, choice):
        option = _option_by_value(self._control(device).get("suction", []), choice)
        if not option:
            raise ValueError("Diese Saugstufe wird vom Gerät nicht angeboten")
        if device.v1_properties:
            await device.v1_properties.command.send("set_custom_mode", [int(option["command"])])
            return
        if device.b01_q10_properties:
            from roborock.data.b01_q10.b01_q10_code_mappings import YXFanLevel

            level = YXFanLevel.from_code_optional(int(option["command"]))
            if level is None:
                raise ValueError("Unbekannte Saugstufe")
            await device.b01_q10_properties.vacuum.set_fan_level(level)
            return
        raise ValueError("Dieses Roborock-Modell unterstützt Saugstufen noch nicht")

    async def _set_water(self, device, choice):
        option = _option_by_value(self._control(device).get("water", []), choice)
        if not option:
            raise ValueError("Diese Wassermenge wird vom Gerät nicht angeboten")
        if device.v1_properties:
            await device.v1_properties.command.send("set_water_box_custom_mode", [int(option["command"])])
            return
        if device.b01_q10_properties:
            from roborock.data.b01_q10.b01_q10_code_mappings import B01_Q10_DP

            await device.b01_q10_properties.command.send(B01_Q10_DP.WATER_LEVEL, int(option["command"]))
            return
        raise ValueError("Dieses Roborock-Modell unterstützt Wassermengen noch nicht")

    async def _clean_rooms(self, device, segments):
        available = {int(item["value"]) for item in self._control(device).get("rooms", [])}
        selected = [int(item) for item in segments if int(item) in available]
        if not selected:
            raise ValueError("Der ausgewählte Raum ist nicht mehr verfügbar")
        if device.v1_properties:
            # python-roborock 6.x erwartet für APP_SEGMENT_CLEAN ein Objekt
            # innerhalb der Parameterliste. Das frühere [[segment_id]] wird
            # von aktuellen Geräten teilweise ohne Fehlerantwort verworfen.
            await device.v1_properties.command.send("app_segment_clean", [{"segments": selected}])
        elif device.b01_q10_properties:
            await device.b01_q10_properties.vacuum.clean_segments(selected)
        else:
            raise ValueError("Dieses Roborock-Modell unterstützt Raumreinigung noch nicht")
        self._remember_target(device, "room", selected[0])

    async def _execute_routine(self, device, routine_id):
        available = {int(item["value"]) for item in self._control(device).get("routines", [])}
        if routine_id not in available:
            raise ValueError("Die ausgewählte Routine ist nicht mehr verfügbar")
        if not device.v1_properties:
            raise ValueError("Routinen werden von diesem Roborock-Protokoll noch nicht unterstützt")
        await device.v1_properties.routines.execute_routine(routine_id)
        self._remember_target(device, "routine", routine_id)

    async def _discover_controls(self, device):
        control = {"cleaning_types": [], "suction": [], "water": [], "rooms": [], "routines": []}
        if device.v1_properties:
            status = device.v1_properties.status
            control["cleaning_types"] = [
                _choice(index, _translate_mode(option.value), option.value)
                for index, option in enumerate(status.cleaning_mode_options)
            ]
            control["suction"] = [
                _choice(option.code, _translate_mode(option.value), option.code)
                for option in status.fan_speed_options
            ]
            control["water"] = [
                _choice(option.code, _translate_mode(option.value), option.code)
                for option in status.water_mode_options
            ]
            with contextlib.suppress(Exception):
                await device.v1_properties.rooms.refresh()
                control["rooms"] = [
                    _choice(room.segment_id, room.name, room.segment_id)
                    for room in (device.v1_properties.rooms.rooms or [])
                ]
            with contextlib.suppress(Exception):
                routines = await device.v1_properties.routines.get_routines()
                control["routines"] = [
                    _choice(routine.id, routine.name, routine.id) for routine in routines
                ]
        elif device.b01_q10_properties:
            from roborock.data.b01_q10.b01_q10_code_mappings import YXCleanType, YXFanLevel, YXWaterLevel

            control["cleaning_types"] = _enum_choices(YXCleanType)
            control["suction"] = _enum_choices(YXFanLevel)
            control["water"] = _enum_choices(YXWaterLevel)
        self.controls[str(device.duid)] = control
        self._update_q10_rooms(device)

    def _update_q10_rooms(self, device):
        if not device.b01_q10_properties:
            return
        rooms = getattr(device.b01_q10_properties.map, "rooms", []) or []
        if rooms:
            self._control(device)["rooms"] = [_choice(room.id, room.name, room.id) for room in rooms]

    def _control(self, device):
        return self.controls.setdefault(str(device.duid), {
            "cleaning_types": [], "suction": [], "water": [], "rooms": [], "routines": [],
        })

    def _node(self, device, status, refresh_error=None):
        node_id = self.context.stable_node_id(str(device.duid))
        now = time.time()
        status_name = self._status_name(status)
        translated_status = _translate_status(status_name)
        online = bool(getattr(device, "is_connected", False)) and refresh_error is None
        battery = _number(getattr(status, "battery", None))
        clean_time = _number(getattr(status, "clean_time", None))
        clean_area = _number(getattr(status, "clean_area", None))
        is_q10 = bool(device.b01_q10_properties)
        if not is_q10:
            clean_time /= 60
            clean_area /= 1_000_000
        error_text = _status_error(status)
        if refresh_error:
            error_text = _friendly_error(refresh_error)
        fan = getattr(status, "fan_speed_name", None) or _enum_label(getattr(status, "fan_level", None))
        if not fan:
            fan = _text(getattr(status, "fan_power", None))
        water = getattr(status, "water_mode_name", None) or _enum_label(getattr(status, "water_level", None))
        cleaning = status_name in self.CLEANING_STATES
        docked = status_name in self.DOCK_STATES
        control = self._control(device)
        cleaning_type_name = getattr(status, "current_cleaning_mode_name", None) or _enum_label(getattr(status, "clean_mode", None))
        cleaning_type_value = _choice_value_for_label(control.get("cleaning_types", []), cleaning_type_name)
        fan_value = _number(getattr(status, "fan_power", None))
        if is_q10:
            fan_value = _enum_code(getattr(status, "fan_level", None))
        water_value = _number(getattr(status, "water_box_mode", None))
        if is_q10:
            water_value = _enum_code(getattr(status, "water_level", None))

        def attr(offset, kind, name, value, unit="", editable=False, data=None, minimum=None, maximum=None, step=None):
            item = {
                "id": self.context.attribute_id(node_id, offset),
                "node_id": node_id,
                "type": kind,
                "name": name,
                "unit": unit,
                "current_value": value,
                "editable": editable,
                "last_changed": now,
            }
            if editable:
                item["target_value"] = value
            if data is not None:
                item["data"] = data
            if minimum is not None:
                item["minimum"] = minimum
            if maximum is not None:
                item["maximum"] = maximum
            if step is not None:
                item["step_value"] = step
            return item

        attributes = [
            attr(1, 1, "Reinigung", 1 if cleaning else 0, editable=True, minimum=0, maximum=1, step=1),
            attr(2, 1, "Ladestation", 1 if docked else 0, editable=True, minimum=0, maximum=1, step=1),
            attr(3, 8, "Akkustand", battery, "%"),
            attr(4, 213, "Status", 1 if online else 0, "text", data=translated_status if online else "Nicht erreichbar"),
            attr(5, 70, "Fehler", 0 if not error_text else 1, "text", data=error_text or "Kein Fehler"),
        ]
        if control.get("cleaning_types"):
            attributes.append(attr(
                10, 213, "Reinigungsart", cleaning_type_value, "choice", True,
                data=_choice_data(cleaning_type_value, control["cleaning_types"]),
                minimum=min(item["value"] for item in control["cleaning_types"]),
                maximum=max(item["value"] for item in control["cleaning_types"]), step=1,
            ))
        if control.get("suction"):
            attributes.append(attr(
                11, 213, "Saugstufe", fan_value, "choice", True,
                data=_choice_data(fan_value, control["suction"]),
                minimum=min(item["value"] for item in control["suction"]),
                maximum=max(item["value"] for item in control["suction"]), step=1,
            ))
        if control.get("water"):
            attributes.append(attr(
                12, 213, "Wassermenge", water_value, "choice", True,
                data=_choice_data(water_value, control["water"]),
                minimum=min(item["value"] for item in control["water"]),
                maximum=max(item["value"] for item in control["water"]), step=1,
            ))
        if control.get("rooms"):
            room_options = [_choice(-1, "Gesamte Fläche", -1, "complete"), *control["rooms"]]
            selected_room = self._selected_target(device, "room", room_options)
            attributes.append(attr(
                13, 213, "Raum auswählen", selected_room, "choice", True,
                data=_choice_data(selected_room, room_options, "Gesamte Fläche"),
                minimum=min(item["value"] for item in room_options),
                maximum=max(item["value"] for item in room_options), step=1,
            ))
        if control.get("routines"):
            routine_options = [_choice(-1, "Gesamte Fläche", -1, "complete"), *control["routines"]]
            selected_routine = self._selected_target(device, "routine", routine_options)
            attributes.append(attr(
                14, 213, "Routine auswählen", selected_routine, "choice", True,
                data=_choice_data(selected_routine, routine_options, "Gesamte Fläche"),
                minimum=min(item["value"] for item in routine_options),
                maximum=max(item["value"] for item in routine_options), step=1,
            ))
        attributes.extend([
            attr(15, 1, "Reinigung stoppen", 0, editable=True, minimum=0, maximum=1, step=1),
            attr(16, 1, "Punktreinigung", 0, editable=True, minimum=0, maximum=1, step=1),
        ])
        if clean_area:
            attributes.append(attr(6, 222, "Gereinigte Fläche", round(clean_area, 1), "m²"))
        if clean_time:
            attributes.append(attr(7, 214, "Reinigungszeit", round(clean_time, 1), "min"))
        if fan and not control.get("suction"):
            attributes.append(attr(8, 213, "Saugstufe", 0, "text", data=_translate_mode(fan)))
        if water and not control.get("water"):
            attributes.append(attr(9, 213, "Wassermenge", 0, "text", data=_translate_mode(water)))
        model = str(getattr(device.product, "model", "Roborock"))
        return {
            "id": node_id,
            "name": str(device.name or model),
            "note": f"Server · Roborock · {model}",
            "state": 1 if online and not error_text else (2 if error_text else 0),
            "profile": 0,
            "protocol": 20,
            "image": "fan",
            "state_changed": now,
            "attributes": attributes,
        }

    @staticmethod
    def _status_name(status):
        if status is None:
            return "unknown"
        return _enum_name(getattr(status, "state", None) or getattr(status, "status", None))

    def _email(self):
        return str(self.configuration.get("email", "")).strip().lower()

    def _poll_seconds(self):
        try:
            return max(15, min(3600, int(float(self.configuration.get("poll_seconds", 30)))))
        except (TypeError, ValueError):
            return 30

    def _remember_target(self, device, kind, value):
        device_key = str(device.duid)
        current = dict(self.selected_targets.get(device_key, {}))
        current[kind] = int(value)
        current["kind"] = kind
        self.selected_targets[device_key] = current
        self.context.save_state({"selected_targets": self.selected_targets})

    def _select_target(self, device, kind, value):
        if int(value) == -1:
            device_key = str(device.duid)
            current = dict(self.selected_targets.get(device_key, {}))
            current["kind"] = "complete"
            self.selected_targets[device_key] = current
            self.context.save_state({"selected_targets": self.selected_targets})
            return
        options = self._control(device).get("rooms" if kind == "room" else "routines", [])
        if not _option_by_value(options, value):
            raise ValueError("Der ausgewählte Raum beziehungsweise die Routine ist nicht mehr verfügbar")
        self._remember_target(device, kind, value)

    def _selected_target(self, device, kind, options):
        target = self.selected_targets.get(str(device.duid), {})
        if target.get("kind", "complete") != kind:
            return -1
        selected = target.get(kind, -1)
        return int(selected) if _option_by_value(options, selected) else -1

    @staticmethod
    def _session_matches(session, email):
        return (
            isinstance(session, dict)
            and session.get("email") == email
            and isinstance(session.get("user_data"), dict)
            and bool(session["user_data"])
        )


def _enum_name(value):
    if value is None:
        return "unknown"
    name = getattr(value, "name", None)
    if name:
        return str(name).lower()
    raw = getattr(value, "value", value)
    if isinstance(raw, str):
        return raw.lower()
    return str(raw).lower()


def _enum_label(value):
    if value is None:
        return ""
    raw = getattr(value, "value", None)
    if isinstance(raw, str):
        return raw
    return str(getattr(value, "name", value))


def _enum_code(value):
    if value is None:
        return 0
    code = getattr(value, "code", None)
    if code is not None:
        return int(code)
    raw = getattr(value, "value", value)
    return int(raw) if isinstance(raw, (int, float)) else 0


def _choice(value, label, command, key=None):
    return {
        "value": int(value),
        "label": str(label),
        "command": command,
        "key": str(key if key is not None else command),
    }


def _enum_choices(enum_type):
    choices = []
    for option in enum_type:
        code = getattr(option, "code", None)
        raw = getattr(option, "value", None)
        if code is None or int(code) < 0 or str(raw).lower() == "unknown":
            continue
        choices.append(_choice(code, _translate_mode(raw), code, raw))
    return choices


def _option_by_value(options, value):
    return next((item for item in options if int(item.get("value", -9_999_999)) == int(value)), None)


def _choice_value_for_label(options, label):
    normalized = str(label or "").strip().lower()
    for item in options:
        if normalized in {
            str(item.get("key", "")).strip().lower(),
            str(item.get("command", "")).strip().lower(),
            str(item.get("label", "")).strip().lower(),
        }:
            return int(item["value"])
    return int(options[0]["value"]) if options else 0


def _choice_data(value, options, fallback=""):
    selected = _option_by_value(options, value)
    return json.dumps({
        "label": selected.get("label") if selected else fallback,
        "options": [{"value": item["value"], "label": item["label"]} for item in options],
    }, ensure_ascii=False, separators=(",", ":"))


def _status_error(status):
    if status is None:
        return ""
    name = getattr(status, "error_code_name", None)
    if name and str(name).lower() not in ("none", "no_error", "unknown", "0"):
        return _translate_mode(str(name))
    fault = getattr(status, "fault", None)
    fault_name = _enum_name(fault)
    if fault is not None and fault_name not in ("none", "no_error", "unknown", "0", "-1"):
        return _translate_mode(fault_name)
    return ""


def _translate_status(value):
    labels = {
        "unknown": "Unbekannt", "idle": "Bereit", "sleeping": "Ruhezustand",
        "starting": "Startet", "cleaning": "Reinigt", "sweeping": "Saugt",
        "mopping": "Wischt", "sweep_and_mop": "Saugt und wischt",
        "spot_cleaning": "Punktreinigung", "zoned_cleaning": "Zonenreinigung",
        "segment_cleaning": "Raumreinigung", "paused": "Pausiert",
        "returning_home": "Fährt zur Ladestation", "waiting_to_charge": "Wartet auf Ladung",
        "docking": "Dockt an", "charging": "Lädt", "charging_complete": "Vollständig geladen",
        "emptying_the_bin": "Staubbehälter wird geleert", "washing_the_mop": "Mopp wird gewaschen",
        "going_to_wash_the_mop": "Fährt zur Moppwäsche", "mapping": "Erstellt Karte",
        "relocating": "Bestimmt Position", "updating": "Aktualisiert", "error": "Fehler",
    }
    return labels.get(value, _translate_mode(value))


def _translate_mode(value):
    labels = {
        "off": "Aus", "quiet": "Leise", "balanced": "Ausgeglichen", "turbo": "Turbo",
        "max": "Maximal", "max_plus": "Maximal+", "low": "Niedrig", "medium": "Mittel",
        "high": "Hoch", "gentle": "Sanft", "mild": "Niedrig", "standard": "Standard",
        "intense": "Intensiv", "extreme": "Extrem", "fast": "Schnell", "deep": "Gründlich",
        "deep_plus": "Sehr gründlich", "smart_mode": "Smart", "custom": "Benutzerdefiniert",
        "custom_water_flow": "Eigener Wasserfluss", "vac_and_mop": "Saugen und Wischen (gleichzeitig)", "vacuum": "Saugen",
        "vac_then_mop": "Erst saugen, dann wischen", "vacuum_then_mop": "Erst saugen, dann wischen",
        "vacuum_and_then_mop": "Erst saugen, dann wischen", "sweep_then_mop": "Erst saugen, dann wischen",
        "sweep_and_then_mop": "Erst saugen, dann wischen",
        "mop": "Wischen", "customized": "Benutzerdefiniert", "slight": "Sehr niedrig", "moderate": "Erhöht",
    }
    normalized = str(value).strip().lower()
    return labels.get(normalized, normalized.replace("_", " ").strip().capitalize() or "Unbekannt")


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def _text(value):
    return "" if value is None else str(value)


def _friendly_error(error):
    text = str(error).strip()
    lowered = text.lower()
    translations = (
        (("invalid code", "incorrect code"), "Der Roborock-Anmeldecode ist ungültig oder abgelaufen."),
        (("account does not exist", "no account"), "Das Roborock-Konto wurde nicht gefunden. E-Mail-Adresse prüfen."),
        (("too many codes", "too frequent", "rate limit"), "Zu viele Anfragen. Bitte einige Minuten warten."),
        (("user agreement",), "Bitte die aktuellen Nutzungsbedingungen zuerst in der Roborock-App bestätigen."),
        (("unauthorized", "not authorized"), "Die Roborock-Sitzung ist abgelaufen. Anmeldung zurücksetzen und neu verbinden."),
    )
    for needles, message in translations:
        if any(needle in lowered for needle in needles):
            return message
    return text or error.__class__.__name__
