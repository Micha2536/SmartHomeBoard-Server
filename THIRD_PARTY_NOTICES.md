# Lizenzumfang und Drittanbieterhinweise

Stand: 25. Oktober 2026; direkte Python-Abhängigkeiten aus `requirements.txt` der Serverversion 0.20.26.

## Eigener Projektcode

Die [MIT-Lizenz](LICENSE) gilt für den eigenen Server-, Modul- und Firmwarecode sowie die eigene Dokumentation von Michael Mann in diesem Repository. Fremde Bibliotheken, Frameworks, Datenkataloge, Grafiken und Containerimages behalten ihre jeweiligen Rechte und Lizenzbedingungen. Die Projektlizenz lizenziert diese Bestandteile nicht um.

## Direkte Python-Abhängigkeiten

Die folgenden Angaben wurden gegen die Paketmetadaten bzw. mitgelieferten Lizenzhinweise der angegebenen Versionen geprüft. Die verlinkten Upstream-Projekte enthalten die jeweiligen Lizenztexte und Urheberhinweise.

| Paket | Version | Lizenz |
|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | 0.116.1 | MIT |
| [Uvicorn](https://github.com/encode/uvicorn) | 0.35.0 | BSD-3-Clause |
| [HTTPX](https://github.com/encode/httpx) | 0.28.1 | BSD-3-Clause |
| [h2](https://github.com/python-hyper/h2) | 4.2.0 | MIT |
| [pymodbus](https://github.com/pymodbus-dev/pymodbus) | 3.8.6 | BSD-3-Clause |
| [pySerial](https://github.com/pyserial/pyserial) | 3.5 | BSD-3-Clause |
| [websockets](https://github.com/python-websockets/websockets) | 15.0.1 | BSD-3-Clause |
| [python-roborock](https://github.com/python-roborock/python-roborock) | 6.2.1 | Apache-2.0 |
| [boschshcpy](https://github.com/tschamm/boschshcpy) | 0.6.1 | BSD-3-Clause |
| [cryptography](https://github.com/pyca/cryptography) | 45.0.6 | Apache-2.0 OR BSD-3-Clause |
| [zwave-js-server-python](https://github.com/home-assistant-libs/zwave-js-server-python) | 0.72.0 | Apache-2.0 |
| [SoCo](https://github.com/SoCo/SoCo) | 0.31.0 | MIT |
| [gTTS](https://github.com/pndurette/gTTS) | 2.5.4 | MIT |
| [Mutagen](https://github.com/quodlibet/mutagen/tree/release-1.48.1) | 1.48.1 | GPL-2.0-or-later |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 1.30.0 | MIT |

**Besonders zu beachten:** Das Sonos-Modul importiert Mutagen zum Ermitteln der MP3-Dauer. Mutagen steht unter [GPL Version 2 oder später](https://github.com/quodlibet/mutagen/blob/release-1.48.1/COPYING). Die MIT-Erklärung für eigenen Code macht das kombinierte Serverpaket daher nicht zu einer ausschließlich MIT-lizenzierten Distribution. Bei einer Weitergabe müssen auch die auf die Kombination anwendbaren GPL-Pflichten, Lizenztexte und Quellcodeanforderungen berücksichtigt werden.

Die Tabelle ist keine vollständige Liste aller transitiven Abhängigkeiten. Auch diese können andere Bedingungen haben, etwa LGPL, MPL oder duale Lizenzen. Maßgeblich sind die tatsächlich installierten Versionen samt vollständigen Lizenz- und Urheberhinweisen.

## M5Paper-Firmware

Der eigene Firmwarecode steht unter MIT. Die in `m5paper-firmware/platformio.ini` eingebundenen Komponenten behalten ihre eigenen Lizenzen:

- [M5EPD](https://github.com/m5stack/M5EPD/blob/main/LICENSE), Versionsbereich ab 0.1.5: MIT.
- [ArduinoJson](https://github.com/bblanchon/ArduinoJson/blob/7.x/LICENSE.txt), Versionsbereich ab 7.0.0: MIT.
- [Arduino-ESP32](https://github.com/espressif/arduino-esp32/blob/master/LICENSE.md), Frameworkpaket 3.20004.0: LGPL-2.1-or-later; zusätzliche Frameworkkomponenten können weitere Lizenzen haben.

Bei der Weitergabe fertiger Firmware sind auch die Bedingungen der tatsächlich eingebauten Bibliotheken und Frameworkkomponenten zu beachten. Die Versionsbereiche sind keine vollständige reproduzierbare Lizenzbestandsliste eines einzelnen Firmware-Builds.

## homee-Inhalte und Marken

`server/homee_enums.py` wurde aus dem homee-Katalog der App erzeugt. Übernommene Kataloginhalte sind von der projektseitigen MIT-Erklärung ausgenommen; eigene Integrationslogik bleibt davon getrennt.

In der zugehörigen SmartHomeBoard-App sind homee-Grafiken mit Genehmigung der **homee GmbH** integriert. Diese Genehmigung wird durch die MIT-Lizenz nicht erweitert und stellt keine allgemeine MIT-Freigabe der Grafiken oder Datenkataloge dar. Namen und Marken verbleiben bei den jeweiligen Rechteinhabern.

## Container und Weitergabe

Das Basisimage `python:3.13-slim`, die darin enthaltenen Betriebssystempakete und das optionale Image `zwavejs/zwave-js-ui:11` sind eigenständige Drittanbieterbestandteile. Auch hierfür gelten die jeweiligen Bedingungen.

Diese Übersicht ist keine vollständige Lizenzprüfung eines Containerimages oder einer Firmwaredistribution. Beim Weitergeben müssen die tatsächlich enthaltenen Lizenztexte, Copyright-/NOTICE-Angaben und gegebenenfalls Quellcode- und sonstigen Pflichten erhalten bzw. erfüllt werden.
