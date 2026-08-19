# SmartHomeBoard Server

Der Server verlagert Geräteverbindungen, Zustände und Automationen aus der iOS-App auf einen dauerhaft laufenden Raspberry Pi. Die App bleibt Konfigurationsoberfläche und Dashboard.

## Raspberry Pi installieren

Voraussetzung sind ein Raspberry Pi oder Linux-Rechner mit installiertem Docker und Docker Compose. Das Repository wird direkt von GitHub geladen:

```bash
git clone https://github.com/Micha2536/SmartHomeBoard-Server.git
cd SmartHomeBoard-Server
docker compose up -d --build
```

Alternativ kann unter **Code → Download ZIP** ein Archiv heruntergeladen, auf den Server kopiert und entpackt werden.

1. Im geklonten oder entpackten Verzeichnis starten:

   ```bash
   docker compose up -d --build
   ```

2. Im Browser `http://IP-DES-PI:8400/setup` öffnen. Das responsive Portal trennt Übersicht, Integrationen, E-Paper, Automationen und Geräteprofile. Dort den Kommunikationsport der App festlegen (Standard: `8787`) und den API-Schlüssel erzeugen. Docker-Dateien müssen dafür nicht bearbeitet werden.
3. In der App unter **Einstellungen → Lokaler Server** eintragen:
   - Adresse: `http://IP-DES-PI:8787` beziehungsweise der auf der Setup-Seite gewählte Kommunikationsport
   - API-Schlüssel: der auf der Einrichtungsseite festgelegte Wert
   - Servermodus aktivieren

Der Container verwendet auf Linux `network_mode: host`. Das ist für lokale Geräte, UDP, Broadcast und Multicast sinnvoll. Die Einrichtungsseite bleibt immer auf Port `8400` erreichbar. Die API läuft getrennt auf dem frei wählbaren Kommunikationsport. Port `8400` kann deshalb nicht als Kommunikationsport gewählt werden.

## Push-Nachrichten und Automationsempfänger

Nach Zustimmung zur iOS-Mitteilungserlaubnis registriert sich jedes iPhone oder iPad automatisch mit seinem Gerätenamen beim lokalen Server. Unter **Automationen → Push-Nachricht vom Server** kann anschließend an alle registrierten Geräte oder gezielt an einen oder mehrere Empfänger gesendet werden. In den Automationen werden nur stabile interne Empfänger-IDs gespeichert; die eigentlichen APNs-Gerätetokens zeigt das Portal nicht an.

Für die Zustellung bei geschlossener App benötigt der Server einen Apple-APNs-Schlüssel. Die von Apple geladene `.p8`-Datei wird als `secrets/AuthKey.p8` abgelegt und nicht eingecheckt. Daneben wird eine `.env` angelegt:

```text
SHB_APNS_TEAM_ID=DEINE_APPLE_TEAM_ID
SHB_APNS_KEY_ID=DEINE_APNS_KEY_ID
SHB_APNS_BUNDLE_ID=Michael.SmartHomeBoard
```

Danach den Server mit `docker compose up -d --build` neu erstellen. Das Webportal zeigt im Automationsbereich, ob der Schlüssel vollständig konfiguriert ist und wie viele iOS-Geräte registriert sind. Eigene Texte können mit einem ausgewählten Gerätewert ergänzt werden; dafür stehen `{selectedDevice}`, `{selectedAttribute}` und `{selectedValue}` zur Verfügung. Ohne Platzhalter hängt der Server den ausgewählten Wert lesbar an den Nachrichtentext an.

Automationen können außerdem andere Automationen **abspielen**, **stoppen**, **aktivieren** oder **deaktivieren**. Stoppen bricht auch bereits wartende, verzögerte Aktionen ab. Gegenseitige Endlosschleifen werden serverseitig erkannt und verhindert.

Ab Server 0.15.5 verwenden der API-Prozess der iOS-App und der getrennte Webportal-Prozess SQLite konsequent als gemeinsamen Automationsstand. Änderungen aus der App erscheinen dadurch ohne Serverneustart im Webportal; Webänderungen werden vor der nächsten App-Synchronisierung eingelesen und bleiben erhalten. Die Automationsliste im Webportal aktualisiert sich alle zwei Sekunden im Hintergrund, ohne die Seite neu zu laden oder ihre Scrollposition zu verändern.

Server 0.15.6 verarbeitet beim Push-Empfänger „Alle Geräte“ jeden APNs-Token unabhängig. Ein abgelaufener, widerrufener oder zur falschen Umgebung gehörender Empfänger blockiert dadurch nicht mehr die Zustellung an die übrigen Geräte. Von Apple ausdrücklich als ungültig gemeldete Gerätetokens werden automatisch aus der Empfängerliste entfernt.

Server 0.15.7 führt im Webportal schrittweise durch die Automationserstellung. Technische homee-Attributtypen werden als verständliche deutsche Eigenschaften dargestellt; aktueller Wert, Einheit und Wertebereich erscheinen direkt bei der Auswahl. Binäre Werte werden beispielsweise als Ein/Aus, Geöffnet/Geschlossen oder Alarm/Kein Alarm angeboten.

## Server aktualisieren

Vor einem Update sollte der persistente Ordner `data` gesichert werden. Anschließend im Repository ausführen:

```bash
git pull
docker compose up -d --build
```

Die Datenbank und eigenen Modbus-Profile bleiben im eingebundenen `data`-Ordner erhalten.

## Mitgelieferte Module

- `demo`: prüft dynamische Formulare, Liveupdates und Steuerbefehle.
- `go_e`: lokale go-e HTTP API v2.
- `modbus`: universeller Modbus-TCP-Adapter. MENNEKES AMTRON 4You 500 / 4Business 700 ist als Profil enthalten.
- `enocean`: EnOcean USB300 über das standardisierte ESP3-Protokoll mit persistentem Anlernmodus.
- `homee`: dauerhafte lokale homee-WebSocket-Verbindung mit persistentem Gerätesnapshot und Livewerten.
- `roborock`: persistente Roborock-Cloud-Verbindung mit E-Mail-Code-Anmeldung, Zuständen und Reinigungssteuerung.
- `bosch-smart-home`: lokale, zertifikatsgesicherte Verbindung zum Bosch Smart Home Controller mit Livewerten, Gerätesteuerung und Szenarien.

## Bosch Smart Home verbinden

Die Integration arbeitet ausschließlich im lokalen Netz über den Bosch Smart Home Controller; ein Bosch-Cloudkonto wird nicht benötigt. Unter **Integrationen → Bosch Smart Home** zunächst die IP-Adresse des Controllers und einmalig dessen Systempasswort eintragen und speichern. Dann den Controller in den Kopplungsmodus versetzen: beim Smart Home Controller II die Fronttaste kurz drücken, beim Controller der ersten Generation die Taste gedrückt halten, bis die LEDs blinken. Anschließend über **Modulaktionen → Controller koppeln** die einmalige Registrierung auslösen.

Der Server erzeugt bei der Kopplung einen eigenen `oss_`-Client mit eingeschränkter Rolle und ein 2048-Bit-Clientzertifikat. Zertifikat und privater Schlüssel werden persistent in der lokalen Serverdatenbank gespeichert; das Systempasswort wird nach erfolgreicher Kopplung aus der Integrationskonfiguration entfernt. Bei Serverneustarts wird nur die bestehende Zertifikatsverbindung wiederhergestellt – es findet kein erneutes Pairing und kein Cloud-Login statt.

Gerätewerte werden zunächst vollständig eingelesen und anschließend über den von Bosch vorgesehenen Long-Poll-Kanal live aktualisiert. Abgebildet werden unter anderem Temperaturen, Luftfeuchtigkeit, Heizungs-Sollwerte, Fensterkontakte, Zwischenstecker, Leistung und Energie, Lichtpegel, Rollläden, Bewegungsmelder, Wassermelder, Luftqualität und weitere vom Controller bereitgestellte skalare Zustände. Schreibbare Schalter, Solltemperaturen und Positionen lassen sich aus Dashboard und Automationen ändern. Bosch-Szenarien erscheinen als eigene ausführbare Servergeräte.

Die lokale Bosch-Smart-Home-API ist laut Bosch für private, nicht gewinnorientierte Nutzung vorgesehen. Diese Integration ist daher für den privaten SmartHomeBoard-Betrieb gedacht und ist keine von Bosch zertifizierte oder unterstützte Software.

## Roborock verbinden

Unter **Integrationen → Roborock** zuerst die E-Mail-Adresse des Roborock-Kontos speichern. Danach über **Modulaktionen** einen Anmeldecode anfordern, den Code aus der E-Mail in das Konfigurationsfeld eintragen und erneut speichern. Die bestätigte Sitzung wird persistent auf dem lokalen Server und nicht in der iOS-App abgelegt; der einmalige Code wird nach erfolgreicher Anmeldung aus der Konfiguration entfernt.

Der Server hält die Geräteverbindung unabhängig von der iOS-App aktiv. Unterstützte V1-Modelle (unter anderem viele S-, Q- und Saros-Modelle) sowie neuere Q10-Geräte liefern Akkustand, Status, Fehler, gereinigte Fläche und Reinigungszeit. Zusätzlich werden die vom konkreten Modell gemeldeten Reinigungsarten, Saugstufen, Wassermengen und Räume übernommen. Bei V1-Geräten erscheinen außerdem die in der Roborock-App angelegten Routinen. Komplett-, Raum-, Punkt- und Routinenreinigung, Pause, Stop sowie die Rückkehr zum Dock können dadurch direkt aus SmartHomeBoard ausgelöst werden. Anmeldung, Aktualisierung, Abmeldung und Konfiguration sind identisch im Webportal und in der iOS-App verfügbar.

Die iOS-Dashboardvorlage **Roborock** wird für Server-Roborocks automatisch vorgeschlagen. Auf der Kachel befinden sich Akku, Status, Start/Pause, Stop und Dock. Über das Reglersymbol öffnet sich die vollständige modellabhängige Steuerung. Eine Raum- oder Routinenauswahl startet die ausgewählte Reinigung unmittelbar; **Start** ohne Auswahl beginnt weiterhin die normale Komplettreinigung.

Die Anbindung nutzt die nicht von Roborock offiziell bereitgestellte Open-Source-Bibliothek `python-roborock`. Änderungen an der Roborock-Cloud können deshalb ein späteres Serverupdate erforderlich machen. Das voreingestellte Aktualisierungsintervall beträgt 30 Sekunden und wird zum Schutz von Gerät und Cloud auf mindestens 15 Sekunden begrenzt.

## homee dauerhaft über den Server verbinden

Unter `http://IP-DES-PI:8400/setup/integrations` **homee** auswählen und IP-Adresse, Benutzername sowie Passwort eintragen. Der Server holt den Access Token lokal vom homee, hält die WebSocket-Verbindung offen und verbindet sich nach Unterbrechungen automatisch neu. Nach jeder Verbindung fordert er mit `GET:all` den vollständigen Datenbestand an. Nodes und einzelne Attributänderungen werden in SQLite gespeichert und über den SmartHomeBoard-WebSocket an verbundene iPads verteilt. Weitere Ressourcen wie Homeegramme, Gruppen, Pläne, Benutzer, Beziehungen und Szenarien werden als persistenter Integrationszustand gespeichert und durch nachfolgende Socket-Ereignisse aktualisiert.

Ab Server 0.9.3 erzeugt jede homee-Integration automatisch eine eigene stabile `device_hardware_id` und verwendet ihren Access Token über Neustarts hinweg weiter. Damit tritt der Server gegenüber homee als eigener Client auf und fordert nicht bei jeder Wiederverbindung einen neuen Token an. Ab 0.9.4 öffnet auch der Verbindungstest keine zweite Sitzung mehr, wenn die Integration bereits läuft; kurze Abbrüche werden mit einem begrenzten Wiederanmeldeabstand behandelt. Ab 0.9.5 erzwingt das Modul zusätzlich einen Single-Flight-Login: Solange ein Verbindungsaufbau läuft oder ein Socket aktiv ist, kann kein weiterer Anmeldeversuch beginnen. Erst ein vom Empfangsloop festgestellter und vollständig aufgeräumter Verbindungsbruch gibt den nächsten Versuch frei. Server 0.9.6 verarbeitet außerdem den von homee verwendeten `all`-Umschlag korrekt, sodass Geräte, Attribute und weitere Ressourcen aus `GET:all` angelegt werden. Ab 0.10.0 lösen fehlerhafte einzelne Nutzdaten ausdrücklich keinen Reconnect mehr aus; `GET:all` ist gegen Wiederholung innerhalb einer Minute geschützt. Im Webportal steht bei der homee-Integration zusätzlich eine manuelle WebSocket-Konsole mit Befehlsvorlagen und einem auf 100 Einträge begrenzten, nach Nachrichtentyp filterbaren Protokoll zur Verfügung. Ab 0.10.1 bestätigt das Webportal gespeicherte Integrationen sofort und zeigt während des asynchronen Starts „Verbindung wird aufgebaut“, statt nach einem lokalen HTTP-Timeout fälschlich einen leeren Serverfehler zu melden. Ab 0.10.2 normalisiert der Nachrichtenfilter Singular-, Plural- und `payload`-Umschläge. Der JSON-formatierte Livefeed ist standardmäßig pausiert, lässt sich ohne Seitenneuladen starten und stoppen und beendet sich nach 60 Sekunden oder beim Wechsel in den Hintergrund automatisch. Ab 0.10.3 besitzt der EnOcean-Anlerndialog eine eigene Profilsuche. FT55 Einfach- und Doppelwippe werden getrennt ausgewählt; je Wippe entstehen stabile Attributinstanzen für I/O gedrückt und losgelassen sowie ein zusätzlicher Energy-Harvesting-Kanal. Ab 0.10.4 aktualisiert die EnOcean-Webseite den Anlern-Countdown und die Geräteliste im Hintergrund. Ein neu empfangener Sender erscheint dadurch sofort, ohne die komplette Seite neu zu laden oder die Scrollposition zurückzusetzen. Ab 0.10.5 filtert die Gerätesuche in der E-Paper-Konfiguration die Attributauswahl anhand des Gerätenamens und baut das Dropdown browserübergreifend neu auf. Zusätzlich wird dringend empfohlen, in der offiziellen homee-App einen eigenen Benutzer nur für den SmartHomeBoard-Server anzulegen. Die persönliche homee-App und der dauerhaft verbundene Server verwenden dann getrennte Benutzer- und Geräteidentitäten und können sich nicht gegenseitig aus einer Sitzung verdrängen.

Wenn der Servermodus in der iOS-App aktiv ist, baut das iPad bewusst keine zweite direkte homee-Verbindung auf. Dadurch stammen alle iPads, Automationen und E-Paper aus demselben persistenten Datenbestand; auch bei geschlossener App bleiben die Werte aktuell.

Alle Integrationen lassen sich sowohl in der iOS-App als auch unter `/setup/integrations` anlegen, bearbeiten, testen und löschen. Beide Oberflächen arbeiten mit denselben API-Endpunkten und derselben SQLite-Datenbank.

## EnOcean USB300

Der EnOcean-Cube wird dem Container als serielle Schnittstelle `/dev/enocean` bereitgestellt. Die mitgelieferte Compose-Datei enthält bereits die Zuordnung für den beim Entwicklungssystem erkannten USB300. Auf einem anderen System wird der dort angezeigte stabile Gerätename über eine Datei `.env` neben der Compose-Datei gesetzt:

```bash
ls -l /dev/serial/by-id/
```

```text
ENOCEAN_DEVICE=/dev/serial/by-id/usb-EnOcean_GmbH_EnOcean_USB_300_DB_MEINE-SERIENNUMMER-if00-port0
```

Danach den Container neu bauen und starten:

```bash
docker compose up -d --build
```

In der App unter **Einstellungen → Lokaler Server → Integration hinzufügen → EnOcean USB300** die Integration speichern. Im Integrationseintrag wird zuerst das EEP-Geräteprofil ausgewählt und erst danach der Anlernmodus gestartet. Anschließend am Sensor die Lerntaste betätigen beziehungsweise einen batterielosen Taster oder Fenstergriff bedienen. Der erste bislang unbekannte Sender mit passender Telegrammfamilie wird persistent gespeichert und der Lernmodus sofort automatisch beendet. Bereits gespeicherte Sender-IDs bleiben bei späteren Lernvorgängen gesperrt, solange das Gerät nicht gelöscht wurde. In derselben App-Ansicht können Name und EEP bestehender Geräte geändert oder Geräte dauerhaft gelöscht werden.

Unter `http://IP-DES-PI:8400/setup/enocean` befindet sich die vollständige Webverwaltung. Dort können Geräte angelernt, dauerhaft gelöscht, umbenannt und einem anderen EEP-Profil zugeordnet werden. Die Seite zeigt außerdem Sender-ID, Empfangspegel, letztes Telegramm und Rohdaten. Ein durchsuchbarer Katalog enthält 64 Sensor- und Aktorprofile einschließlich typischer Eltako-Geräte.

Die erste Version empfängt Geräte. Unterstützt werden insbesondere D5-00-01, F6-02-01/02, F6-05-01/02, F6-10-00, A5-14-09, die A5-02-Temperaturprofile sowie A5-04-01/02, A5-06-02, A5-07-01/02/03 und A5-08-01/02/03. Unbekannte Profile erscheinen mit Rohwert, Sender-ID, EEP und Empfangsqualität und können dadurch später ergänzt werden. Für F6-Sender, die ihr Profil technisch nicht selbst übertragen, lässt sich das Standardprofil wählen oder das EEP nach dem Anlernen in der Webverwaltung ändern.

Eltako-Aktorprofile wie A5-38-08, D2-01-07, D2-01-12 sowie die Rückmeldeprofile A5-11-04, F6-3F-7F und A5-3F-7F sind im Katalog vorbereitet. Ein Katalogeintrag bedeutet noch keine Sendefreigabe: Aktoren benötigen zusätzlich eine profilgenaue Telegrammerzeugung, Sender-Basis-ID und einen kontrollierten Teach-in-Ablauf. Bis das pro Aktorfamilie implementiert und am realen Gerät geprüft ist, werden diese Profile bewusst nur als Rohdaten beziehungsweise „Katalog“ gekennzeichnet.

Konfigurationen, API-Schlüssel, Nodes, letzte Werte und Automationen liegen persistent in `data/smarthomeboard.sqlite3`.

## M5Paper-Displays

Die passende PlatformIO-Firmware liegt gemeinsam mit dem Server unter [`m5paper-firmware`](m5paper-firmware). Dadurch bleiben Serverprotokoll und Displayquellcode in derselben Version verfügbar. Die Firmware enthält keine WLAN-Zugangsdaten, feste Serveradresse, privaten IP-Adressen oder Gerätetokens; WLAN und Kopplung werden erst auf dem jeweiligen M5Paper eingerichtet und im ESP32-NVS gespeichert.

Der Server beantwortet die lokale Gerätesuche auf UDP-Port `8788`. Ein neues M5Paper registriert sich danach selbstständig und zeigt einen sechsstelligen Kopplungscode an. Unter `http://IP-DES-PI:8400/setup/displays` erscheint es und kann dort mit diesem Code benannt und gekoppelt werden. Erst nach Bestätigung des Codes wird das Display freigeschaltet.

Jedes Display besitzt eine stabile Geräte-ID und einen eigenen, zufällig erzeugten Gerätetoken. Dadurch können mehrere M5Paper unabhängig benannt und konfiguriert werden. Für jedes Gerät speichert der Server den Online-Status, die Firmwareversion, die individuelle Ansichtsdefinition und deren Versionsnummer. Das Display ruft nur seine eigene Konfiguration ab; der allgemeine Server-API-Schlüssel wird nicht auf dem M5Paper gespeichert.

Die Ansichtsdefinition ist JSON-basiert und kann unter anderem Aktualisierungszeit, Seiten und Widgets enthalten. Die Quellen der Widgets verweisen später auf die dauerhaft im Server laufenden Nodes und Attribute. Damit bleiben Aktualisierung und Berechnung unabhängig davon aktiv, ob das iPad online ist.

Ab Version 0.9.0 können sowohl die iOS-App als auch das Webportal für jedes gekoppelte M5Paper Name, Überschrift, Listen- oder Kachelansicht, den Schlaf-/Aktualisierungstakt sowie bis zu acht sortierbare Serverwerte konfigurieren. Die Attributauswahl ist nach Gerät, Attribut und Einheit durchsuchbar. Der Server löst die hinterlegten Node- und Attribut-IDs bei jedem Abruf in fertig formatierte Anzeigewerte auf und dekodiert URL-kodierte Einheiten wie `%20` oder `%25`. Das M5Paper benötigt deshalb keinen allgemeinen API-Schlüssel und das iPad muss für Aktualisierungen nicht erreichbar sein.

Das go-e-Modul kann neben numerischen IP-Adressen auch Bonjour-/mDNS-Namen wie `app.local` direkt aus dem Container auflösen. Der abweichende HTTP-Port wird weiterhin separat in der Integration eingestellt.

Die Einrichtungsseite zeigt außerdem die Zahl der geladenen Module, Integrationen und Geräte. Nach der Ersteinrichtung ist der bisherige API-Schlüssel einmal pro Browsersitzung erforderlich. Anschließend bleiben Servereinstellungen, Automationstests sowie die Modbus- und EnOcean-Verwaltung gemeinsam freigeschaltet. Das HttpOnly-Sitzungscookie enthält nicht den API-Schlüssel, besitzt keine dauerhafte Ablaufzeit und wird durch eine Änderung des API-Schlüssels automatisch ungültig. Eine Portänderung startet den Container automatisch neu; die Setup-Seite ist anschließend weiterhin unter Port `8400` erreichbar.

## Neues Modul hinzufügen

Ein Modul ist ein eigener Ordner unter `modules` mit einer Datei `module.py`:

```text
modules/
└── mein_geraet/
    └── module.py
```

Es exportiert genau zwei Funktionen:

```python
def manifest():
    return {
        "id": "mein-geraet",
        "name": "Mein Gerät",
        "version": "1.0.0",
        "icon": "sensor",
        "supportsDiscovery": False,
        "supportsMultipleInstances": True,
        "fields": [
            {"key": "host", "type": "text", "title": "IP-Adresse", "required": True}
        ]
    }

def create(configuration, context):
    return Adapter(configuration, context)
```

Der Adapter implementiert:

```python
class Adapter:
    async def start(self): ...
    async def stop(self): ...
    async def set_value(self, node_id, attribute_id, value): ...
```

Zustände werden mit `await context.publish_node(node)` veröffentlicht. IDs erzeugt ein Modul stabil mit:

```python
node_id = context.stable_node_id(external_id)
attribute_id = context.attribute_id(node_id, 1)
```

Danach genügt:

```bash
docker compose restart
```

Wenn das neue Modul zusätzliche Python-Pakete benötigt, diese in `requirements.txt` ergänzen und einmal neu bauen:

```bash
docker compose up -d --build
```

Die iOS-App lädt Manifest und Felder dynamisch. Ein App-Update ist für zusätzliche Module nicht erforderlich, solange diese das universelle Node-/Attribute-Modell verwenden.

## Modbus-Profil hinzufügen

Die Einrichtungsseite unter `http://IP-DES-PI:8400/setup` enthält den Bereich **Modbus-Templates verwalten**. Dort werden alle mitgelieferten Profile mit Registerzahl und Standard-Unit-ID angezeigt. Jedes Profil kann vollständig als JSON geöffnet und als Muster für ein eigenes Gerät verwendet werden.

Eigene Profile werden geprüft und persistent unter `/data/modbus-profiles` gespeichert. Sie bleiben dadurch auch bei einem Austausch oder Update des Containers erhalten. Nach dem Speichern startet der Container automatisch neu und das Profil erscheint in der dynamisch geladenen Profilauswahl der App.

Beim Verbinden lädt die App die vollständigen Profile über `GET /api/v1/modbus/profiles` und speichert sie in ihrer lokalen Konfiguration. Dadurch bleiben selbst angelegte Serverprofile auch dann in der direkten Modbus-Integration der App auswählbar, wenn der Server später deaktiviert oder vorübergehend nicht erreichbar ist.

In der App befindet sich unter **Einstellungen → Modbus TCP → Template-Editor** dieselbe Profilverwaltung. Vorhandene Profile können als Vergleich geöffnet oder als eigene Variante kopiert werden. Eigene App-Templates werden lokal persistent gespeichert und bei aktiver Serververbindung automatisch hochgeladen beziehungsweise gelöscht.

Mitgeliefert sind:

- Generischer Verbindungstest
- Victron Energy GX-System / Venus OS
- Victron Energy Solar Charger / MPPT über GX
- SMA Sunny Boy
- b-control / TQ Energy Manager EM300
- MENNEKES AMTRON 4You 500 / 4Business 700

Alternativ kann weiterhin eine JSON-Datei direkt in `modules/modbus/profiles` ergänzt werden. Der Serverkern und die App bleiben unverändert.

## API

- `GET /api/v1/health`
- `GET /api/v1/modules`
- `GET /api/v1/modbus/profiles`
- `POST/DELETE /api/v1/modbus/profiles`
- `GET/POST/PUT/DELETE /api/v1/integrations`
- `GET /api/v1/integrations/{id}/state`
- `POST /api/v1/integrations/{id}/actions/{action}`
- `GET /api/v1/nodes`
- `POST /api/v1/displays/register`
- `GET /api/v1/displays`
- `POST /api/v1/displays/{id}/pair`
- `PUT /api/v1/displays/{id}/configuration`
- `GET /api/v1/displays/device/{id}/configuration`
- `POST /api/v1/displays/device/{id}/heartbeat`
- `PUT /api/v1/nodes/{node}/attributes/{attribute}`
- `GET/PUT /api/v1/automations`
- `WS /api/v1/events`

Alle HTTP-Anfragen verwenden `Authorization: Bearer <API-Schlüssel>`. Der WebSocket erhält den Token als Query-Parameter. `/setup` läuft getrennt unter Port `8400` und bleibt im lokalen Netz erreichbar, verlangt nach der Ersteinrichtung aber den bisherigen Schlüssel, bevor Einstellungen geändert werden.

## Automationen

Die erste Serverversion unterstützt dauerhaft:

- Gerätewert als Auslöser
- Wertänderung absolut oder prozentual
- tägliche und einmalige Zeitpunkte
- Gerätewert- sowie Vor-/Nach-Zeitbedingungen
- verzögerte Aktionen
- erneutes Auslösen ersetzt einen laufenden Ablauf derselben Aktion
- Prüfung der Bedingungen beim Auslösen, Ausführen oder zu beiden Zeitpunkten
- Geräteattribute setzen

Darstellungsaktionen wie Pop-up, Ton oder Seitenwechsel werden als `client_action` an geöffnete Apps gesendet. Direkte Apple-Home-Verbindungen bleiben wegen Apples HomeKit-Rechten auf iOS/macOS lokal.

## Sicherheit

Im reinen Heimnetz schützt der API-Schlüssel vor unbeabsichtigtem Zugriff. Für Zugriffe über das Internet den Port nicht direkt freigeben, sondern VPN oder einen TLS-Reverse-Proxy einsetzen. Die optionale Umgebungsvariable `SHB_API_TOKEN` wird aus Kompatibilitätsgründen weiterhin unterstützt, sperrt aber Änderungen über die Webseite, solange sie gesetzt ist.

Server 0.10.6 verwendet für E-Paper-Attribute mit der Einheit `text` den dekodierten Inhalt des `data`-Feldes als Anzeigewert. Der numerische `current_value` und die Einheit `text` werden in diesem Fall nicht mehr auf dem Display ausgegeben.

Server 0.10.7 überträgt den E-Paper-Zeitstempel in der mit `SHB_TIMEZONE` konfigurierten lokalen Zeitzone. Mit der Voreinstellung `Europe/Berlin` werden Sommer- und Winterzeit automatisch korrekt berücksichtigt.

Server 0.11.0 ergänzt die persistente Roborock-Integration, gemeinsame Modulaktionen in iOS-App und Webportal sowie die gerätespezifische Darstellung und Steuerung in der App.

Server 0.11.1 erweitert Roborock um modellabhängige Reinigungsarten, Saugstufen, Wassermengen, Raumreinigung, Routinen, Punktreinigung und einen echten Stop-Befehl. Die iOS-App besitzt dafür eine eigene Dashboard-Vorlage und blendet nur die vom jeweiligen Gerät gemeldeten Möglichkeiten ein.

Server 0.11.2 ergänzt die zusammengesetzte Automationsaktion „Roborock reinigen“. Roboter, Reinigungsart, Saugstufe, Wassermenge und Ziel (komplett, Raum, Routine oder Punkt) werden gemeinsam gespeichert. Der Server setzt die gewählten Modi geschützt und nacheinander und startet die Reinigung erst danach; die Automation läuft damit auch ohne verbundenes iPad zuverlässig weiter.

Server 0.12.0 ergänzt Bosch Smart Home als vollständig lokale Serverintegration. Das einmalige Controller-Pairing erzeugt persistente Clientzertifikate; danach laufen Gerätesnapshot, Long-Poll-Liveupdates, steuerbare Attribute und Bosch-Szenarien unabhängig von iPad und Bosch-Cloud auf dem Server.

Server 0.12.1 merkt sich bei Roborock den zuletzt gestarteten Raum und die zuletzt gestartete Routine persistent. Beide Namen bleiben dadurch im Dashboard-Auswahlfeld sichtbar. Die Reinigungsart „Saugen und Wischen“ wird eindeutig als gleichzeitiger Modus bezeichnet; einen vom Gerät tatsächlich gemeldeten sequenziellen Modus zeigt der Server als „Erst saugen, dann wischen“ an. Bei Modellen ohne diesen Gerätemodus kann die entsprechende Abfolge als Roborock-Routine angelegt und anschließend unter „Routine starten“ gewählt werden.

Server 0.12.2 sendet die V1-Raumreinigung im von `python-roborock` 6.x erwarteten Segmentobjekt. Aktuelle Roboter verwerfen das ältere verschachtelte Listenformat teilweise ohne Fehlermeldung. Die iOS-Auswahl zeigt den angetippten Raum außerdem sofort an und gleicht ihn anschließend mit der Serverbestätigung ab.

Server 0.12.3 trennt bei Roborock Auswahl und Ausführung: Das Antippen eines Raums oder einer Routine speichert nur das Reinigungsziel. Erst der normale Startknopf führt die gewählte Raumreinigung beziehungsweise Routine aus. Mit „Gesamte Fläche“ wird wieder auf die normale Komplettreinigung umgeschaltet.

Server 0.13.0 ergänzt VELUX ACTIVE und MotionBlinds als persistente Serverintegrationen. VELUX übernimmt Rollläden, Position und Auf/Ab/Stopp über die VELUX-Cloud. MotionBlinds erkennt die Rollläden lokal am WLAN-Gateway, verarbeitet UDP-Live-Reports und fragt nach Befehlen den tatsächlichen Zustand aktiv nach. Beide Integrationen liefern dieselben Attribute wie die bisherigen direkten iOS-Verbindungen und stehen dadurch auch ohne verbundenes iPad für Dashboard, E-Paper und Automationen bereit.

Für VELUX werden E-Mail, Passwort und Abfrageintervall eingetragen. Zugangsdaten und erneuerte OAuth-Tokens verbleiben in der lokalen Serverdatenbank; die Verbindung selbst benötigt Internetzugriff zur VELUX-Cloud.

Für MotionBlinds werden die feste IP-Adresse des WLAN-Gateways und dessen Secret Key eingetragen. Der Server verwendet UDP-Port 32100 für Befehle, standardmäßig Port 32200 für Antworten und Multicast `238.0.0.18:32101` für Live-Reports. Bei Docker ist deshalb `network_mode: host` erforderlich, wie in der mitgelieferten Compose-Konfiguration vorgesehen.

Server 0.14.0 ergänzt im Webportal einen vollständigen No-Code-Editor für persistente Serverautomationen. Auslöser können Gerätewerte, Wertänderungen, tägliche Uhrzeiten oder einmalige Zeitpunkte sein. UND-Bedingungen unterstützen Gerätewerte sowie Zeitfenster. Als lokale Aktionen stehen das Setzen und Umschalten steuerbarer Attribute sowie die zusammengesetzte Roborock-Reinigung mit Modus, Saugstufe, Wassermenge und Ziel zur Verfügung. Mehrere Auslöser, Bedingungen und verzögerte Aktionen können direkt im Browser ergänzt, bearbeitet, getestet und gelöscht werden.

Eine im Webportal neu angelegte oder dort bearbeitete Regel wird serververwaltet. Die iPad-Synchronisierung aktualisiert weiterhin App-Regeln, überschreibt serververwaltete Regeln aber nicht und löscht sie auch nicht. Dadurch kann der Server vollständig ohne iPad konfiguriert werden, während beide Oberflächen parallel nutzbar bleiben.

Server 0.14.1 dekodiert URL-kodierte Anzeigenamen, Attributnamen, Einheiten und Auswahltexte im Webportal zentral. Dadurch erscheinen beispielsweise `%20` als Leerzeichen und UTF-8-Sequenzen wieder als lesbare Umlaute, ohne die persistent gespeicherten Rohwerte oder Gerätezuordnungen zu verändern.

## Z-Wave

Server 0.15.0 ergänzt Z-Wave als persistente lokale Integration. Der offizielle Z-Wave-JS-Treiber besitzt exklusiv den USB-Stick; SmartHomeBoard verbindet sich lokal mit dessen WebSocket-Server und übernimmt Geräte, Livewerte, schreibbare Attribute, Anlernen und Ausschließen. S2-Sicherheitsklassen werden bestätigt, die fünfstellige Geräte-PIN kann bei Bedarf direkt im SmartHomeBoard-Webportal eingegeben werden.

Server 0.15.1 übernimmt zusätzlich die von jedem Gerät gemeldete Hersteller-ID, den Produkttyp, die Produkt-ID und Firmwareversion sowie die von der offiziellen Z-Wave-JS Config DB aufgelösten Hersteller-, Produkt- und Geräteklassenbezeichnungen. Alle lesbaren skalaren Eigenschaften und Endpunkte werden weiterhin automatisch als SmartHomeBoard-Attribute angelegt; geheime Schlüssel und interne Konfigurationswerte bleiben ausgeblendet.

Server 0.15.2 synchronisiert den angezeigten Z-Wave-Anlernstatus direkt mit dem `inclusionState` des Controllers. Erfolgreiches Anlernen, Zeitablauf, manueller Abbruch, Fehler und Ausschluss setzen den Webportalstatus dadurch zuverlässig zurück; ein vom Controller abgelehnter Start wird nicht mehr fälschlich als aktiv angezeigt.

Server 0.15.3 führt bei Bosch Smart Home den virtuellen `roomClimateControl` und die zugehörigen Raum- oder Heizkörperthermostate anhand ihrer Bosch-Raum-ID zu einem Heizungsgerät zusammen. Dieses enthält gemeinsam Isttemperatur, Luftfeuchtigkeit, Solltemperatur, den veränderbaren Betriebsmodus, Boost und Sommermodus. Die Node-ID des physischen Thermostats bleibt erhalten; der überholte virtuelle Doppel-Node wird beim nächsten Einlesen entfernt.

Auf dem Docker-Host zuerst den stabilen Stick-Pfad bestimmen:

```bash
ls -l /dev/serial/by-id/
```

Danach im Projektverzeichnis eine `.env` mit diesem Pfad anlegen, beispielsweise:

```dotenv
ZWAVE_DEVICE=/dev/serial/by-id/usb-0658_0200-if00
ZWAVE_SESSION_SECRET=eine-lange-zufaellige-zeichenfolge
```

Z-Wave JS UI und SmartHomeBoard anschließend gemeinsam starten:

```bash
docker compose --profile zwave up -d --build
```

Die Z-Wave-JS-Oberfläche ist im lokalen Netz unter `http://SERVER-IP:8091` erreichbar. Dort einmalig die vier Z-Wave-Sicherheitsschlüssel erzeugen und den Z-Wave-JS-WebSocket-Server auf Port `3000` aktivieren. Danach im SmartHomeBoard-Webportal eine Z-Wave-Integration mit `ws://127.0.0.1:3000` anlegen. Der WebSocket-Server besitzt keine eigene Authentifizierung und sollte deshalb nicht über Router oder Reverse Proxy ins Internet freigegeben werden.
