# SmartHomeBoard Server

Der Server hält Geräteverbindungen, Zustände und Automationen dauerhaft auf einem Raspberry Pi oder Linux-Rechner. iOS-App und Webportal dienen zur gemeinsamen Konfiguration und Anzeige.

Externe KI-Anwendungen anbinden: [MCP einrichten, Homeegramme und Serverautomationen verwalten](MCP.md).

## Updates

- **0.20.x:** Erweiterte Automationspfade und Auslöser, Sonos-Steuerung und Sprachausgabe, lesbare Push-Werte und Schaltprotokoll. MCP-Anbindung für Gerätezugriff, grafische Homeegramm-Entwürfe, HG-Verwaltung und modulübergreifende Serverautomationen. Getrennte Freigaben, deaktivierte Anlage, lokale HG-Sicherungen und Wiederholungsschutz; bestätigte Einzel- und Mehrfachlöschung.
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

## KI-Anbindung mit MCP

MCP verbindet eine externe KI-Anwendung mit dem SHB-Server. Die KI erhält Werkzeuge für freigegebene Serverdaten und Aktionen; ein eigenes KI-Modell wird nicht auf dem SHB-Server betrieben.

### Einrichten

1. Server und iOS-App aktualisieren und die App unter **Lokaler Server** verbinden. Der vollständige hier beschriebene Umfang benötigt mindestens Server **0.20.26** und App-Build **318**.
2. In der App **Lokaler Server → KI-Anbindung → MCP-Zugang und HG-Entwürfe** öffnen. **MCP-Zugang aktivieren** einschalten, gewünschte Rechte auswählen und **Berechtigungen speichern** drücken.
3. Einen **MCP-Schlüssel** erstellen und sofort sicher kopieren. Er wird nur einmal angezeigt und ist vom administrativen Server-API-Schlüssel getrennt.
4. Im KI-Client eine Verbindung mit **Streamable HTTP** anlegen:

   - Adresse: die in der App angezeigte MCP-Adresse, normalerweise `http://SERVER-IP:8787/mcp/`.
   - HTTP-Header: `Authorization: Bearer <MCP-Schlüssel>`.
5. Die Werkzeugliste neu laden oder einen neuen Chat starten. Zum Testen zunächst „Zeige meine Geräte“ anfragen.

Der KI-Client muss eigene Bearer-Header unterstützen; OAuth-Anmeldung ist derzeit nicht implementiert. Den Schlüssel nicht als Chatnachricht versenden oder in GitHub ablegen.

### Was ist möglich?

- Geräte und Zustände lesen sowie Homeegramme mit aufgelösten Geräte- und Gruppennamen erklären.
- Homeegramme grafisch ansehen und Entwürfe gemeinsam mit der KI bearbeiten; dafür muss der Client **MCP Apps** unterstützen. Reine Text-MCP-Clients zeigen keine interaktive Oberfläche.
- Neue Homeegramme deaktiviert auf homee anlegen; vorhandene HGs gezielt bearbeiten, aktivieren/deaktivieren oder einzeln bzw. mehrere ausdrücklich ausgewählte HGs löschen.
- Freigegebene An/Aus-Geräte einschalten, ausschalten oder umschalten.
- Echte SHB-Serverautomationen mit den nativ unterstützten Auslösern, Bedingungen, Pfaden und Aktionen deaktiviert anlegen sowie separat aktivieren/deaktivieren. Sie laufen auf dem Server und können Geräte verschiedener Servermodule verbinden; Homeegramme laufen dagegen auf homee.

Schreibrechte sind einzeln freizugeben und standardmäßig aus. Neue Homeegramme und Serverautomationen werden **deaktiviert** gespeichert; bearbeitete HGs bleiben ebenfalls deaktiviert. Aktivieren ist ein eigener bewusster Auftrag und kein Play-Befehl. Nicht jede Möglichkeit des regulären App-Editors steht auch als MCP-Werkzeug bereit; die genauen Grenzen stehen in der [MCP-Anleitung](MCP.md).

### Beispielaufträge an die KI

- „Erkläre mir die Auslöser, Bedingungen und Aktionen von Homeegramm 503.“
- „Erstelle ein deaktiviertes Homeegramm: Bei Bewegung im Flur den ausgewählten Flurspot einschalten. Zeige mir vorher die Geräte und den Ablauf.“
- „Öffne den Homeegramm-Entwurf grafisch, damit wir ihn gemeinsam bearbeiten können.“
- „Lege eine deaktivierte Serverautomation an: Wenn der ausgewählte Sensor Bewegung meldet, schalte die ausgewählte Shelly-Lampe ein.“

Nur serverseitig vorhandene Daten werden bereitgestellt. Messwerte können Cachewerte sein; HomeKit-/Matter-Daten, die nur in der iOS-App vorliegen, werden nicht automatisch geteilt. Cloud-KI-Anbieter erhalten die angefragten Hausdaten. Der Client muss den Server erreichen können; eine private LAN-Adresse ist aus einer Cloud-KI nicht direkt erreichbar. Für entfernten Zugriff HTTPS über einen abgesicherten Zugang verwenden, keine ungeschützte Portfreigabe.

Zugriff und einzelne Rechte lassen sich in der App wieder entziehen; ein neuer Schlüssel widerruft den bisherigen. Details zu Werkzeugen, Freigaben, gemeinsamer Grafikansicht und Sicherheit: [MCP.md](MCP.md).

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

Für Sprachausgabe wird die Audiodatei über gTTS erzeugt. Dafür ist Internet erforderlich und der Sprachtext wird extern verarbeitet; die Gerätesteuerung erfolgt lokal.

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

## Lizenz und Drittanbieter

Der eigene Projektcode und die eigene Dokumentation von Michael Mann stehen unter der [MIT-Lizenz](LICENSE), Copyright © 2026 Michael Mann. MIT erlaubt auch kommerzielle Weiterverwendung dieses eigenen Codes.

Abhängigkeiten, übernommene homee-Kataloginhalte und andere Fremdbestandteile behalten ihre eigenen Bedingungen. Insbesondere wird **Mutagen unter GPL-2.0-or-later** verwendet; das kombinierte Serverpaket ist daher nicht pauschal ausschließlich MIT-lizenziert. Die in der zugehörigen App verwendeten **homee-Grafiken sind mit Genehmigung der homee GmbH integriert** und werden durch die MIT-Lizenz nicht allgemein freigegeben.

Siehe [Lizenzumfang und Drittanbieterhinweise](THIRD_PARTY_NOTICES.md), einschließlich Python-Bibliotheken und M5Paper-Frameworks.
