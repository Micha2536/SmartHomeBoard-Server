# SmartHomeBoard M5Paper-Firmware

PlatformIO-Firmware zur Kopplung eines oder mehrerer M5Paper-Displays mit dem SmartHomeBoard-Server. Sie gehört zum Repository [`Micha2536/SmartHomeBoard-Server`](https://github.com/Micha2536/SmartHomeBoard-Server) und liegt dort im Ordner `m5paper-firmware`.

## Datenschutz und Zugangsdaten

Der veröffentlichte Quellcode enthält keine private WLAN-Kennung, kein WLAN-Passwort, keine feste Serveradresse, keine private IP-Adresse und keinen Gerätetoken. Das M5Paper fragt WLAN und Passwort über seine Touchoberfläche ab. Diese Daten sowie der nach der Kopplung vom Server ausgestellte Gerätetoken werden ausschließlich im NVS des jeweiligen ESP32 gespeichert und gehören nicht zum PlatformIO-Projekt.

## Aktueller Stand

- WLAN-Netze per Touch auswählen
- nach Empfangsstärke sortierte Netzwerkliste
- QWERTZ-Bildschirmtastatur mit Groß- und Sonderzeichen
- Zugangsdaten erst nach erfolgreicher Verbindung im ESP32-NVS speichern
- bei späteren Starts automatisch mit dem gespeicherten WLAN verbinden
- mittlere Taste beim Einschalten mindestens 700 ms halten, um die WLAN-Auswahl erneut zu öffnen

Nach erfolgreicher WLAN-Einrichtung sucht das M5Paper den SmartHomeBoard-Server automatisch per UDP im lokalen Netz. Es registriert sich mit einer stabilen, aus der WLAN-MAC abgeleiteten Geräte-ID und speichert den vom Server ausgestellten Gerätetoken im NVS. Ein neues Display zeigt anschließend den sechsstelligen Kopplungscode an. Bereits gekoppelte Displays erhalten ihre individuelle Konfigurationsversion.

Im normalen Aktualisierungsablauf bleibt das bisherige E-Paper-Bild während WLAN- und Serververbindung sichtbar. Nach dem Zeichnen schaltet sich das Gerät im Akkubetrieb vollständig ab und wird über den BM8563-RTC nach dem konfigurierten Intervall wieder eingeschaltet. Bei USB-Versorgung dient zusätzlich der interne ESP32-Deep-Sleep-Timer als Fallback.

## Build

Das Repository klonen und in den Firmwareordner wechseln:

```sh
git clone https://github.com/Micha2536/SmartHomeBoard-Server.git
cd SmartHomeBoard-Server/m5paper-firmware
```

Anschließend mit installiertem PlatformIO bauen:

```sh
platformio run
```

## Upload

```sh
platformio run --target upload
```

Danach das M5Paper neu starten. Beim ersten Start erscheint die WLAN-Auswahl. Nach erfolgreicher Verbindung wird der SmartHomeBoard-Server automatisch im lokalen Netz gefunden und ein Kopplungscode angezeigt. Die weitere Zuordnung erfolgt in der iOS-App unter **Einstellungen → Lokaler Server → M5Paper-Displays** oder im Serverportal unter `http://IP-DES-SERVERS:8400/setup/displays`.
