from __future__ import annotations

import datetime as dt
import json
from html import escape
from urllib.parse import unquote


def display_text(value):
    """Decode URL-encoded homee text for presentation without changing stored values."""
    text = str(value or "")
    for _ in range(3):
        if "%" not in text:
            break
        try:
            decoded = unquote(text, encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            break
        if decoded == text:
            break
        text = decoded
    return text


def display_html(value):
    return escape(display_text(value))


def shell(title, subtitle, body, version, active="dashboard"):
    items = [
        ("dashboard", "/setup", "Übersicht"),
        ("integrations", "/setup/integrations", "Integrationen"),
        ("displays", "/setup/displays", "E-Paper"),
        ("automations", "/setup/automations", "Automationen"),
        ("modbus", "/setup/modbus", "Modbus-Templates"),
        ("enocean", "/setup/enocean", "EnOcean"),
    ]
    nav = "".join(f'<a class="{"active" if key == active else ""}" href="{url}">{label}</a>' for key, url, label in items)
    return f'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · SmartHomeBoard</title><style>
:root{{--bg:#eef2f7;--panel:#fff;--text:#142033;--muted:#657187;--line:#d9e1eb;--blue:#1677ff;--blue2:#075fc8;--ok:#167747;--bad:#b42318}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0c1421;--panel:#151f2e;--text:#edf4ff;--muted:#9baac0;--line:#2b3a50;--blue:#4d9aff;--blue2:#2075d8;--ok:#54c58a;--bad:#ff8a80}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}a{{color:inherit;text-decoration:none}}
.app{{min-height:100vh;display:grid;grid-template-columns:225px minmax(0,1fr)}}aside{{background:#111d30;color:#eaf2ff;padding:24px 16px;position:sticky;top:0;height:100vh}}.brand{{display:flex;align-items:center;gap:10px;margin:0 8px 26px;font-weight:800}}.logo{{width:38px;height:38px;border-radius:12px;background:linear-gradient(145deg,#41c4ff,#2166e5);display:grid;place-items:center;font-size:19px}}nav{{display:grid;gap:5px}}nav a{{padding:11px 13px;border-radius:10px;color:#b9c7dc}}nav a:hover,nav a.active{{background:#ffffff16;color:#fff}}.version{{position:absolute;bottom:22px;left:28px;color:#8291a8;font-size:12px}}
main{{width:100%;max-width:1180px;padding:30px 34px 70px}}header{{margin-bottom:24px}}h1{{font-size:28px;margin:0 0 5px}}h2{{font-size:18px;margin:0 0 14px}}h3{{font-size:16px;margin:0}}p{{line-height:1.48}}.muted,small{{color:var(--muted)}}
.stats,.cards,.split{{display:grid;gap:14px}}.stats{{grid-template-columns:repeat(4,1fr)}}.cards{{grid-template-columns:repeat(2,1fr);margin-top:16px}}.split{{grid-template-columns:minmax(260px,.8fr) minmax(380px,1.5fr)}}.panel,.stat,.link-card{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;box-shadow:0 7px 24px #00000008}}.stat b{{font-size:28px;display:block}}.link-card{{display:flex;justify-content:space-between;align-items:center;min-height:102px}}.link-card span{{color:var(--blue);font-size:22px}}.panel{{margin-bottom:15px}}.list{{display:grid;gap:9px}}.row{{border:1px solid var(--line);border-radius:12px;padding:12px;display:flex;justify-content:space-between;gap:12px;align-items:center}}.row.selected{{border-color:var(--blue);box-shadow:0 0 0 2px #1677ff20}}.row small{{display:block;margin-top:3px}}.badge{{padding:4px 8px;border-radius:999px;background:#64748b20;font-size:12px;white-space:nowrap}}.badge.ok{{color:var(--ok);background:#16a34a1c}}.badge.bad{{color:var(--bad);background:#ef44441c}}.protocol-log{{display:grid;gap:8px;max-height:620px;overflow:auto;margin-top:12px}}.protocol-message{{border:1px solid var(--line);border-radius:11px;padding:10px;background:var(--bg)}}.protocol-head{{display:flex;gap:8px;align-items:center;justify-content:space-between;margin-bottom:7px}}.protocol-head span{{display:flex;gap:7px;align-items:center}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;line-height:1.45}}.protocol-filter{{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:end}}
form{{display:grid;gap:13px}}label{{display:grid;gap:6px;font-weight:650}}input,select,textarea{{width:100%;border:1px solid var(--line);border-radius:10px;padding:11px 12px;font:inherit;color:var(--text);background:var(--bg)}}textarea{{min-height:95px;resize:vertical}}.check{{display:flex;align-items:center;gap:9px}}.check input{{width:auto}}button,.button{{border:0;border-radius:10px;padding:11px 15px;background:var(--blue);color:#fff;font-weight:750;font-size:14px;cursor:pointer;display:inline-block;text-align:center}}button:hover,.button:hover{{background:var(--blue2)}}button.secondary,.button.secondary{{background:#64748b}}button.danger{{background:#c2362b}}.actions{{display:flex;flex-wrap:wrap;gap:9px}}.actions form{{display:block}}.notice{{border-radius:11px;padding:12px 14px;margin-bottom:14px;background:#16a34a1c;color:var(--ok)}}.notice.error{{background:#ef44441c;color:var(--bad)}}details summary{{cursor:pointer;font-weight:750}}code{{font-family:ui-monospace,SFMono-Regular,monospace;overflow-wrap:anywhere}}.widget{{display:grid;grid-template-columns:34px 1.3fr 1fr 80px 42px;gap:8px;align-items:end;padding:10px;border:1px solid var(--line);border-radius:11px}}.grab{{cursor:grab;font-size:20px;text-align:center;padding-bottom:10px;color:var(--muted)}}.widget button{{padding:10px;background:#c2362b}}.event{{display:grid;grid-template-columns:125px 1fr;gap:12px;border-left:4px solid #64748b;background:var(--bg);padding:10px;border-radius:8px}}.event.success{{border-color:#16a34a}}.event.warning{{border-color:#f59e0b}}.event.error{{border-color:#ef4444}}
@media(max-width:850px){{.app{{grid-template-columns:1fr}}aside{{height:auto;position:static;padding:13px}}.brand{{margin:0 4px 10px}}nav{{display:flex;overflow:auto}}nav a{{white-space:nowrap}}.version{{display:none}}main{{padding:22px 15px 60px}}.split,.cards{{grid-template-columns:1fr}}}}@media(max-width:620px){{.stats{{grid-template-columns:repeat(2,1fr)}}.widget{{grid-template-columns:28px 1fr 42px}}.widget label:nth-of-type(2),.widget label:nth-of-type(3){{grid-column:2/4}}}}
</style></head><body><div class="app"><aside><div class="brand"><div class="logo">⌂</div><div>SmartHomeBoard</div></div><nav>{nav}</nav><div class="version">Server {escape(version)}</div></aside><main><header><h1>{escape(title)}</h1><div class="muted">{escape(subtitle)}</div></header>{body}</main></div></body></html>'''


def notice(message="", error=""):
    return (f'<div class="notice"><b>Gespeichert</b><br>{escape(message)}</div>' if message else "") + (f'<div class="notice error"><b>Fehler</b><br>{escape(error)}</div>' if error else "")


def token_field(required, authenticated):
    if not required:
        return '<input type="hidden" name="current_token" value="">'
    if authenticated:
        return '<input type="hidden" name="current_token" value="">'
    return '<label>API-Schlüssel<input name="current_token" type="password" autocomplete="current-password" required></label>'


def dashboard(version, module_count, integrations, node_count, displays, automation_count, port, setup_port, configured, authenticated, env_token, suggested, message="", error="", revealed_token="", restart_port=None):
    cards = [
        ("Integrationen", "homee, Modbus, go-e und weitere Verbindungen verwalten", "/setup/integrations"),
        ("E-Paper", "Displays koppeln, Aufbau und Wertebelegung konfigurieren", "/setup/displays"),
        ("Automationen", "Serverregeln prüfen, testen und Protokoll ansehen", "/setup/automations"),
        ("Modbus-Templates", "Geräteprofile ansehen und eigene Vorlagen anlegen", "/setup/modbus"),
    ]
    link_cards = "".join(f'<a class="link-card" href="{url}"><div><h3>{title}</h3><p class="muted">{text}</p></div><span>›</span></a>' for title, text, url in cards)
    current = '<label>Bisheriger API-Schlüssel<input name="current_token" type="password" required autocomplete="current-password"></label>' if configured and not authenticated else ""
    required = "" if configured else "required"
    env_note = '<div class="notice error">Der Schlüssel wird durch <code>SHB_API_TOKEN</code> vorgegeben und kann hier nicht geändert werden.</div>' if env_token else ""
    token_result = f'<div class="notice"><b>Diesen Schlüssel jetzt kopieren:</b><br><code>{escape(revealed_token)}</code></div>' if revealed_token else ""
    restart = f'<div class="notice">Der Container startet neu und ist anschließend auf Port {restart_port} erreichbar. Die Einrichtungsseite bleibt unter Port {setup_port} erreichbar.</div>' if restart_port else ""
    body = f'''{notice(message,error)}<section class="stats"><div class="stat"><b>{module_count}</b><span class="muted">Module</span></div><div class="stat"><b>{len(integrations)}</b><span class="muted">Integrationen</span></div><div class="stat"><b>{node_count}</b><span class="muted">Geräte</span></div><div class="stat"><b>{len(displays)}</b><span class="muted">E-Paper</span></div></section><section class="cards">{link_cards}</section>
<section class="panel" style="margin-top:16px"><details><summary>Server- und Sicherheitseinstellungen</summary><p class="muted">Der Einrichtungsport bleibt {setup_port}; App und API verwenden den Kommunikationsport.</p>{env_note}{restart}{token_result}<form method="post" action="/setup">{current}<label>Kommunikationsport<input name="server_port" type="number" min="1024" max="65535" value="{port}" required></label><label>Neuer API-Schlüssel<input name="new_token" type="password" minlength="16" {required} value="{escape(suggested)}"><small>Leer lassen, um den vorhandenen Schlüssel beizubehalten.</small></label><label>Neuen Schlüssel wiederholen<input name="confirm_token" type="password" minlength="16" {required} value="{escape(suggested)}"></label><button>Servereinstellungen speichern</button></form></details></section>'''
    return shell("Übersicht", f"{automation_count} Automationen laufen persistent auf dem Server", body, version, "dashboard")


def integrations_page(version, manifests, integrations, selected_module="", selected_id="", authenticated=False, token_required=False, message="", error="", homee_protocol=None, protocol_filter=""):
    manifest_map = {item["id"]: item for item in manifests}
    selected = next((item for item in integrations if item["id"] == selected_id), None)
    module_id = selected["module_id"] if selected else (selected_module if selected_module in manifest_map else (manifests[0]["id"] if manifests else ""))
    manifest = manifest_map.get(module_id)
    integration_rows = "".join(
        f'<a class="row {"selected" if selected and item["id"] == selected["id"] else ""}" href="/setup/integrations?edit={escape(item["id"])}"><span><b>{display_html(item["name"])}</b><small>{display_html(manifest_map.get(item["module_id"],{}).get("name",item["module_id"]))} · {item.get("device_count",0)} Geräte</small></span><span class="badge {"ok" if item.get("status") in ("Verbunden","Verbindung erfolgreich") else "bad" if item.get("error") else ""}">{display_html(item.get("status") or "Bereit")}</span></a>'
        for item in integrations
    ) or '<p class="muted">Noch keine Integration angelegt.</p>'
    module_rows = "".join(f'<a class="row" href="/setup/integrations?module={escape(item["id"])}"><span><b>{display_html(item["name"])}</b><small>{display_html(item.get("description", ""))}</small></span><span>＋</span></a>' for item in manifests)
    editor = '<p class="muted">Es sind keine Servermodule installiert.</p>'
    if manifest:
        config = dict(selected.get("configuration", {})) if selected else {}
        fields = "".join(_field_html(field, config.get(field["key"], field.get("default", "")), editing=bool(selected)) for field in manifest.get("fields", []))
        editor = f'''<h2>{"Bearbeiten" if selected else "Neu anlegen"} · {escape(manifest["name"])}</h2><p class="muted">{escape(manifest.get("description", ""))}</p><form method="post" action="/setup/integrations/save">{token_field(token_required,authenticated)}<input type="hidden" name="integration_id" value="{escape(selected["id"] if selected else "")}"><input type="hidden" name="module_id" value="{escape(module_id)}"><label>Name<input name="name" value="{escape(selected["name"] if selected else manifest["name"])}" required maxlength="80"></label><label class="check"><input name="enabled" type="checkbox" value="1" {"checked" if not selected or selected.get("enabled") else ""}> Aktiv</label>{fields}<div class="actions"><button>Persistent speichern</button></div></form>'''
        if selected:
            editor += f'''<div class="actions" style="margin-top:10px"><form method="post" action="/setup/integrations/test">{token_field(token_required,authenticated)}<input type="hidden" name="integration_id" value="{escape(selected["id"])}"><button class="secondary">Verbindung testen</button></form><form method="post" action="/setup/integrations/delete" onsubmit="return confirm('Integration wirklich löschen?')">{token_field(token_required,authenticated)}<input type="hidden" name="integration_id" value="{escape(selected["id"])}"><button class="danger">Löschen</button></form></div>'''
            module_actions = manifest.get("actions", [])
            if module_actions:
                action_forms = "".join(_module_action_html(selected["id"], action, token_required, authenticated) for action in module_actions)
                editor += f'''<section style="margin-top:22px;border-top:1px solid var(--line);padding-top:18px"><h2>Modulaktionen</h2><div class="actions">{action_forms}</div></section>'''
            if module_id == "homee":
                editor += _homee_console_html(selected["id"], homee_protocol or {}, protocol_filter, token_required, authenticated)
    body = f'''{notice(message,error)}<div class="split"><div><section class="panel"><h2>Vorhandene Verbindungen</h2><div class="list">{integration_rows}</div></section><section class="panel"><h2>Neue Verbindung</h2><div class="list">{module_rows}</div></section></div><section class="panel">{editor}</section></div>'''
    return shell("Integrationen", "Dieselben persistenten Servereinstellungen werden in Webportal und iOS-App angezeigt.", body, version, "integrations")


def _module_action_html(integration_id, action, token_required, authenticated):
    action_id = escape(str(action.get("id", "")))
    title = escape(str(action.get("title", action_id)))
    destructive = action.get("role") == "destructive"
    css_class = "danger" if destructive else "secondary"
    confirmation = f''' onsubmit="return confirm('{title} wirklich ausführen?')"''' if destructive else ""
    fields = "".join(_action_field_html(field) for field in action.get("fields", []))
    return f'''<form method="post" action="/setup/integrations/action"{confirmation}>{token_field(token_required,authenticated)}<input type="hidden" name="integration_id" value="{escape(integration_id)}"><input type="hidden" name="action_id" value="{action_id}">{fields}<button class="{css_class}">{title}</button></form>'''


def _action_field_html(field):
    key = str(field.get("key", "")).strip()
    title = str(field.get("title", key))
    kind = str(field.get("type", "text"))
    input_type = "password" if kind == "password" else "number" if kind == "number" else "text"
    required = "required" if field.get("required") else ""
    pattern = f' pattern="{escape(str(field["pattern"]))}"' if field.get("pattern") else ""
    maxlength = f' maxlength="{int(field["maxlength"])}"' if field.get("maxlength") else ""
    placeholder = escape(str(field.get("placeholder", "")))
    help_html = f'<small>{display_html(field.get("help", ""))}</small>' if field.get("help") else ""
    return f'<label>{display_html(title)}<input type="{input_type}" name="payload__{escape(key)}" {required}{pattern}{maxlength} placeholder="{placeholder}" autocomplete="off">{help_html}</label>'


_HOMEE_COMMAND_TEMPLATES = (
    ("Geräte und gesamte Konfiguration", "GET:all"),
    ("Alle Geräte", "GET:nodes"),
    ("Ein Gerät", "GET:nodes/{node_id}"),
    ("Ein Attribut", "GET:nodes/{node_id}/attributes/{attribute_id}"),
    ("Alle Homeegramme", "GET:homeegrams"),
    ("Alle Gruppen", "GET:groups"),
    ("Alle Beziehungen", "GET:relationships"),
    ("Alle Pläne", "GET:plans"),
    ("Alle Szenarien", "GET:scenarios"),
    ("Alle Benutzer", "GET:users"),
    ("Einstellungen", "GET:settings"),
    ("Cubes", "GET:cubes"),
    ("Attributwert setzen", "PUT:nodes/{node_id}/attributes/{attribute_id}?target_value={value}"),
    ("Homeegramm ausführen", "PUT:homeegrams/{homeegram_id}?play=1"),
    ("Diagnose: Analytics (app.min.js)", "GET:analytics"),
    ("Sicherung abfragen (app.min.js)", "GET:backup"),
    ("Sicherungsaktion starten – Vorsicht (app.min.js)", "POST:backup"),
    ("Sicherungsaktion abbrechen (app.min.js)", "POST:backup?cancel=1"),
    ("Loglevel setzen (app.min.js)", "PUT:log?component={component}&level={level}"),
)


def _homee_console_html(integration_id, protocol, selected_filter, token_required, authenticated):
    categories = ["", "node", "user", "homeegram", "attribute", "settings", "all", "warning", "code", "error", "other", "command"]
    labels = {"": "Alle Nachrichten", "other": "Sonstige", "command": "Gesendet"}
    if selected_filter not in categories:
        selected_filter = ""
    template_options = '<option value="">Vorlage auswählen …</option>' + "".join(
        f'<option value="{escape(command)}">{escape(label)} · {escape(command)}</option>'
        for label, command in _HOMEE_COMMAND_TEMPLATES
    )
    filter_options = "".join(
        f'<option value="{escape(category)}" {"selected" if category == selected_filter else ""}>{escape(labels.get(category, category))}</option>'
        for category in categories
    )
    messages = list(protocol.get("messages", [])) if isinstance(protocol, dict) else []
    if selected_filter:
        messages = [item for item in messages if str(item.get("category", "other")).lower() == selected_filter]
    rows = []
    for item in reversed(messages):
        try:
            stamp = dt.datetime.fromtimestamp(float(item.get("timestamp", 0))).strftime("%H:%M:%S")
        except (TypeError, ValueError, OSError):
            stamp = "--:--:--"
        direction = "→ homee" if item.get("direction") == "out" else "← homee"
        truncated = f' · gekürzt, {int(item.get("size", 0))} Zeichen' if item.get("truncated") else ""
        rows.append(f'''<div class="protocol-message"><div class="protocol-head"><span><b>{escape(str(item.get("category", "other")))}</b><small>{escape(direction)}</small></span><small>{stamp}{escape(truncated)}</small></div><pre>{escape(_pretty_protocol_message(item.get("message", "")))}</pre></div>''')
    log = "".join(rows) or '<p class="muted">Für diesen Filter liegen noch keine WebSocket-Nachrichten vor.</p>'
    count_text = f'{len(messages)} Eintrag' if len(messages) == 1 else f'{len(messages)} Einträge'
    return f'''<section id="homeeProtocolConsole" data-integration-id="{escape(integration_id)}" style="margin-top:22px;border-top:1px solid var(--line);padding-top:20px"><h2>homee WebSocket-Konsole</h2><p class="muted">Bekannte Strukturen auswählen oder genau eine Nachricht von Hand eingeben. Platzhalter in geschweiften Klammern vor dem Senden ersetzen.</p><form method="post" action="/setup/integrations/homee/send" onsubmit="return confirm('Diese WebSocket-Nachricht wirklich an homee senden?')">{token_field(token_required,authenticated)}<input type="hidden" name="integration_id" value="{escape(integration_id)}"><label>Befehlsvorlage<select id="homeeCommandTemplate" onchange="if(this.value)document.getElementById('homeeCommand').value=this.value">{template_options}</select></label><label>WebSocket-Nachricht<textarea id="homeeCommand" name="command" maxlength="2048" required placeholder="GET:nodes">GET:nodes</textarea></label><button>Nachricht senden</button></form><div class="protocol-head" style="margin-top:24px"><h2 style="margin:0">WebSocket-Livefeed</h2><span><span class="badge" id="homeeProtocolLiveState">○ Pausiert</span></span></div><label>Nachrichtentyp<select id="homeeProtocolFilter">{filter_options}</select></label><div class="actions"><button id="homeeProtocolLiveToggle" type="button">Livefeed starten</button><button id="homeeProtocolRefresh" type="button" class="secondary">Einmal aktualisieren</button></div><p class="muted">Der Livefeed stoppt automatisch nach 60 Sekunden und zeigt höchstens 100 Einträge.</p><p id="homeeProtocolCount" class="muted">{count_text} im gewählten Filter</p><div id="homeeProtocolLog" class="protocol-log">{log}</div>{_homee_protocol_script()}</section>'''


def _pretty_protocol_message(message):
    text = str(message)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return text
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return text
    prefix = text[:start].strip()
    formatted = json.dumps(parsed, ensure_ascii=False, indent=2)
    return f"{prefix}\n{formatted}" if prefix else formatted


def _homee_protocol_script():
    return '''<script>(function(){
const root=document.getElementById('homeeProtocolConsole');if(!root)return;
const filter=document.getElementById('homeeProtocolFilter');
const log=document.getElementById('homeeProtocolLog');
const count=document.getElementById('homeeProtocolCount');
const state=document.getElementById('homeeProtocolLiveState');
const toggle=document.getElementById('homeeProtocolLiveToggle');
let loading=false,timer=null,stopTimer=null,live=false;
function pretty(value){const text=String(value??'');const start=text.indexOf('{'),end=text.lastIndexOf('}');if(start<0||end<start)return text;try{const prefix=text.slice(0,start).trim();const json=JSON.stringify(JSON.parse(text.slice(start,end+1)),null,2);return prefix?prefix+'\\n'+json:json}catch(_){return text}}
function render(messages){const previousTop=log.scrollTop;const wasAtTop=previousTop<8;log.replaceChildren();if(!messages.length){const empty=document.createElement('p');empty.className='muted';empty.textContent='Für diesen Filter liegen noch keine WebSocket-Nachrichten vor.';log.append(empty)}else{[...messages].reverse().forEach(item=>{const row=document.createElement('div');row.className='protocol-message';const head=document.createElement('div');head.className='protocol-head';const left=document.createElement('span');const category=document.createElement('b');category.textContent=item.category||'other';const direction=document.createElement('small');direction.textContent=item.direction==='out'?'→ homee':'← homee';left.append(category,direction);const meta=document.createElement('small');const date=new Date(Number(item.timestamp||0)*1000);meta.textContent=(Number.isFinite(date.getTime())?date.toLocaleTimeString('de-DE'): '--:--:--')+(item.truncated?' · gekürzt, '+Number(item.size||0)+' Zeichen':'');head.append(left,meta);const pre=document.createElement('pre');pre.textContent=pretty(item.message);row.append(head,pre);log.append(row)})}count.textContent=messages.length+' '+(messages.length===1?'Eintrag':'Einträge')+' im gewählten Filter';if(!wasAtTop)log.scrollTop=previousTop}
async function update(){if(loading||document.hidden)return;loading=true;try{const query=new URLSearchParams({integration_id:root.dataset.integrationId,category:filter.value,limit:'100'});const response=await fetch('/setup/integrations/homee/protocol?'+query,{cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);const data=await response.json();render(Array.isArray(data.messages)?data.messages:[]);if(live){state.textContent='● Live · maximal 60 s';state.className='badge ok'}else{setPaused()}}catch(error){state.textContent='● Verbindung unterbrochen';state.className='badge bad'}finally{loading=false}}
function setPaused(label='○ Pausiert'){state.textContent=label;state.className='badge';toggle.textContent='Livefeed starten'}
function stop(label='○ Pausiert'){live=false;if(timer)clearInterval(timer);if(stopTimer)clearTimeout(stopTimer);timer=null;stopTimer=null;setPaused(label)}
function start(){if(live)return;live=true;state.textContent='● Live · maximal 60 s';state.className='badge ok';toggle.textContent='Livefeed stoppen';update();timer=setInterval(update,1500);stopTimer=setTimeout(()=>stop('○ Nach 60 s pausiert'),60000)}
filter.addEventListener('change',update);document.getElementById('homeeProtocolRefresh').addEventListener('click',update);toggle.addEventListener('click',()=>live?stop():start());document.addEventListener('visibilitychange',()=>{if(document.hidden&&live)stop('○ Im Hintergrund pausiert')});
})();</script>'''


def _field_html(field, value, editing=False):
    key, kind = str(field["key"]), str(field.get("type", "text"))
    title, help_text = str(field.get("title", key)), str(field.get("help", ""))
    required = "required" if field.get("required") and not (kind == "password" and editing) else ""
    common = f'name="config__{escape(key)}" {required}'
    if kind == "select":
        options = "".join(f'<option value="{escape(str(item.get("value","")))}" {"selected" if str(item.get("value","")) == str(value) else ""}>{escape(str(item.get("label",item.get("value",""))))}</option>' for item in field.get("options", []))
        control = f'<select {common}>{options}</select>'
    elif kind == "multiline":
        control = f'<textarea {common} placeholder="{escape(str(field.get("placeholder","")))}">{escape(str(value))}</textarea>'
    else:
        input_type = "password" if kind == "password" else "number" if kind in ("port","integer","duration","number") else "text"
        step = ' step="any"' if kind == "number" else ""
        minimum = f' min="{escape(str(field["minimum"]))}"' if "minimum" in field else ""
        maximum = f' max="{escape(str(field["maximum"]))}"' if "maximum" in field else ""
        shown = "" if kind == "password" and editing else str(value)
        control = f'<input type="{input_type}" {common}{step}{minimum}{maximum} value="{escape(shown)}" placeholder="{escape(str(field.get("placeholder","Unverändert" if kind == "password" and editing else "")))}">'
    help_html = f'<small>{escape(help_text)}</small>' if help_text else ""
    return f'<label>{escape(title)}{control}{help_html}</label>'


def displays_page(version, displays, nodes, selected_id="", authenticated=False, token_required=False, message="", error=""):
    selected = next((item for item in displays if item["id"] == selected_id), next((item for item in displays if item["status"] == "paired"), None))
    rows = "".join(f'<a class="row {"selected" if selected and item["id"] == selected["id"] else ""}" href="/setup/displays?display={escape(item["id"])}"><span><b>{display_html(item["name"])}</b><small>{display_html(item["model"])} · {escape(item.get("ip_address") or "IP unbekannt")}</small></span><span class="badge {"ok" if item["status"] == "paired" else ""}">{"Gekoppelt" if item["status"] == "paired" else "Wartet"}</span></a>' for item in displays) or '<p class="muted">Noch kein E-Paper registriert.</p>'
    pending = "".join(f'''<form class="panel" method="post" action="/setup/displays/pair"><h3>{escape(item["name"])}</h3>{token_field(token_required,authenticated)}<input type="hidden" name="display_id" value="{escape(item["id"])}"><label>Name<input name="name" value="{escape(item["name"])}" required></label><label>Kopplungscode<input name="pairing_code" inputmode="numeric" pattern="[0-9]{{6}}" required></label><button>Koppeln</button></form>''' for item in displays if item["status"] == "pending")
    editor = '<p class="muted">Ein gekoppeltes Display auswählen, um seine Ansicht zu bearbeiten.</p>'
    if selected and selected["status"] == "paired":
        config = selected.get("configuration", {}) or {}
        widgets = list(config.get("widgets", []))[:8]
        widget_rows = "".join(_widget_row(widget, nodes) for widget in widgets)
        editor = f'''<h2>{escape(selected["name"])} konfigurieren</h2><p class="muted">Änderungen werden in derselben Serverkonfiguration gespeichert, die auch die iOS-App verwendet.</p><form method="post" action="/setup/displays/save">{token_field(token_required,authenticated)}<input type="hidden" name="display_id" value="{escape(selected["id"])}"><label>Name<input name="name" value="{escape(selected["name"])}" required></label><label>Titel<input name="title" value="{escape(str(config.get("title","SmartHomeBoard")))}" maxlength="60"></label><div class="split"><label>Aktualisierung in Minuten<input name="sleep_minutes" type="number" min="1" max="1440" value="{int(config.get("sleep_minutes",5) or 5)}" required></label><label>Layout<select name="layout"><option value="list" {"selected" if config.get("layout") != "grid" else ""}>Liste</option><option value="grid" {"selected" if config.get("layout") == "grid" else ""}>Kacheln</option></select></label></div><h3>Werte und Reihenfolge</h3><div id="widgets" class="list">{widget_rows}</div><button type="button" class="secondary" onclick="addWidget()">Wert hinzufügen</button><button>Konfiguration speichern</button></form><form method="post" action="/setup/displays/delete" style="margin-top:10px" onsubmit="return confirm('Display wirklich entfernen?')">{token_field(token_required,authenticated)}<input type="hidden" name="display_id" value="{escape(selected["id"])}"><button class="danger">Display entfernen</button></form>{_widget_script(_attribute_options(nodes))}'''
    body = f'''{notice(message,error)}<div class="split"><div><section class="panel"><h2>Displays</h2><div class="list">{rows}</div></section>{pending}</div><section class="panel">{editor}</section></div>'''
    return shell("E-Paper", "Kopplung, Displayaufbau, Wertbelegung und Aktualisierung zentral verwalten.", body, version, "displays")


def _attribute_options(nodes, selected=""):
    options = '<option value="">Wert auswählen …</option>'
    for node in nodes:
        for attribute in node.get("attributes", []):
            value = f'{node.get("id",0)}:{attribute.get("id",0)}'
            node_name = display_text(node.get("name", "Gerät"))
            attribute_name = display_text(attribute.get("name", "Wert"))
            unit = display_text(attribute.get("unit", "")).strip()
            label = f'{node_name} · {attribute_name}' + (f' ({unit})' if unit else '')
            device_search = f'{node_name} {node.get("id", 0)}'.casefold()
            options += f'<option value="{escape(value)}" data-device-search="{escape(device_search)}" {"selected" if value == selected else ""}>{escape(label)}</option>'
    return options


def _widget_row(widget, nodes):
    selected = f'{widget.get("node_id",0)}:{widget.get("attribute_id",0)}'
    return f'''<div class="widget" draggable="true"><div class="grab">↕</div><label>Wert<input class="widget-search" type="search" placeholder="Gerät suchen …" aria-label="Werteliste nach Gerät filtern" oninput="filterWidget(this)"><small class="widget-filter-status">Alle Geräte</small><select name="widget_source">{_attribute_options(nodes,selected)}</select></label><label>Beschriftung<input name="widget_label" value="{escape(str(widget.get("label","")))}"></label><label>Nachkommastellen<input name="widget_decimals" type="number" min="0" max="3" value="{int(widget.get("decimals",1) or 0)}"></label><button type="button" onclick="this.closest('.widget').remove()">×</button><input type="hidden" name="widget_id" value="{escape(str(widget.get("id","")))}"></div>'''


def _widget_script(options):
    safe_options = options.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    return f'''<script>const list=document.getElementById('widgets');let dragged=null;function filterWidget(input){{const query=input.value.trim().toLocaleLowerCase();const label=input.parentElement;const select=label.querySelector('select');if(!select._allWidgetOptions){{select._allWidgetOptions=[...select.options].map(option=>option.cloneNode(true));select._widgetSelectedValue=select.value;select.addEventListener('change',()=>{{select._widgetSelectedValue=select.value}})}}const matches=select._allWidgetOptions.filter(option=>!option.value||!query||(option.dataset.deviceSearch||'').includes(query));select.replaceChildren(...matches.map(option=>option.cloneNode(true)));if(matches.some(option=>option.value===select._widgetSelectedValue))select.value=select._widgetSelectedValue;else select.value='';const count=matches.filter(option=>option.value).length;label.querySelector('.widget-filter-status').textContent=query?`${{count}} Werte des Geräts`:'Alle Geräte';}}function addWidget(){{if(list.children.length>=8)return;list.insertAdjacentHTML('beforeend',`<div class="widget" draggable="true"><div class="grab">↕</div><label>Wert<input class="widget-search" type="search" placeholder="Gerät suchen …" aria-label="Werteliste nach Gerät filtern" oninput="filterWidget(this)"><small class="widget-filter-status">Alle Geräte</small><select name="widget_source">{safe_options}</select></label><label>Beschriftung<input name="widget_label"></label><label>Nachkommastellen<input name="widget_decimals" type="number" min="0" max="3" value="1"></label><button type="button" onclick="this.closest('.widget').remove()">×</button><input type="hidden" name="widget_id"></div>`);}}list.addEventListener('dragstart',e=>{{dragged=e.target.closest('.widget')}});list.addEventListener('dragover',e=>{{e.preventDefault();const row=e.target.closest('.widget');if(row&&dragged&&row!==dragged){{const box=row.getBoundingClientRect();list.insertBefore(dragged,e.clientY<box.top+box.height/2?row:row.nextSibling)}}}});</script>'''


# Verständliche Namen für homee-Attribute, die im Rohprotokoll häufig nur mit
# ihrer numerischen CAAttributeType-ID ankommen. Modulattribute mit einem
# eigenen Namen behalten immer ihre genauere Modulbezeichnung.
_AUTOMATION_ATTRIBUTE_NAMES = {
    0: "Allgemeiner Wert", 1: "Ein/Aus", 2: "Dimmstufe", 3: "Aktuelle Leistung",
    4: "Energieverbrauch gesamt", 5: "Temperatur", 6: "Solltemperatur",
    7: "Luftfeuchtigkeit", 8: "Batteriestand", 9: "Status-LED",
    10: "Fensterposition", 11: "Helligkeit", 12: "Wasseralarm", 13: "Sirene",
    14: "Öffnungszustand", 15: "Position", 16: "Rauchalarm",
    17: "Stromausfallalarm", 18: "Ventilstellung", 19: "Binärer Eingang",
    20: "CO₂-Gehalt", 21: "Luftdruck", 22: "Füllstand", 23: "Farbe",
    24: "Farbsättigung", 25: "Bewegung", 26: "Bewegungsempfindlichkeit",
    28: "Nachlaufzeit Bewegung", 29: "Aufwachintervall", 30: "Sabotagealarm",
    31: "Warmwassertemperatur", 32: "Warmwasser-Solltemperatur",
    33: "Verbindungsqualität", 40: "Tastenzustand", 41: "Farbton",
    42: "Farbtemperatur", 43: "Hardwareversion", 44: "Firmwareversion",
    45: "Softwareversion", 49: "Solltemperatur-Korrektur", 51: "Spannung",
    52: "Übertemperaturalarm", 54: "Untertemperaturalarm", 61: "LED-Helligkeit",
    64: "Temperaturkorrektur", 65: "Wasserverbrauch gesamt", 68: "Leckagealarm",
    69: "Batteriewarnung", 70: "Störungsmeldung", 71: "Verbindungswarnung",
    72: "Betriebsmodus", 73: "Heizprogramm", 74: "Warmwasserprogramm",
    76: "Anwesenheit", 79: "Ölalarm", 80: "Wasseralarm", 85: "Sommermodus",
    87: "Automatikmodus", 89: "Vorlauftemperatur", 92: "Gerätetemperatur",
    94: "Luftdruck", 95: "Außenluftfeuchtigkeit", 96: "Innenluftfeuchtigkeit",
    97: "Außentemperatur", 98: "Innentemperatur", 99: "Lüftungsstufe",
    100: "Lüftungsmodus", 101: "Niederschlag", 104: "Betriebsstunden",
    112: "Rollladenmodus", 113: "Lamellenposition", 116: "Bodenfeuchtigkeit",
    124: "Farbmodus", 127: "Heiz-/Kühlmodus", 128: "Abwesenheit",
    129: "Heizstatus",
}

_AUTOMATION_ALARM_TYPES = {12, 16, 17, 25, 30, 52, 54, 68, 69, 70, 71, 76, 79, 80}


def _automation_nodes(nodes):
    """Add presentation-only metadata used by the no-code editor."""
    result = []
    for source_node in nodes:
        node = dict(source_node)
        attributes = []
        for source_attribute in source_node.get("attributes", []):
            attribute = dict(source_attribute)
            try:
                attribute_type = int(attribute.get("type", -1))
            except (TypeError, ValueError):
                attribute_type = -1
            raw_name = display_text(attribute.get("name", "")).strip()
            generic = not raw_name or raw_name.casefold() in {"attribut", "attribute", "wert", "value"}
            if raw_name.casefold().startswith(("attribute type", "attributtyp", "attribute_type")):
                generic = True
            friendly = _AUTOMATION_ATTRIBUTE_NAMES.get(attribute_type, f"Eigenschaft {attribute_type}")
            attribute["_display_name"] = friendly if generic else raw_name
            unit = display_text(attribute.get("unit", "")).strip()
            current = attribute.get("current_value", attribute.get("value"))
            current_text = "–" if current is None else display_text(current)
            attribute["_current_text"] = f"{current_text} {unit}".strip()
            if attribute_type == 14:
                attribute["_binary_labels"] = ["Geschlossen", "Geöffnet"]
            elif attribute_type in _AUTOMATION_ALARM_TYPES:
                attribute["_binary_labels"] = ["Kein Alarm", "Alarm"]
            elif attribute_type in {1, 9, 13, 19, 46, 76, 85, 87, 128}:
                attribute["_binary_labels"] = ["Aus", "Ein"]
            attributes.append(attribute)
        node["attributes"] = attributes
        result.append(node)
    return result


def automations_page(version, status, nodes=None, rules=None, selected_id="", authenticated=False, token_required=False, message="", error=""):
    nodes, rules = nodes or [], rules or []
    selected = next((item for item in rules if str(item.get("id", "")) == selected_id), None)
    cards = "".join(
        f'<a class="row {"selected" if selected_id == str(item["id"]) else ""}" href="/setup/automations?edit={escape(str(item["id"]))}"><span><b>{display_html(item["name"])}</b><small>{item.get("trigger_count",0)} Auslöser · {item.get("condition_count",0)} Bedingungen · {item.get("action_count",0)} Aktionen · {"Server" if item.get("origin") == "server" else "iPad"}</small></span><span class="badge {"ok" if item.get("enabled") else ""}">{"Aktiv" if item.get("enabled") else "Aus"}</span></a>'
        for item in status.get("automations", [])
    ) or '<p class="muted">Noch keine Automation angelegt.</p>'
    options = "".join(f'<option value="{escape(str(item["id"]))}">{display_html(item["name"])}</option>' for item in status.get("automations", []))
    events = "".join(f'<div class="event {escape(item.get("level",""))}"><time>{dt.datetime.fromtimestamp(item["timestamp"]).strftime("%d.%m. %H:%M:%S")}</time><span><b>{display_html(item.get("rule_name","Automation"))}</b><small>{display_html(item.get("message",""))}</small></span></div>' for item in status.get("recent_events", [])[:12]) or '<p class="muted">Noch keine Ausführung protokolliert.</p>'
    push = status.get("push", {})
    push_info = (
        f'<div class="panel"><h2>Apple Push</h2><p><span class="badge {"ok" if push.get("configured") else "bad"}">{"Konfiguriert" if push.get("configured") else "Schlüssel fehlen"}</span> '
        f'<span class="muted">· {int(push.get("device_count", 0))} registrierte iOS-Geräte</span></p>'
        '<p class="muted">Benötigt im Server: SHB_APNS_TEAM_ID, SHB_APNS_KEY_ID, SHB_APNS_BUNDLE_ID und SHB_APNS_KEY_PATH.</p></div>'
    )
    safe_nodes = json.dumps(_automation_nodes(nodes), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    safe_automations = json.dumps(
        [{"id": str(item.get("id", "")), "name": item.get("name", "Automation")} for item in rules],
        ensure_ascii=False, separators=(",", ":"),
    ).replace("</", "<\\/")
    safe_push_devices = json.dumps(status.get("push", {}).get("devices", []), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    safe_rule = json.dumps(selected or {}, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    delete_form = ""
    if selected:
        delete_form = f'''<form method="post" action="/setup/automations/delete" onsubmit="return confirm('Automation wirklich dauerhaft löschen?')">{token_field(token_required,authenticated)}<input type="hidden" name="rule_id" value="{escape(str(selected.get('id','')))}"><button class="danger">Automation löschen</button></form>'''
    editor = f'''
<section class="panel automation-editor"><div class="actions" style="justify-content:space-between;align-items:center"><div><h2>{"Automation bearbeiten" if selected else "Neue Automation erstellen"}</h2><p class="muted">Lege in drei Schritten fest, wann etwas passieren soll.</p></div><a class="button secondary" href="/setup/automations">＋ Neu</a></div>
<form id="automationEditor" method="post" action="/setup/automations/save">{token_field(token_required,authenticated)}<input type="hidden" name="rule_json" id="ruleJSON">
<div class="automation-basics"><label>Name der Automation<input id="ruleName" maxlength="120" placeholder="z. B. Licht bei Bewegung einschalten" required><small>Ein kurzer Name, an dem du die Automation später wiedererkennst.</small></label><label>Erneut auslösen frühestens nach<input id="cooldown" type="number" min="0" max="86400" step="1" value="30"><small>Sekunden – verhindert mehrfaches Auslösen direkt hintereinander.</small></label></div>
<div class="automation-settings"><label class="check"><input id="ruleEnabled" type="checkbox" checked> Automation ist aktiv</label><label>Zusätzliche Bedingungen prüfen<select id="conditionValidation"><option value="triggerTime">Sofort beim Auslösen</option><option value="executionTime">Erst vor der Ausführung</option><option value="both">Beim Auslösen und vor der Ausführung</option></select></label></div>
<div class="automation-block trigger-block"><div class="automation-heading"><span class="step-number">1</span><div><h3>Wann soll die Automation starten?</h3><p>Wähle einen Gerätewert oder eine Uhrzeit als Auslöser.</p></div><button type="button" class="secondary" onclick="addCondition('triggers',true)">＋ Weiteren Auslöser hinzufügen</button></div><div id="triggers" class="automation-list"></div></div>
<div class="automation-block condition-block"><div class="automation-heading"><span class="step-number">2</span><div><h3>Soll sie nur unter bestimmten Bedingungen laufen?</h3><p>Optional – beispielsweise nur, wenn es dunkel oder niemand zu Hause ist.</p></div><button type="button" class="secondary" onclick="addCondition('conditions',false)">＋ Bedingung hinzufügen</button></div><div id="conditions" class="automation-list"><p class="empty-hint">Keine zusätzliche Bedingung – die Automation darf immer laufen.</p></div></div>
<div class="automation-block action-block"><div class="automation-heading"><span class="step-number">3</span><div><h3>Was soll anschließend passieren?</h3><p>Füge einen Gerätebefehl, eine andere Automation oder eine Nachricht hinzu.</p></div><button type="button" class="secondary" onclick="addAction()">＋ Weitere Aktion hinzufügen</button></div><div id="actions" class="automation-list"></div></div>
<div class="automation-save"><div><b>Fertig?</b><small>Die Automation wird dauerhaft auf dem Server gespeichert.</small></div><button>Automation speichern</button></div></form><div class="actions" style="margin-top:10px">{delete_form}</div></section>
<style>.automation-editor{{padding:22px}}.automation-basics{{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin:20px 0 14px;padding:16px;border:1px solid var(--line);border-radius:14px;background:var(--bg)}}.automation-settings{{display:grid;grid-template-columns:1fr 1.4fr;gap:16px;align-items:end}}.automation-block{{border-top:1px solid var(--line);padding-top:21px;margin-top:22px}}.automation-heading{{display:grid;grid-template-columns:38px 1fr auto;gap:12px;align-items:center}}.automation-heading p{{margin:4px 0 0;color:var(--muted)}}.step-number{{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;background:var(--blue);color:white;font-weight:800;font-size:17px}}.automation-list{{display:grid;gap:12px;margin-top:14px}}.automation-row{{border:1px solid var(--line);background:var(--bg);border-radius:14px;padding:15px;display:grid;gap:10px;box-shadow:0 3px 12px #00000006}}.automation-row-title{{display:flex;justify-content:space-between;align-items:center;gap:10px}}.automation-row-title b{{font-size:14px}}.automation-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:end}}.automation-grid .dynamic{{display:grid!important;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;grid-column:1/-1}}.automation-row .remove{{background:transparent;color:var(--bad);border:1px solid #ef444455;padding:8px 11px}}.field-hint{{min-height:18px;font-weight:400}}.empty-hint{{border:1px dashed var(--line);border-radius:12px;padding:14px;color:var(--muted);margin:0}}.automation-save{{position:sticky;bottom:12px;margin-top:24px;padding:14px 16px;border-radius:14px;background:var(--panel);border:1px solid var(--line);box-shadow:0 10px 35px #0002;display:flex;justify-content:space-between;align-items:center;gap:15px;z-index:3}}.automation-save small{{display:block;margin-top:3px}}@media(max-width:800px){{.automation-basics,.automation-settings{{grid-template-columns:1fr}}.automation-heading{{grid-template-columns:38px 1fr}}.automation-heading button{{grid-column:1/-1}}}}@media(max-width:700px){{.automation-grid,.automation-grid .dynamic{{grid-template-columns:1fr}}.automation-save{{position:static;align-items:stretch;flex-direction:column}}}}</style>
<script>
const automationNodes={safe_nodes};const automationRules={safe_automations};const pushDevices={safe_push_devices};const initialRule={safe_rule};
const htmlEscape=value=>String(value??'').replace(/[&<>"']/g,char=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[char]));
const displayDecode=value=>{{let text=String(value??'');for(let index=0;index<3&&text.includes('%');index++){{try{{const decoded=decodeURIComponent(text);if(decoded===text)break;text=decoded}}catch(_error){{break}}}}return text}};
const uid=()=>crypto.randomUUID?crypto.randomUUID():`${{Date.now()}}-${{Math.random().toString(16).slice(2)}}`;
const numberValue=(value,fallback=0)=>Number.isFinite(Number(value))?Number(value):fallback;
function nodeOptions(selected,roborock=false,query=''){{const needle=query.trim().toLocaleLowerCase();return automationNodes.filter(node=>(!roborock||node.integration_module==='roborock')&&(!needle||`${{displayDecode(node.name||'')}} ${{node.id}}`.toLocaleLowerCase().includes(needle))).map(node=>`<option value="${{node.id}}" ${{String(node.id)===String(selected)?'selected':''}}>${{htmlEscape(displayDecode(node.name||`Gerät ${{node.id}}`))}}</option>`).join('')}}
function selectedNode(select){{return automationNodes.find(node=>String(node.id)===String(select.value))}}
function attributeOptions(nodeID,selected,editable=false){{const node=automationNodes.find(item=>String(item.id)===String(nodeID));return (node?.attributes||[]).filter(attribute=>!editable||attribute.editable).map(attribute=>{{const name=displayDecode(attribute._display_name||attribute.name||`Eigenschaft ${{attribute.id}}`),unit=displayDecode(attribute.unit||''),current=displayDecode(attribute._current_text||'');const detail=current&&current!=='–'?` · aktuell ${{current}}`:unit?` (${{unit}})`:'';return `<option value="${{attribute.id}}" ${{String(attribute.id)===String(selected)?'selected':''}}>${{htmlEscape(name+detail)}}</option>`}}).join('')}}
function selectedAttribute(nodeID,attributeID){{return (automationNodes.find(item=>String(item.id)===String(nodeID))?.attributes||[]).find(attribute=>String(attribute.id)===String(attributeID))}}
function attributeHint(nodeID,attributeID){{const attribute=selectedAttribute(nodeID,attributeID);if(!attribute)return 'Bitte eine Eigenschaft auswählen.';const parts=[];if(attribute._current_text&&attribute._current_text!=='–')parts.push(`Aktueller Wert: ${{displayDecode(attribute._current_text)}}`);if(attribute.minimum!=null||attribute.maximum!=null)parts.push(`Erlaubt: ${{attribute.minimum??'…'}} bis ${{attribute.maximum??'…'}}${{attribute.unit?' '+displayDecode(attribute.unit):''}}`);parts.push(attribute.editable?'Kann gelesen und geändert werden.':'Kann als Auslöser oder Bedingung verwendet werden.');return parts.join(' · ')}}
function valueEditor(nodeID,attributeID,value,className='value'){{const attribute=selectedAttribute(nodeID,attributeID),labels=attribute?._binary_labels;if(labels&&Number(attribute?.minimum??0)===0&&Number(attribute?.maximum??1)===1)return `<select class="${{className}}"><option value="0" ${{Number(value)===0?'selected':''}}>${{htmlEscape(labels[0])}}</option><option value="1" ${{Number(value)===1?'selected':''}}>${{htmlEscape(labels[1])}}</option></select>`;const min=attribute?.minimum!=null?` min="${{attribute.minimum}}"`:'',max=attribute?.maximum!=null?` max="${{attribute.maximum}}"`:'',step=attribute?.step_value??attribute?.stepValue??'any';return `<input class="${{className}}" type="number" step="${{step}}"${{min}}${{max}} value="${{numberValue(value,1)}}">`}}
function wireNodeSearch(dynamic,roborock=false){{const input=dynamic.querySelector('.nodeSearch'),select=dynamic.querySelector('.node');if(!input||!select)return;input.addEventListener('input',()=>{{const previous=select.value;select.innerHTML=nodeOptions(previous,roborock,input.value);if(select.value!==previous)select.dispatchEvent(new Event('change'))}})}}
function timeText(minutes){{const value=Math.max(0,Math.min(1439,numberValue(minutes)));return `${{String(Math.floor(value/60)).padStart(2,'0')}}:${{String(value%60).padStart(2,'0')}}`}}
function localDate(value){{const date=value?new Date(value):new Date(Date.now()+300000);if(Number.isNaN(date.getTime()))return '';const local=new Date(date.getTime()-date.getTimezoneOffset()*60000);return local.toISOString().slice(0,16)}}
function conditionKinds(trigger){{return trigger?[['attribute','Gerätewert'],['attributeChangedBy','Gerätewert ändert sich um'],['timeDaily','Täglich um'],['timeOnce','Einmalig am']]:[['attribute','Gerätewert'],['timeAfter','Uhrzeit nach'],['timeBefore','Uhrzeit vor']]}}
function addCondition(containerID,trigger,item={{}}){{const container=document.getElementById(containerID);container.querySelector('.empty-hint')?.remove();const row=document.createElement('div');row.className='automation-row';row.dataset.id=item.id||uid();row.dataset.trigger=trigger?'1':'0';row.innerHTML=`<div class="automation-row-title"><b>${{trigger?'Auslöser':'Zusätzliche Bedingung'}}</b><button type="button" class="remove" onclick="removeAutomationRow(this)">Entfernen</button></div><div class="automation-grid"><label>Was soll beobachtet werden?<select class="kind">${{conditionKinds(trigger).map(option=>`<option value="${{option[0]}}" ${{option[0]===(item.kind||'attribute')?'selected':''}}>${{option[1]}}</option>`).join('')}}</select></label><div class="dynamic"></div></div>`;container.appendChild(row);row.querySelector('.kind').addEventListener('change',()=>renderCondition(row,{{}}));renderCondition(row,item)}}
function removeAutomationRow(button){{const row=button.closest('.automation-row'),container=row.parentElement,isCondition=container.id==='conditions';row.remove();if(isCondition&&!container.querySelector('.automation-row'))container.innerHTML='<p class="empty-hint">Keine zusätzliche Bedingung – die Automation darf immer laufen.</p>'}}
function renderCondition(row,item){{const kind=row.querySelector('.kind').value,dynamic=row.querySelector('.dynamic');if(kind.startsWith('time')){{dynamic.innerHTML=kind==='timeOnce'?`<label>Datum und Uhrzeit<input class="scheduled" type="datetime-local" value="${{localDate(item.scheduledAt)}}"><small>Die Automation startet einmalig zu diesem Zeitpunkt.</small></label>`:`<label>Uhrzeit<input class="clock" type="time" value="${{timeText(item.minuteOfDay??1320)}}"><small>${{kind==='timeDaily'?'Wird jeden Tag zu dieser Uhrzeit ausgelöst.':'Wird als zusätzliche Zeitbedingung geprüft.'}}</small></label>`;return}}const nodeID=item.nodeID??automationNodes[0]?.id??0;dynamic.innerHTML=`<label>1. Gerät suchen und auswählen<input class="nodeSearch" type="search" placeholder="z. B. Wohnzimmer oder Thermostat"><select class="node">${{nodeOptions(nodeID)}}</select></label><label>2. Eigenschaft des Geräts<select class="attribute">${{attributeOptions(nodeID,item.attributeID,false)}}</select><small class="field-hint"></small></label><label>3. Vergleich<select class="comparison"><option value="equal">ist gleich</option><option value="notEqual">ist nicht gleich</option><option value="greater">ist größer als</option><option value="less">ist kleiner als</option></select></label><label>4. Vergleichswert<span class="valueField"></span></label>${{kind==='attributeChangedBy'?`<label>Änderung berechnen als<select class="changeUnit"><option value="absolute">Fester Wert</option><option value="percent">Prozent</option></select></label>`:''}}`;const nodeSelect=dynamic.querySelector('.node'),attributeSelect=dynamic.querySelector('.attribute'),refreshAttribute=()=>{{dynamic.querySelector('.field-hint').textContent=attributeHint(nodeSelect.value,attributeSelect.value);dynamic.querySelector('.valueField').innerHTML=valueEditor(nodeSelect.value,attributeSelect.value,item.value)}};dynamic.querySelector('.comparison').value=item.comparison||'equal';if(dynamic.querySelector('.changeUnit'))dynamic.querySelector('.changeUnit').value=item.changeUnit||'absolute';nodeSelect.addEventListener('change',event=>{{attributeSelect.innerHTML=attributeOptions(event.target.value,'',false);refreshAttribute()}});attributeSelect.addEventListener('change',refreshAttribute);wireNodeSearch(dynamic);refreshAttribute()}}
function choiceValues(nodeID,name){{const node=automationNodes.find(item=>String(item.id)===String(nodeID)),attribute=(node?.attributes||[]).find(item=>item.name===name);if(!attribute)return[];try{{const parsed=typeof attribute.data==='string'?JSON.parse(attribute.data):attribute.data;if(Array.isArray(parsed))return parsed;if(Array.isArray(parsed?.options))return parsed.options}}catch(_error){{}}return[]}}
function choiceOptions(nodeID,name,selected,emptyLabel='Nicht ändern'){{return `<option value="">${{emptyLabel}}</option>`+choiceValues(nodeID,name).map(item=>`<option value="${{item.value}}" ${{String(item.value)===String(selected)?'selected':''}}>${{htmlEscape(displayDecode(item.label??item.name??item.value))}}</option>`).join('')}}
function addAction(item={{}}){{const container=document.getElementById('actions'),row=document.createElement('div');row.className='automation-row';row.dataset.id=item.id||uid();row.innerHTML=`<div class="automation-grid"><label>Art<select class="kind"><option value="setAttribute">Gerät steuern</option><option value="toggleAttribute">Schalter umschalten</option><option value="roborockCleaning">Roborock reinigen</option><option value="controlAutomation">Andere Automation steuern</option><option value="serverPushNotification">Push-Nachricht vom Server</option></select></label><div class="dynamic" style="display:contents"></div><label>Verzögerung<input class="delay" type="number" min="0" max="86400" step="1" value="${{numberValue(item.delaySeconds)}}"><small>Sekunden</small></label><button type="button" class="remove" onclick="this.closest('.automation-row').remove()">Entfernen</button></div>`;container.appendChild(row);row.querySelector('.kind').value=item.kind||'setAttribute';row.querySelector('.kind').addEventListener('change',()=>renderAction(row,{{}}));renderAction(row,item)}}
function renderAction(row,item){{const kind=row.querySelector('.kind').value,dynamic=row.querySelector('.dynamic');if(kind==='controlAutomation'){{dynamic.innerHTML=`<label>Automation<select class="automation">${{automationRules.filter(rule=>String(rule.id)!==String(initialRule.id)).map(rule=>`<option value="${{htmlEscape(rule.id)}}" ${{String(rule.id)===String(item.automationID)?'selected':''}}>${{htmlEscape(displayDecode(rule.name))}}</option>`).join('')}}</select></label><label>Befehl<select class="operation"><option value="play">Abspielen</option><option value="stop">Laufzeit stoppen</option><option value="enable">Aktivieren</option><option value="disable">Deaktivieren</option></select><small>Stoppen bricht laufende Verzögerungen ab.</small></label>`;dynamic.querySelector('.operation').value=item.automationOperation||'play';return}}if(kind==='serverPushNotification'){{const nodeID=item.nodeID??automationNodes[0]?.id??0;dynamic.innerHTML=`<label>Überschrift<input class="title" maxlength="180" value="${{htmlEscape(item.title||'SmartHomeBoard')}}"></label><label>Nachricht<textarea class="message" maxlength="1500">${{htmlEscape(item.message||'Automation wurde ausgeführt.')}}</textarea></label><label class="check"><input class="includeValue" type="checkbox" ${{item.includeAttributeValue?'checked':''}}> Gerätewert einfügen</label><label class="pushNode">Gerät<input class="nodeSearch" type="search" placeholder="Gerät suchen …"><select class="node">${{nodeOptions(nodeID)}}</select></label><label class="pushAttribute">Attribut<select class="attribute">${{attributeOptions(nodeID,item.attributeID,false)}}</select><small>Platzhalter: {{selectedDevice}}, {{selectedAttribute}}, {{selectedValue}}</small></label>`;const update=()=>{{const visible=dynamic.querySelector('.includeValue').checked;dynamic.querySelector('.pushNode').style.display=visible?'grid':'none';dynamic.querySelector('.pushAttribute').style.display=visible?'grid':'none'}};dynamic.querySelector('.includeValue').addEventListener('change',update);dynamic.querySelector('.node').addEventListener('change',event=>{{dynamic.querySelector('.attribute').innerHTML=attributeOptions(event.target.value,'',false)}});wireNodeSearch(dynamic);update();return}}if(kind==='roborockCleaning'){{const nodeID=item.nodeID??automationNodes.find(node=>node.integration_module==='roborock')?.id??0;dynamic.innerHTML=`<label>Roborock<input class="nodeSearch" type="search" placeholder="Roborock suchen …"><select class="node">${{nodeOptions(nodeID,true)}}</select></label><label>Reinigungsart<select class="cleaning">${{choiceOptions(nodeID,'Reinigungsart',item.roborockCleaningType)}}</select></label><label>Saugstufe<select class="suction">${{choiceOptions(nodeID,'Saugstufe',item.roborockSuction)}}</select></label><label>Wassermenge<select class="water">${{choiceOptions(nodeID,'Wassermenge',item.roborockWater)}}</select></label><label>Ziel<select class="target"><option value="complete">Komplette Fläche</option><option value="room">Raum</option><option value="routine">Routine</option><option value="spot">Punktreinigung</option></select></label><label>Zielwert<select class="targetValue"></select></label>`;const target=dynamic.querySelector('.target');target.value=item.roborockTarget||'complete';const refresh=()=>{{const name=target.value==='room'?'Raum auswählen':target.value==='routine'?'Routine auswählen':null;dynamic.querySelector('.targetValue').innerHTML=name?choiceOptions(dynamic.querySelector('.node').value,name,item.roborockTargetValue,'Bitte auswählen'):'<option value="-1">Gesamte Fläche</option>'}};target.addEventListener('change',refresh);dynamic.querySelector('.node').addEventListener('change',()=>renderAction(row,{{kind:'roborockCleaning',nodeID:dynamic.querySelector('.node').value,roborockTarget:target.value}}));wireNodeSearch(dynamic,true);refresh();return}}const editable=true,nodeID=item.nodeID??automationNodes.find(node=>(node.attributes||[]).some(attribute=>attribute.editable))?.id??0;dynamic.innerHTML=`<label>Gerät<input class="nodeSearch" type="search" placeholder="Gerät suchen …"><select class="node">${{nodeOptions(nodeID)}}</select></label><label>Attribut<select class="attribute">${{attributeOptions(nodeID,item.attributeID,editable)}}</select></label>${{kind==='setAttribute'?`<label>Zielwert<input class="value" type="number" step="any" value="${{numberValue(item.value,1)}}"></label>`:''}}`;dynamic.querySelector('.node').addEventListener('change',event=>{{dynamic.querySelector('.attribute').innerHTML=attributeOptions(event.target.value,'',editable)}});wireNodeSearch(dynamic)}}
function collectCondition(row){{const kind=row.querySelector('.kind').value,result={{id:row.dataset.id,kind}};if(kind==='timeOnce'){{result.scheduledAt=new Date(row.querySelector('.scheduled').value).toISOString();result.isConsumed=false}}else if(kind.startsWith('time')){{const parts=row.querySelector('.clock').value.split(':');result.minuteOfDay=Number(parts[0])*60+Number(parts[1])}}else{{result.nodeID=Number(row.querySelector('.node').value);result.attributeID=Number(row.querySelector('.attribute').value);result.comparison=row.querySelector('.comparison').value;result.value=Number(row.querySelector('.value').value);if(kind==='attributeChangedBy')result.changeUnit=row.querySelector('.changeUnit').value}}return result}}
function optionalNumber(element){{return element&&element.value!==''?Number(element.value):null}}
function collectAction(row){{const kind=row.querySelector('.kind').value,result={{id:row.dataset.id,kind,delaySeconds:Number(row.querySelector('.delay').value||0)}};if(kind==='controlAutomation'){{result.automationID=row.querySelector('.automation').value;result.automationOperation=row.querySelector('.operation').value}}else if(kind==='serverPushNotification'){{result.title=row.querySelector('.title').value;result.message=row.querySelector('.message').value;result.includeAttributeValue=row.querySelector('.includeValue').checked;if(result.includeAttributeValue){{result.nodeID=Number(row.querySelector('.node').value);result.attributeID=Number(row.querySelector('.attribute').value)}}}}else if(kind==='roborockCleaning'){{result.nodeID=Number(row.querySelector('.node').value);result.roborockCleaningType=optionalNumber(row.querySelector('.cleaning'));result.roborockSuction=optionalNumber(row.querySelector('.suction'));result.roborockWater=optionalNumber(row.querySelector('.water'));result.roborockTarget=row.querySelector('.target').value;result.roborockTargetValue=Number(row.querySelector('.targetValue').value||-1)}}else{{result.nodeID=Number(row.querySelector('.node').value);result.attributeID=Number(row.querySelector('.attribute').value);if(kind==='setAttribute')result.value=Number(row.querySelector('.value').value)}}return result}}
document.getElementById('automationEditor').addEventListener('submit',event=>{{try{{const rule={{id:initialRule.id||uid(),name:document.getElementById('ruleName').value.trim(),isEnabled:document.getElementById('ruleEnabled').checked,cooldownSeconds:Number(document.getElementById('cooldown').value||0),conditionValidation:document.getElementById('conditionValidation').value,triggers:[...document.querySelectorAll('#triggers .automation-row')].map(collectCondition),conditions:[...document.querySelectorAll('#conditions .automation-row')].map(collectCondition),actions:[...document.querySelectorAll('#actions .automation-row')].map(collectAction)}};if(!rule.triggers.length||!rule.actions.length)throw new Error('Mindestens ein Auslöser und eine Aktion sind erforderlich.');document.getElementById('ruleJSON').value=JSON.stringify(rule)}}catch(error){{event.preventDefault();alert(error.message)}}}});
const baseRenderAction=renderAction;renderAction=(row,item)=>{{baseRenderAction(row,item);if(row.querySelector('.kind').value!=='serverPushNotification')return;const label=document.createElement('label');label.className='pushRecipients';label.innerHTML=`Empfänger<select class="recipients" multiple size="${{Math.max(2,Math.min(6,pushDevices.length+1))}}"><option value="" ${{!(item.pushDeviceIDs||[]).length?'selected':''}}>Alle registrierten Geräte</option>${{pushDevices.map(device=>`<option value="${{htmlEscape(device.id)}}" ${{(item.pushDeviceIDs||[]).includes(device.id)?'selected':''}}>${{htmlEscape(displayDecode(device.name))}}</option>`).join('')}}</select><small>Mit Cmd/Strg können mehrere Geräte gewählt werden.</small>`;row.querySelector('.dynamic').appendChild(label)}};const baseCollectAction=collectAction;collectAction=row=>{{const result=baseCollectAction(row);if(result.kind==='serverPushNotification'){{const values=[...row.querySelector('.recipients').selectedOptions].map(option=>option.value).filter(Boolean);result.pushDeviceIDs=values}}return result}};
document.getElementById('ruleName').value=initialRule.name||'';document.getElementById('ruleEnabled').checked=initialRule.isEnabled!==false;document.getElementById('cooldown').value=initialRule.cooldownSeconds??30;document.getElementById('conditionValidation').value=initialRule.conditionValidation||'triggerTime';(initialRule.triggers?.length?initialRule.triggers:[{{kind:'attribute'}}]).forEach(item=>addCondition('triggers',true,item));(initialRule.conditions||[]).forEach(item=>addCondition('conditions',false,item));(initialRule.actions?.length?initialRule.actions:[{{kind:'setAttribute'}}]).forEach(addAction);
</script>'''
    body = f'''{notice(message,error)}<div class="split"><section><div class="panel"><div class="actions" style="justify-content:space-between"><h2 id="automationOverviewCount">Serverregeln · {status.get("count",0)}</h2><a class="button" href="/setup/automations">＋ Automation</a></div><div class="list" id="automationOverviewList">{cards}</div></div>{push_info}<div class="panel"><h2>Automation testen</h2><p class="muted">Der Test führt die Aktionen wirklich aus.</p><form method="post" action="/setup/automations/test">{token_field(token_required,authenticated)}<label>Automation<select name="rule_id" required>{options}</select></label><button {"" if options else "disabled"}>Jetzt testen</button></form></div><div class="panel"><h2>Letzte 12 Ereignisse</h2><div class="list">{events}</div></div></section><section>{editor}</section></div>'''
    body += '''<script>
const overviewEscape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
let automationOverviewSignature='';
async function refreshAutomationOverview(){
  if(document.visibilityState!=='visible')return;
  try{
    const response=await fetch('/setup/automations/status',{cache:'no-store'});
    if(!response.ok)return;
    const status=await response.json(),items=status.automations||[];
    const signature=JSON.stringify(items);
    if(signature===automationOverviewSignature)return;
    automationOverviewSignature=signature;
    document.getElementById('automationOverviewCount').textContent=`Serverregeln · ${status.count||0}`;
    const selected=new URLSearchParams(location.search).get('edit')||'';
    document.getElementById('automationOverviewList').innerHTML=items.length?items.map(item=>`<a class="row ${String(item.id)===selected?'selected':''}" href="/setup/automations?edit=${encodeURIComponent(item.id)}"><span><b>${overviewEscape(item.name||'Automation')}</b><small>${Number(item.trigger_count||0)} Auslöser · ${Number(item.condition_count||0)} Bedingungen · ${Number(item.action_count||0)} Aktionen · ${item.origin==='server'?'Server':'iPad'}</small></span><span class="badge ${item.enabled?'ok':''}">${item.enabled?'Aktiv':'Aus'}</span></a>`).join(''):'<p class="muted">Noch keine Automation angelegt.</p>';
  }catch(_error){}
}
automationOverviewSignature=JSON.stringify(%s);
setInterval(refreshAutomationOverview,2000);
document.addEventListener('visibilitychange',refreshAutomationOverview);
</script>''' % json.dumps(status.get("automations", []), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return shell("Automationen", "Lokal anlegen, bearbeiten und dauerhaft ohne iPad ausführen.", body, version, "automations")
