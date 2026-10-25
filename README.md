# SmartHomeBoard Server

Der Server hält Geräteverbindungen, Zustände und Automationen dauerhaft auf einem Raspberry Pi oder Linux-Rechner. iOS-App und Webportal dienen zur gemeinsamen Konfiguration und Anzeige.

Externe KI-Anwendungen anbinden: [MCP einrichten, Homeegramme und Serverautomationen verwalten](MCP.md).

## Updates

- **0.20.26 / iOS-Build 318:** MCP kann echte SHB-Serverautomationen mit allen nativ unterstützten Server-Auslösern, Bedingungen, Pfaden und Aktionen anlegen. Separate neue Freigaben für deaktiviertes Anlegen sowie Aktivieren/Deaktivieren; keine automatische Ausführung. Eigene SHB-IDs statt homee-HG-IDs, Gerätegruppen, sichere Push-Empfängerauswahl, Sonos, Roborock und Automationssteuerung. Bestehende App-/Serverregeln und unabhängige Zeitabläufe bleiben erhalten; UUID-Wiederholungen erzeugen keine Kopien und Aktivierungen benötigen Name und aktuelle Revision. Neue Rechte sind standardmäßig aus; nach dem Serverupdate MCP-Werkzeugliste neu laden bzw. neuen Chat öffnen.
- **0.20.25 / iOS-Build 317 unverändert:** Mehrfachlöschung von HGs stoppt nicht mehr bei automatischer homee-Neusortierung oder beim Entfernen vorhandener Verweise auf bereits im selben Auftrag bestätigt gelöschte HGs. Vor dem nächsten HG wird der vollständige Bestand frisch geladen; verkürzte DELETE-Antworten lösen keinen falschen Abbruch aus. Echte Inhalts-/Identitätsänderungen, fehlende Freigaben und unbestätigte Löschungen stoppen weiterhin sicher. Ergebnis und Audit nennen einen sicheren Abbruchcode; Sicherungen und UUID-Schutz bleiben erhalten.
- **0.20.24 / iOS-Build 317 unverändert:** HGs als Aktionen aktivieren, deaktivieren, abspielen oder stoppen sowie als Auslöser auf Auslösen, Stoppen, Aktivieren oder Deaktivieren eines Quell-HGs reagieren. In MCP anlegen, bearbeiten, lesen und im gemeinsamen grafischen Entwurf auswählen. Verweise bleiben auf die gewählte homee-Quelle begrenzt; Speichern führt kein referenziertes HG aus. Neue und bearbeitete HGs bleiben deaktiviert.
- **0.20.23 / iOS-Build 317 unverändert:** Gruppen in HGs über MCP mit Namen, Mitgliedern, passenden Attributen und lesbaren Zielzuständen anzeigen; Gruppen/Relationships gezielt nachladen. Mehrere Original-Homeegramme gezielt löschen: explizite Liste mit 1–50 IDs und Namensbestätigungen derselben Quelle, gemeinsame Vorprüfung und nacheinander gesicherte, bestätigte Löschungen. Beim ersten Fehler oder unklaren Ergebnis wird gestoppt und der Stand pro HG gemeldet. Nutzt die vorhandene Löschfreigabe; keine Wildcards und kein automatisches Freischalten. Nach dem Update die MCP-Werkzeugliste neu laden.
- **0.20.22 / iOS-Build 317:** Vollständiges Paket behebt den Startabbruch wegen fehlendem `server.homeegram_editing`. Neue getrennte KI-Freigaben für Aktivieren/Deaktivieren und gezieltes Bearbeiten von HG-Elementen. Echte interaktive MCP-App zum Ansehen und gemeinsamen Erstellen revisionierter Entwürfe; Versand nur ausdrücklich und deaktiviert. Der Chat-Client muss MCP Apps unterstützen und nach dem Serverupdate die Werkzeuge neu laden.
- **0.20.21 / iOS-Build 316:** Homeegramme auch in der serververbundenen App bestätigt löschen. Direkte und verzögerte homee-Löschantworten werden korrekt erkannt; die App lädt die HG-Liste über den Server. Backup, genaue Quellen-/Namensprüfung und Schutz gegen wiederholte Löschbefehle bleiben erhalten.
- **0.20.20:** Einzelne echte Homeegramme über MCP löschen: separate Freigabe, exakte Namensbestätigung, vollständige lokale Sicherung vor dem Löschen und Bestätigung durch homee. Wiederholte Anfragen senden keinen zweiten Löschbefehl.
- **0.20.19:** KI kann mit getrennter Freigabe echte deaktivierte HGs auf homee anlegen und Servergeräte ein-/aus-/umschalten. Vorhandene KI-Entwürfe lassen sich in iOS bearbeiten und über die Serververbindung auf homee speichern. Kein direkter iPhone-homee-Zugang erforderlich.
- **0.20.18:** Optionaler MCP-Zugang für externe KI-Clients: Messwerte lesen und deaktivierte HG-Entwürfe in SHB prüfen. Eigener widerrufbarer Schlüssel, keine Geräteausführung.
- **0.20.17:** Homeegramm-Pfade mit Voraussetzungen, Immer-, Dann- und Sonst-Aktionen; manuelle Automationen ohne Auslöser.
- **0.20.x:** Sonos-Steuerung und Sprachausgabe, Wertänderungs-Auslöser, lesbare Push-Werte und Schaltprotokoll.
- **0.19.x:** Laufzeitstatus für Automationen und bidirektionale EnOcean-Rollläden.
- **0.18.x:** Philips Hue und erweitertes EnOcean-Anlernen.
- **0.17.x:** Shelly Gen2–4, Plus Add-on und BLU/BTHome.
- **0.16.x:** homee-History, Synchronisierung, Push-IDs und Z-Wave-Sollwerte.
- **0.15.x:** Z-Wave, Push-Empfänger und Automationserstellung.
- **0.10–0.14:** Webportal, Roborock, Bosch, VELUX, MotionBlinds und E-Paper.

## Installation

Voraussetzung sind Docker und Docker Compose:

```bash
git clone https://github.com/Micha2536/SmartHomeBoard-Server.git
cd SmartHomeBoard-Server
docker compose up -d --build
```

Alternativ das Repository über **Code → Download ZIP** laden, entpacken und im Ordner mit `docker compose up -d --build` starten.

1. `http://SERVER-IP:8400/setup` öffnen.
2. Kommunikationsport (Standard `8787`) und API-Schlüssel festlegen.
3. In der App unter **Einstellungen → Lokaler Server** Adresse und API-Schlüssel eintragen und den Servermodus aktivieren.

Die Setup-Seite bleibt auf Port `8400`; die App verwendet den eingestellten Kommunikationsport.

## Server aktualisieren

Vorher den Ordner `data` sichern, danach:

```bash
git pull
docker compose up -d --build
```

## Integrationen einrichten

Integrationen werden im Webportal unter **Integrationen** oder in der iOS-App unter **Lokaler Server** angelegt. Nicht benötigte Geräte können dort deaktiviert werden; ihre Werte bleiben erhalten.

### Shelly Gen2+, Plus Add-on und BLU

- Integration speichern; Geräte werden normalerweise automatisch gefunden.
- Falls die Suche nicht funktioniert, feste Shelly-IP-Adressen kommagetrennt eintragen.
- Bei aktivierter Shelly-Anmeldung das Gerätepasswort hinterlegen.
- Für BLU-Geräte eine Vorlage wählen, den 30-Sekunden-Lernmodus starten und den Sensor betätigen. Bei verschlüsseltem BTHome zusätzlich den 32-stelligen AES-Schlüssel eintragen.

### Philips Hue

- Bridge automatisch suchen lassen oder ihre IP-Adresse eintragen.
- Runde Taste auf der Bridge drücken.
- **Bridge-Taste drücken und verbinden** auswählen.

Die Kopplung bleibt im persistenten `data`-Ordner gespeichert.

### Bosch Smart Home

- IP-Adresse und einmalig das Systempasswort des Controllers eintragen.
- Controller in den Kopplungsmodus versetzen.
- **Controller koppeln** auswählen.

Controller II: Fronttaste kurz drücken. Controller I: Taste halten, bis die LEDs blinken. Die lokale API ist für private, nicht gewinnorientierte Nutzung vorgesehen.

### Bosch Home Connect

- Im Home Connect Developer Portal eine Anwendung mit Device Flow und den Scopes `IdentifyAppliance Monitor` anlegen.
- Client-ID und optional Client-Secret speichern.
- **Home Connect koppeln** wählen, den Code auf der Home-Connect-Seite eingeben und dort mit SingleKey ID anmelden.

Die SingleKey-ID-Zugangsdaten bleiben ausschließlich bei Home Connect; der SHB-Server speichert nur die erteilten OAuth-Tokens. Geräte und Livewerte werden anschließend serverseitig über den Home-Connect-SSE-Strom aktualisiert. Die erste Version dient ausschließlich der Anzeige und startet keine Programme.

### Sonos (lokal)

- Das Sonos-Modul benötigt kein Cloudkonto und findet Lautsprecher per SSDP im lokalen Netz.
- Wenn Docker oder ein VLAN Multicast blockiert, die IP-Adressen der Lautsprecher im Modul hinterlegen.
- Wiedergabe/Pause, Lautstärke, Stumm, Stop, Titel vor/zurück, Gruppenlautstärke, Bass, Höhen und Loudness stehen als schreibbare Attribute für Serverautomationen bereit.

### Roborock

- E-Mail-Adresse des Roborock-Kontos speichern.
- Anmeldecode anfordern.
- Code aus der E-Mail eintragen und erneut speichern.

Die Anmeldung benötigt Internetzugriff zur Roborock-Cloud.

### homee

- IP-Adresse, Benutzername und Passwort eintragen.
- Empfohlen wird ein eigener homee-Benutzer für den Server.
- Bei aktivem Servermodus keine zweite direkte homee-Verbindung in der App verwenden.

### VELUX ACTIVE und MotionBlinds

- **VELUX ACTIVE:** E-Mail, Passwort und Abfrageintervall eintragen; Internetzugriff ist erforderlich.
- **MotionBlinds:** feste Gateway-IP und Secret Key eintragen. Docker muss mit `network_mode: host` laufen.

### go-e Charger

- IP-Adresse oder lokalen Hostnamen des Chargers eintragen.
- Falls abweichend, den HTTP-Port anpassen.

### Modbus TCP

- IP-Adresse, Port, Unit-ID und passendes Geräteprofil auswählen.
- Eigene Profile unter **Modbus-Templates verwalten** im Webportal oder im Template-Editor der App anlegen.
- Eigene Serverprofile werden persistent in `data/modbus-profiles` gespeichert.

Enthalten sind Profile für Victron GX/MPPT, SMA Sunny Boy, b-control/TQ EM300 und MENNEKES AMTRON.

## EnOcean USB300

Den stabilen USB-Pfad ermitteln:

```bash
ls -l /dev/serial/by-id/
```

Im Projektordner eine `.env` anlegen und den vollständigen USB300-Pfad eintragen:

```dotenv
ENOCEAN_DEVICE=/dev/serial/by-id/usb-EnOcean_GmbH_EnOcean_USB_300_DB_SERIENNUMMER-if00-port0
```

Danach neu starten:

```bash
docker compose up -d --build
```

Anschließend die EnOcean-Integration anlegen, EEP-Profil auswählen, Lernmodus starten und den Sensor beziehungsweise Taster betätigen. Geräte können im Webportal unter `/setup/enocean` umbenannt, neu zugeordnet oder gelöscht werden.

## Z-Wave

Den stabilen Stick-Pfad mit `ls -l /dev/serial/by-id/` ermitteln. Danach `.env.example` kopieren und anpassen:

```bash
cp .env.example .env
nano .env
```

```dotenv
ZWAVE_DEVICE=/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_SERIENNUMMER-if00-port0
ZWAVE_SESSION_SECRET=eine-lange-zufaellige-zeichenfolge
```

Z-Wave JS UI und SmartHomeBoard starten:

```bash
docker compose --profile zwave up -d --build
```

Einmalig `http://SERVER-IP:8091` öffnen und in Z-Wave JS UI:

1. Seriellen Port `/dev/zwave` kontrollieren.
2. S0- und S2-Sicherheitsschlüssel erzeugen und dauerhaft sichern.
3. Z-Wave-JS-WebSocket auf Port `3000` aktivieren.

Danach im SmartHomeBoard-Webportal eine Z-Wave-Integration mit `ws://127.0.0.1:3000` anlegen. Port `3000` und die Oberfläche auf Port `8091` nicht öffentlich freigeben. Den Ordner `zwave-store` regelmäßig sichern und niemals veröffentlichen.

Für spätere Updates weiterhin das Profil angeben:

```bash
git pull
docker compose --profile zwave up -d --build
```

## M5Paper

Die Firmware liegt unter [`m5paper-firmware`](m5paper-firmware) und enthält keine privaten Zugangsdaten.

1. Firmware aufspielen und WLAN am Display einrichten.
2. Den angezeigten Kopplungscode unter `http://SERVER-IP:8400/setup/displays` eingeben.
3. Name, Ansicht, Werte und Aktualisierungstakt konfigurieren.

## Push-Nachrichten

Für Push bei geschlossener App die Apple-APNs-Datei als `secrets/AuthKey.p8` ablegen und `.env` ergänzen:

```dotenv
SHB_APNS_TEAM_ID=DEINE_APPLE_TEAM_ID
SHB_APNS_KEY_ID=DEINE_APNS_KEY_ID
SHB_APNS_BUNDLE_ID=Michael.SmartHomeBoard
SHB_APNS_KEY_PATH=/run/secrets/AuthKey.p8
```

Danach `docker compose up -d --build` ausführen. iOS-Geräte registrieren sich nach erteilter Mitteilungserlaubnis automatisch. In Push-Texten stehen `{name}`, `{attribute}`, `{value}` und `{unit}` als Platzhalter zur Verfügung.

## Automationen

Automationen können im Webportal oder in der App erstellt werden. Serverfähige Auslöser, Bedingungen und Aktionen laufen auch ohne verbundenes iPad. iPad-spezifische Aktionen bleiben als lokale Automation gekennzeichnet.

## Sicherheit und Datensicherung

- `data`, `zwave-store`, `secrets` und `.env` regelmäßig sichern.
- Diese Ordner und Dateien niemals bei GitHub veröffentlichen.
- Serverports nicht direkt ins Internet freigeben; für externen Zugriff VPN oder TLS-Reverse-Proxy verwenden.
- API-Schlüssel, Zugangsdaten und APNs-Schlüssel nur im lokalen Server hinterlegen.
