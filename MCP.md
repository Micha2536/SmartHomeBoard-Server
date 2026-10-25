# MCP in SHB (Server 0.20.26 / iOS-Build 318)

## Echte SHB-Serverautomationen über MCP

Ab 0.20.26 sind **Serverautomationen** ein eigener MCP-Bereich, getrennt von Homeegrammen und HG-Entwürfen. Sie werden in der regulären Automationsverwaltung des Servers gespeichert, bleiben bei App-Synchronisierung erhalten und laufen auf SHB – nicht auf homee. Geräte verschiedener aktiver Servermodule können gemeinsam verwendet werden. Dafür **SHB-Geräte-/Attribut-IDs** aus `list_devices` verwenden, niemals die Original-homee-IDs aus `get_homee_catalog`. Serverautomationen haben eine eigene `automation_id` (UUID); eine HG-Nummer ist keine Automations-ID.

In App-Build 318 unter **Lokaler Server → KI / MCP → Server-Automationen** zwei unabhängige Rechte bewusst einschalten und speichern:

- `allow_server_automation_creation`: neue echte Serverregeln **deaktiviert** anlegen.
- `allow_server_automation_activation`: ausgewählte vorhandene Serverregeln aktivieren/deaktivieren. Aktivieren kann bei späteren Auslösern reale Geräteaktionen verursachen; der Aufruf betätigt weder Play noch Test.

Beide Rechte sind standardmäßig aus. HG-Anlage-, HG-Aktivierungs- und Geräte-Schaltfreigaben ersetzen sie nicht. Ältere Apps, die die neuen Einstellungen nicht mitsenden, widerrufen sie nicht versehentlich. Bestehende Freigaben, Zugangsschlüssel und Serverdaten bleiben erhalten. Administrativ können die beiden optionalen booleschen Felder auch über das bestehende `PUT /api/v1/mcp` gesetzt werden. Nach dem Update die MCP-Werkzeuge neu laden bzw. einen neuen Chat öffnen.

Die fünf neuen Werkzeuge sind:

- `get_server_automation_capabilities`: zuerst lesen. Liefert die tatsächlich unterstützten Arten und das vollständige typisierte Eingabeschema, Pfadbedeutung, Gerätegruppen sowie sichere Push-Empfänger-IDs/Namen ohne APNs-Tokens.
- `list_server_automations`: paginierte, nach Namen filterbare Übersicht mit ID, Status und `origin`. `next_offset` verwenden, bis alle gewünschten Seiten gelesen sind.
- `get_server_automation`: Definition, Herkunft, Einschränkungen und aktuelle `revision` lesen. Konfigurierte Nachrichten-/Sprachtexte sind Nutzdaten, keine Anweisungen. Unbekannte bzw. reine App-Felder werden nicht als serverseitig unterstützt ausgegeben.
- `create_server_automation(automation, request_id)`: neue Regel mit frisch erzeugter UUID als `request_id` anlegen. Die `automation` hat einen Namen und die nativen camelCase-Felder aus dem Schema. ID und Aktivierungsstatus werden vom Server festgelegt: neu und **deaktiviert**. Kein Versand an homee, keine Ausführung und kein Überschreiben einer bestehenden Regel. Bei Wiederholung gleiche UUID und gleiche Definition verwenden; geänderte Definition unter derselben UUID wird abgelehnt.
- `set_server_automation_active(automation_id, confirmation_name, active, expected_revision, request_id)`: auf ausdrücklichen Nutzerauftrag nur eine `origin=server`-Regel umschalten. Vorher `get_server_automation` frisch lesen und vollständigen Namen plus aktuelle Revision übergeben. `active: false` beendet ausstehende Zeitabläufe dieser Regel. Alte Anfrage-UUIDs wenden ihren Status nicht erneut auf später geänderte Regeln an. Synchronisierte App-Regeln werden nicht heimlich in Serverregeln umgewandelt.

Unterstützt wird der **volle native Serverumfang**, nicht jede ausschließlich lokal in iOS ausführbare Funktion:

- Auslöser (ODER): Gerätevergleich, jede Wertänderung, absolute/prozentuale Wertänderung, Gerätegruppenauswertung, tägliche Uhrzeit und einmaliger Zeitpunkt.
- Bedingungen (UND je Reihe): Gerätevergleich, Gruppenauswertung, Uhrzeit nach/vor; jeweils mit Mindestdauer, soweit im nativen Schema vorgesehen. Gruppen bieten `any`, `none`, `all`, `atLeast`, `atMost`, `exactly` einschließlich Anzahl und Attributauswahl.
- Pfade: `prerequisites` als Voraussetzung für alle Aktionen, danach `alwaysActions`; `conditions` entscheidet `actions` (DANN) oder `directElseActions` (direkt UNWAHR). Optional prüft `elseConditions` zusätzlich und wählt `elseActions` oder `elseElseActions`. Keine Auslöser sind für manuell/indirekt aufrufbare Regeln ebenfalls zulässig; mindestens eine Aktion ist erforderlich.
- Aktionen: Gerätewert setzen, Schalter umschalten, Roborock-Reinigung (Fläche/Raum/Routine/Punkt und optionale Reinigungswerte), Sonos-Sprachausgabe (Text, Lautstärke, Wiedergabe wiederherstellen), andere Automation steuern (Play/Stop/Aktivieren/Deaktivieren) und Server-Push (Titel, Text, ausgewählte Empfänger und optionaler Gerätewert). Dies sind **gespeicherte zukünftige Aktionen**, keine direkten MCP-Ausführungsbefehle.
- Für alle Pfade: Aktionsverzögerungen, `cooldownSeconds`, `conditionValidation` mit `triggerTime`, `executionTime` oder `both` und die nativen Haltezeiten. Die zentrale Servervalidierung wird mit dem Webeditor geteilt; ungültige Geräte, Werte, Gruppen oder Empfänger werden nicht still durch andere ersetzt.

Die Laufzeitgrenzen stehen zusätzlich in `runtime_limits`: Positive Auslöser-Haltezeiten sind nur für Geräte- und Gruppen-Zustandsauslöser (`attribute`, `groupAttribute`) vorgesehen. Bei Änderungs- und Zeit-Auslösern muss `holdSeconds` null Sekunden sein; eine Aktionsverzögerung ist unabhängig davon möglich. Zeit-Auslöser unterstützen tägliche Uhrzeit oder einen einmaligen Zeitpunkt mit Zeitzone, keine nicht implementierten Wochen-/Monats-RRULEs. Sonst würden Einstellungen lediglich gespeichert, aber nicht wie versprochen ausgeführt; deshalb lehnt MCP sie ausdrücklich ab. Zustand und Automationsjournal werden gemeinsam in einer kurzen SQLite-Transaktion gespeichert; der Schutz gegen parallele Web-/App-Änderungen gilt auch bei Aktivierung. Bis zu 100.000 Anfragen werden ohne Verdrängen alter UUIDs behalten.

Smartwidgets, lokale Pop-ups, Dashboard-Seiten, Töne, Displayhelligkeit, Widget-Sichtbarkeit und ausschließlich appseitig weitergeleitete HG-Aktionen sind keine nativ auf dem Server ausgeführten Möglichkeiten und werden hier ausdrücklich abgelehnt. Zusätzliche direkte Play-/Test-, Bearbeitungs- oder Löschwerkzeuge für Serverregeln werden mit diesem Update nicht eingeführt. Bestehende HG-Werkzeuge und ihre Pfadzuordnung bleiben unverändert.

## Homeegramme als Aktion und Auslöser

Ab 0.20.24 unterstützen die Anlege-/Entwurfswerkzeuge, `edit_homeegram` und die gemeinsame grafische Ansicht native HG-Verweise. Eine HG-Aktion kann ein Ziel-HG **aktivieren**, **deaktivieren**, **abspielen** oder **stoppen**. Ein HG-Auslöser kann auf **Wird ausgelöst**, **Wird gestoppt**, **Wird aktiviert** oder **Wird deaktiviert** des Quell-HGs reagieren. Die Namen, Original-IDs und Ereignisse werden beim Lesen aufgelöst; fehlende Verweise bleiben sichtbar. Für Auswahl und Prüfung die HG-Liste genau derselben homee-Integration benutzen.

Für `create_homeegram`, `create_homeegram_draft` und den gemeinsamen `WorkspaceDraft` gilt diese Form (IDs sind nur Beispiele):

```json
{
  "name": "HG-Verknüpfung",
  "manual_play": false,
  "triggers": [{"type": "homeegram", "source_homeegram_id": 810, "event": "played"}],
  "actions": [{"type": "homeegram", "target_homeegram_id": 811, "operation": "stop", "branch": 1, "delay": 5}]
}
```

`event` ist `activated`, `deactivated`, `played` oder `stopped`; `played` entspricht homees „Wird ausgelöst“ und ist der Standard für einen HG-Auslöser. `operation` ist `activate`, `deactivate`, `play` oder `stop`. Geräteauslöser und Geräteaktionen behalten ihre bisherige Form und dürfen in denselben Listen stehen. Bei HG-Verweisen keine Geräte-/Attribut-IDs oder Zahlenwerte hinzufügen. `manual_play` ist davon unabhängig: Es beschreibt den eigenen manuellen Play-Auslöser, nicht das Ereignis eines anderen HGs.

Zum Bearbeiten verwenden native Änderungen `kind: homeegram_actions` mit `target_homeegram_id`, `homeegram_event`, `branch` und `delay` beziehungsweise `kind: homeegram_triggers` mit `source_homeegram_id` und `homeegram_event`. Die Codes sind jeweils 1=Aktivieren/Aktiviert, 2=Deaktivieren/Deaktiviert, 3=Abspielen/Ausgelöst, 4=Stoppen/Gestoppt. Neue Aktionen brauchen einen expliziten Pfad (0=Immer, 1=Dann, 2=Sonst). Ereignisänderungen vorhandener HG-Aktionen erhalten vorhandenen Pfad und Verzögerung, einschließlich der Abwesenheit dieser Felder in alten HGs.

Dies konfiguriert zukünftige Abläufe und ist **kein direkter Ausführungsbefehl**. Das Anlegen benötigt weiterhin nur die bestehende Anlagefreigabe, das Bearbeiten die Bearbeitungsfreigabe. Kein Recht wird automatisch eingeschaltet. Neue und bearbeitete HGs bleiben deaktiviert; referenzierte HGs werden beim Speichern nicht aktiviert, abgespielt oder gestoppt. Direktes Aktivieren/Deaktivieren über `set_homeegram_active` bleibt ein getrennter Weg. Neue Direkt-Play-/Stop-Werkzeuge werden damit nicht eingeführt. iOS-Build 317 bleibt unverändert; nach dem Serverupdate die MCP-Werkzeugliste neu laden.

## Startfehler und vollständiges Update

`ModuleNotFoundError: No module named 'server.homeegram_editing'` bedeutet einen unvollständigen Zwischenstand. Das vollständige Paket ab 0.20.22 enthält `homeegram_editing.py`, `homeegram_revision.py`, die zugehörigen Adaptermethoden und die interaktive Oberfläche. Nicht nur einzelne Python-Dateien austauschen. `data`, `secrets` und `.env` behalten; dann `docker compose up -d --build` ausführen (mit Z-Wave zusätzlich `--profile zwave`). Der Starttest prüft Import und vollständigen Anwendungsstart in einem frischen Prozess mit isolierten Testdaten.

## Mehrere Homeegramme gezielt löschen

Ab Server 0.20.23 kann `delete_homeegrams` auf ausdrücklichen Nutzerauftrag 1 bis 50 Original-HGs einer einzelnen homee-Quelle löschen. Jedes Ziel wird mit positiver `homeegram_id` und exakt URL-dekodiertem `confirmation_name` einzeln aufgeführt; dazu gehört eine neue UUID als `request_id` für den gesamten Auftrag. Keine Platzhalter, keine Wildcards und keine automatische Bereinigung. Die bisherige Freigabe `allow_homee_deletion` gilt auch hierfür; es gibt weder eine neue Freigabe noch eine automatische Aktivierung des Löschrechts. iOS-Build 317 bleibt unverändert.

Vor dem ersten Löschbefehl lädt der Server den Bestand frisch und prüft alle aufgeführten Ziele und Namen. Ist ein Ziel nicht eindeutig oder falsch benannt, beginnt keine Löschung. Danach arbeitet er die Liste nacheinander ab: jedes vollständige Original vor seinem Löschbefehl lokal sichern und anschließend die Entfernung durch homee bestätigen. Bei der ersten fehlgeschlagenen oder unklaren Löschung stoppt der Auftrag; noch nicht bearbeitete Ziele werden als `not_attempted` ausgegeben. Die Ergebnisliste zeigt den Stand je HG. Das ist keine atomare Transaktion: bereits bestätigte Löschungen werden bei einem späteren Fehler nicht rückgängig gemacht. Verweise aus anderen HGs können ungültig werden.

Ab 0.20.25 wird vor jedem neuen Einzelauftrag erneut vollständig gelesen. Eine verkürzte HG-Liste in der vorherigen DELETE-Antwort reicht zwar als Bestätigung der Abwesenheit, aber nicht als Inhaltsvorprüfung für das nächste HG. Automatische Änderungen an `order` gelten nicht als Bearbeitung. Außerdem dürfen ursprünglich vorhandene HG-Verweiszeilen exakt entfernt sein, wenn das referenzierte HG bereits in diesem selben Auftrag bestätigt gelöscht wurde. Alle verbleibenden Zeilen müssen unverändert und in derselben Reihenfolge vorliegen; neue/geänderte Verweise, Änderungen an Aktionen, Bedingungen, Namen, Erstellungszeit oder unbekannten Feldern stoppen weiterhin. Die strengere normale Editor-Revision bleibt unverändert. Zusätzliche Vergleichsdaten enthalten nur Hashes und Referenz-IDs, keine zweite Rohdatenkopie privater Nachrichten oder URLs.

Bei einem Stopp enthält das betroffene Ergebnis einen sicheren `error_code`: `selection_changed`, `incomplete_homeegram`, `confirmation_timeout`, `connection_unavailable` oder `deletion_not_confirmed`. Das Audit nennt die betroffene HG-ID und diesen Code, aber keine privaten HG-Inhalte. Der Status bleibt aus Kompatibilitätsgründen `uncertain`; die späteren Ziele bleiben `not_attempted`. Alte gespeicherte Aufträge vor 0.20.25 haben nur die frühere Vollrevision und werden nicht still aus aktuellen Daten neu bestätigt. Bei einem solchen alten Auswahlkonflikt die verbliebenen HGs neu lesen und die gewünschte Restliste ausdrücklich neu bestätigen; unklare native Löschaufträge niemals blind erneut senden.

Bei einer unterbrochenen oder unklaren Antwort dieselbe Batch-UUID und exakt dieselbe geordnete Zielliste verwenden. Aus der Batch-UUID abgeleitete stabile Einzel-UUIDs verhindern doppelte Löschbefehle; ein unklarer Einzelauftrag wird nur noch geprüft und nicht blind erneut gesendet. Eine geänderte Liste mit derselben UUID wird abgelehnt. Das bestehende `delete_homeegram` sowie die bestätigte Einzel-Löschung in der administrativen App-API bleiben verfügbar. Nach dem Serverupdate die MCP-Werkzeugliste neu laden.

## Interaktive Homeegramm-Ansicht

Ab 0.20.23 enthalten `get_homeegram`, `show_homeegram` und `get_homeegram_workspace` auch aufgelöste Gruppen-Auslöser, -Bedingungen und -Aktionen. Zu jedem Gruppenelement werden Name und ID der Gruppe, ihre Geräte aus `relationships`, die zum `attribute_type` passenden Eigenschaften mit Original-IDs, Cachewerte und lesbare Zielzustände ausgegeben. Die grafische Ansicht zeigt die Zusammenfassung am Knoten; ein Klick zeigt alle Mitglieder und Eigenschaften. Fehlende Gruppen, Geräte, Eigenschaften oder Relationships werden ausdrücklich markiert, nicht als bekannte leere Gruppe ausgegeben. HG-Mitglieder werden getrennt von Geräten geführt.

Gruppenbedingungen behalten ihren genauen Umfang: kein Gerät, alle Geräte, mindestens eines oder nicht alle. Gruppen-Auslöser beschreiben das Ereignis eines passenden Geräts; Gruppenaktionen zeigen Setzen, Umschalten, Quellwert übernehmen oder Erhöhen/Verringern statt Zahlen ohne Bedeutung. Unbekannte Codes bleiben sichtbar unbekannt. Das ist keine Live-Auswertung und keine Bestätigung einer Geräteaktion. Gruppen sind in dieser MCP-Webansicht nur lesbar; sie werden beim Anzeigen weder verändert noch ausgeführt. Gruppen und Relationships werden bei Verbindungsaufbau sowie ausdrücklich angefordertem Vollabruf nachgeladen, nicht ständig gepollt.

`show_homeegram` öffnet eine echte MCP-App, keine bloße Textskizze. Ohne ID zeigt sie die Übersicht, mit `homeegram_id` ein Original nur lesend, mit `draft_id` einen gemeinsam bearbeitbaren SHB-Entwurf. Die HTML-Resource wird als `text/html;profile=mcp-app` unter `ui://shb/homeegram/v1.html` ausgeliefert und per `_meta.ui.resourceUri` mit dem Werkzeug verknüpft. Es werden keine API-Schlüssel in HTML hinterlegt; Aufrufe laufen über die MCP-Apps-Host-Bridge.

Für die Zusammenarbeit: `create_homeegram_workspace`, dann `show_homeegram(draft_id)`. Nutzer und KI verwenden `get_homeegram_workspace` und `update_homeegram_workspace` mit derselben Entwurf-ID und der aktuellen Revision. Leere Entwürfe sind möglich; Konflikte werden nicht überschrieben. Klicks auf Knoten öffnen deren Felder; Plus/Entfernen ändert nur den Entwurf. `deploy_homeegram_workspace` erstellt erst nach bewusster Bestätigung ein neues deaktiviertes HG. Originale werden in dieser Webansicht nicht direkt editiert; dafür dient das getrennt freigegebene `edit_homeegram`.

Ein reiner Text-MCP-Client zeigt keine interaktive Oberfläche. Nach dem Update in einem MCP-Apps-fähigen Chat-Client die SHB-Verbindung/Werkzeugliste neu laden und ausdrücklich „Homeegramm grafisch öffnen“ anfragen. Ein bestehender `get_homeegram`-Aufruf bleibt ein Datenwerkzeug und öffnet absichtlich keine neue Oberfläche. Ein Serverupdate allein richtet weder eine Cloud-Verbindung noch OAuth ein.

## Vorhandene HGs aktivieren und bearbeiten

`set_homeegram_active` setzt auf ausdrücklichen Nutzerauftrag `active: true` oder `false`, ohne Play. Es benötigt die neue separate Freigabe `allow_homee_activation`.

`edit_homeegram` benötigt `allow_homee_editing`, die Original-ID, den exakt dekodierten Namen und `expected_revision` aus `get_homeegram`. Änderungen sind einzelne `add`, `update` oder `remove`-Operationen mit `section`, `kind`, bei bestehenden Elementen `element_id` sowie `parameters` bei add/update. Geräte-, Zeit-, Play- sowie HG-Auslöser/-Aktionen sind anlegbar/bearbeitbar; andere bekannte native Kategorien sind gezielt entfernbar und bleiben sonst unverändert erhalten. Nicht unterstützte Änderungen werden ausdrücklich abgelehnt. Vor Änderungen wird gesichert und deaktiviert; das HG bleibt deaktiviert. Eine erneute Aktivierung ist eine eigene bewusste Aktion. Neue Freigaben sind standardmäßig aus und können ab iOS-Build 317 unter KI/MCP gesetzt werden.

Neue Bedingungen brauchen explizit `branch: 0` (Immer-Voraussetzung für alle Aktionen) oder `branch: 1` (die gemeinsame Wenn-Reihe). Aktionen verwenden 0=Immer nach Voraussetzungen, 1=Dann, 2=Sonst. Beim Ändern bleiben nicht angegebene Pfad-/Prüfzeitpunkt-Felder erhalten; es gibt keine verschachtelten homee-Bedingungszweige. UUID-Wiederholungen bestätigen nur den vorhandenen Auftrag und senden keine unklaren Änderungen blind erneut. Die vollständige Sicherung bleibt lokal im Modulzustand, nicht im MCP-Ergebnis.

## Einrichtung

1. Server mit diesem Quellstand neu bauen: `docker compose up -d --build`. Mit Z-Wave: `docker compose --profile zwave up -d --build`. Vorhandene Verzeichnisse `data`, `secrets` und `.env` behalten.
2. Einen administrativen API-Schlüssel in der Server-Einrichtung setzen. Ohne diesen sind auch die MCP-Verwaltungsendpunkte gesperrt.
3. In der aktualisierten iOS-App unter **Lokaler Server → KI-Anbindung → MCP-Zugang und HG-Entwürfe** öffnen.
4. Zugang aktivieren und Berechtigungen speichern. Neben Lesen sind getrennt freigebbar: HG-Entwürfe, **HGs auf homee anlegen · deaktiviert**, **Geräte schalten erlauben**, **Homeegramme löschen erlauben**, **Homeegramme aktivieren/deaktivieren** und **Homeegramme bearbeiten**. Neue Rechte sind nach dem Update zunächst aus. Löschen ist eine destruktive Aktion am Original auf homee, nicht am SHB-Import.
5. Bei der Ersteinrichtung einen MCP-Schlüssel erzeugen und sofort kopieren. Er wird nur einmal geliefert; gespeichert wird ausschließlich sein SHA-256-Hash. Vorhandene Schlüssel bleiben beim Serverupdate gültig. Ersetzen widerruft den bisherigen Schlüssel.
6. Im externen MCP-Client **Streamable HTTP** verwenden:
   - URL: `http://SERVER:8787/mcp/` (bzw. die in SHB angezeigte Adresse)
   - HTTP-Header: `Authorization: Bearer <MCP-Schlüssel>`

Der Client muss eigene Bearer-Header unterstützen. Eine OAuth-Anmeldung bzw. automatische OAuth-Registrierung ist **nicht implementiert**. Nicht jede KI-Anwendung unterstützt diese Verbindungsart. Das MCP-SDK 1.30 übernimmt Protokollverhandlung und Streamable-HTTP-Lifecycle; es handelt sich nicht um einen beliebigen JSON-Endpunkt.

Für Zugriff außerhalb des vertrauenswürdigen LAN: TLS/HTTPS über einen abgesicherten Zugang. Keine ungeschützte Portfreigabe. Cloud-Clients können eine private LAN-Adresse nicht direkt erreichen. Bei einer Cloud-KI verlassen angefragte Hausdaten das lokale Netz. Browser-Origins werden ausdrücklich abgewiesen; die erste Ausbaustufe richtet sich an native/serverseitige MCP-Clients. Die iOS-App ist Verwaltungs- und Prüfoberfläche, kein dauerhaft laufender MCP-Server.

## Werkzeuge

- `list_homee_sources`: homee-Quellen auswählen, ohne Zugangsdaten.
- `list_devices`, `get_device_state`: numerische Cachewerte der Servergeräte, Einheiten und verfügbare Gerätezeitstempel. Lesezeit ist kein Nachweis frischer Messwerte.
- `get_homee_catalog`: Original-homee-IDs, Attribute und Wertebereiche. Diese IDs für HGs nutzen, nicht fremde Modul-/SHB-IDs.
- `list_homeegrams`, `get_homeegram`: HG-Cache lesen. Geräte-, Zeit-, Play-, HG-Auslöser/-Aktionen und aufgelöste Gruppenelemente werden ausgegeben. Andere Kategorien werden als ausgelassen markiert; insbesondere keine Webhook-URLs, Push-Inhalte oder Zugangsdaten ausgeben.
- `create_homeegram_draft`: strukturierten Entwurf mit neuer UUID als `request_id` ablegen. Bei Wiederholung dieselbe UUID verwenden; geänderter Inhalt mit derselben UUID wird abgelehnt.
- `create_homeegram`: ein echtes neues HG auf dem ausgewählten homee anlegen, immer deaktiviert. Benötigt `allow_homee_creation`. Verwendet die bestehende homee-Serversitzung. Erfolg liefert die von homee bestätigte HG-ID und `active: 0`; es wird weder aktiviert noch abgespielt.
- `control_device`: mit `operation: on`, `off` oder `toggle` schalten. Benötigt `allow_device_control`. Verwendet SHB-IDs aus `list_devices`, akzeptiert nur sichtbare, schreibbare An/Aus-Attribute aktiver Serverintegrationen. Jeder Befehl benötigt eine neue UUID; bei Retry dieselbe verwenden. Das normale Schaltprotokoll zeigt die Quelle **KI / MCP**.
- `delete_homeegram`: ein eindeutig benanntes Original-Homeegramm auf der ausgewählten homee-Integration löschen. Benötigt `allow_homee_deletion`, die positive Original-HG-ID, den exakt URL-dekodierten Namen als `confirmation_name` und eine neue UUID als `request_id`. Nur auf ausdrücklichen Nutzerauftrag, keine stillschweigende Bereinigung. Der Server lädt vor dem Löschen eine frische Liste, prüft Name und ID, sichert das vollständige HG lokal und bestätigt anschließend seine Abwesenheit auf homee. Verweise aus anderen HGs können ungültig werden.
- `delete_homeegrams`: 1 bis 50 ausdrücklich aufgeführte Original-HGs derselben Quelle mit ID und Namensbestätigung löschen; ebenfalls `allow_homee_deletion`. Gesamte Zielliste vorprüfen, nacheinander sichern/löschen/bestätigen, beim ersten Fehler oder unklaren Ergebnis stoppen. Ergebnis je Ziel statt einer pauschalen Erfolgsmeldung; dieselbe Batch-UUID nur mit derselben geordneten Liste wiederholen. Keine Wildcards.

Die HG-Anlage unterstützt manuelles Play, Geräteauslöser, Gerätebedingungen, Setzen/Umschalten mit Verzögerungen sowie die oben beschriebenen HG-Auslöser/-Aktionen. Zeitpläne, Push, Gruppen und andere Spezialelemente werden bei der Serverübertragung derzeit abgelehnt; diese danach im regulären HG-Editor auf homee ergänzen. Das Tool darf solche Anforderungen nicht als bereits umgesetzt ausgeben. Die Geräte-Schaltfunktion unterstützt hier An/Aus-Schalter; Dimmer-/Positionswerte sind noch kein eigenes MCP-Werkzeug.

Pfadzuordnung bleibt unverändert:

| Element | branch |
|---|---|
| Voraussetzung für alle Aktionen | Bedingung 0 |
| Wenn-Bedingung für Dann/Sonst | Bedingung 1 |
| Immer-Aktion nach erfüllten Voraussetzungen | Aktion 0 |
| Dann-Aktion | Aktion 1 |
| Sonst-Aktion | Aktion 2 |

## Prüfung und Sicherheit

Separate Entwürfe erscheinen nach „Status und Entwürfe laden“ in SHB. **Im Ablauf bearbeiten** öffnet den vorhandenen grafischen Editor mit den Geräten genau der gewählten homee-Quelle. **Auf homee speichern · deaktiviert** überträgt den vorhandenen Entwurf direkt. Speichern im Editor überträgt die aktuell bearbeitete Fassung; Geräte-/Attribut-IDs, Schreibbarkeit, Wertebereiche und Pfade werden auf dem Server erneut geprüft.

Eine direkte Verbindung vom iPhone zum homee ist dafür nicht mehr nötig. Der Server verwendet seine bestehende Sitzung der im Entwurf gespeicherten Integration. Danach bleibt das HG deaktiviert auf homee liegen und kann dort regulär bearbeitet und bewusst aktiviert werden. Der Server speichert die HG-ID am Entwurf; erneutes Übertragen derselben Anfrage erzeugt keine weitere Kopie. Bei unklarer/teilweiser Anlage wird keine zweite Kopie und kein Element blind erneut angelegt; das gegebenenfalls teilweise vorhandene HG bleibt deaktiviert und muss auf homee geprüft werden.

Der MCP-Schlüssel hat keine administrativen Rechte. HG-Abspielen und freie WebSocket-Befehle sind nicht als MCP-Werkzeuge vorhanden. Aktivieren/Deaktivieren, Bearbeiten und Löschen erfordern jeweils eine eigene Freigabe; keines dieser Rechte schaltet Geräte-Schaltrecht oder HG-Anlagerecht automatisch ein. Administrativer API-Zugang und MCP-Schlüssel dürfen nicht anstelle voneinander verwendet werden. Die manuelle Übertragung eines Entwurfs in der iOS-App benutzt den administrativen App-Zugang und benötigt keine KI-Schaltfreigabe. Deaktivierung bzw. Rotation greift bei der nächsten Anfrage; eine bereits laufende Anfrage kann noch abgeschlossen werden.

Vor einer MCP-Löschung liegt die vollständige Originalfassung im lokalen Modulzustand der Serverdatenbank. Die Sicherung wird nicht durch das MCP-Werkzeug ausgegeben, weil sie private URLs, Nachrichten oder andere Inhalte enthalten kann. Nur der administrative API-Zugang kann sie abrufen. Das ist eine Rohdatensicherung, keine automatische Wiederherstellung und kein Ersatz für die vorhandene HG-Backup-/Vorlagenfunktion. Bei unklarer Löschantwort dieselbe Anfrage-ID verwenden: Der Server prüft dann nur noch den Bestand und sendet keinen zweiten Löschbefehl. Ein Erfolg wird erst nach einer frischen Liste ohne die HG-ID gemeldet; eine später wiederverwendete ID wird durch eine bestätigte Wiederholung nicht gelöscht. Bis zu 1.000 Löschanfragen werden ohne automatische Verdrängung alter IDs behalten.

Ab 0.20.21 wird auch eine unmittelbar auf `DELETE:homeegrams/<ID>` gelieferte vollständige HG-Liste als Bestätigung erkannt. Ein weiteres `GET:homeegrams` ist nur nötig, solange noch keine Bestätigung vorliegt. Das verhindert die frühere falsche Timeout-Meldung nach bereits erfolgreicher Löschung.

Die App ab Build 316 nutzt bei serverseitiger homee-Integration den administrativen HG-Endpunkt statt ihrer abgeschalteten Direktverbindung. Sie lädt die Liste aus genau dieser Quelle, bestätigt ID und Namen und entfernt den Eintrag erst nach bestätigter Löschung. Bei unklarer HTTP-Antwort bleibt die Anfrage-ID auch nach App-Neustart erhalten. Für diese ausdrücklich in der App bestätigte Aktion sind keine MCP-Freigaben nötig; der administrative API-Schlüssel ist erforderlich. Mehrere nicht eindeutig zuordenbare homee-Quellen werden nicht automatisch ausgewählt.

Ein Gerätebefehl mit `command_sent: true` wurde an den Adapter übermittelt; dies bestätigt noch nicht den Hardwarezustand (`confirmed: false`). Umschalten basiert auf dem zuletzt bekannten Cachewert. Danach den Zustand mit `get_device_state` prüfen. Anfrage-UUIDs werden vor dem Versand dauerhaft reserviert; auch nach Timeout oder Neustart wird derselbe Befehl nicht erneut gesendet. Unklare Ergebnisse enthalten `command_sent: null`, `retry_allowed: false`. Vor einem neuen Befehl den Gerätezustand prüfen. Das Journal behält bis zu 100.000 Geräteanfragen; HG-Anlage hält bis zu 1.000 Anfragen, ohne alte IDs automatisch zu verwerfen.

Verwaltungs-API (administrativer Server-Schlüssel):

- `GET /api/v1/integrations/{integration_id}/homeegrams`: frische Original-HG-Liste der ausgewählten homee-Quelle
- `POST /api/v1/integrations/{integration_id}/homeegrams/{homeegram_id}/delete`: bestätigte Einzel-Löschung mit `confirmation_name` und `request_id` (UUID), inklusive lokaler Sicherung
- `GET/PUT /api/v1/mcp`: Status/Berechtigungen
- `POST /api/v1/mcp/rotate-token`: neuen Schlüssel einmalig ausgeben
- `GET /api/v1/mcp/drafts`, `DELETE /api/v1/mcp/drafts/{id}`: Entwürfe
- `GET /api/v1/mcp/drafts/{id}/catalog`: Geräte der gespeicherten homee-Quelle für den Editor
- `POST /api/v1/mcp/drafts/{id}/deploy`: Entwurf oder aktuelle bearbeitete Fassung über den Server deaktiviert auf homee anlegen
- `GET /api/v1/mcp/audit`: letzte 100 Zugriffsereignisse ohne Schlüssel oder Nutzdaten
- `GET /api/v1/mcp/homeegram-deletions?integration_id=...`: Metadaten der lokalen Löschsicherungen
- `GET /api/v1/mcp/homeegram-deletions/{request_id}/backup?integration_id=...`: vollständige Original-JSON-Sicherung, ausschließlich für Administratoren

Nur Daten, die der Server tatsächlich besitzt, sind sichtbar. HomeKit-/Matter-Daten aus der iOS-App werden nicht automatisch übertragen. Messhistorien sind noch kein MCP-Werkzeug. Maximal 100 offene Entwürfe und 64 KiB pro MCP-Anfrage; Listen sind begrenzt. Kein externer KI-Dienst wird automatisch eingerichtet oder kontaktiert.

## Tests

`python -m unittest discover -s tests -p 'test_mcp*.py'`

`python -m unittest discover -s tests -p test_homee.py`

`python -m unittest discover -s tests -p 'test_homeegram*.py'`

`python -m unittest discover -s tests -p test_startup.py`

Deckt SDK-HTTP-Handshake, Tools, getrennte Freigaben, Schaltprotokoll, Authentifizierung, Rotation, Origin-Sperre, Pfade, Prüfung der tatsächlich bearbeiteten Fassung, deaktivierte native HGs, bestätigte HG-Löschung mit Sicherung sowie Wiederholungsschutz nach Fehlern/Neustart ab. Mehrfach-Löschtests prüfen zusätzlich Vorprüfung, Einzelergebnisse und Abbruch ohne weitere Löschbefehle. Tests verwenden ausschließlich temporäre Daten und Geräteattrappen; sie ersetzen keinen Test auf einem produktiven homee.
