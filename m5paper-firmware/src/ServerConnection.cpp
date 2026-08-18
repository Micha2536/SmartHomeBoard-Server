#include "ServerConnection.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiUdp.h>

namespace
{
constexpr uint16_t DISCOVERY_PORT = 8788;
constexpr uint32_t DISCOVERY_TIMEOUT_MS = 6000;
constexpr char DISCOVERY_REQUEST[] = "SHB_DISCOVER_V1";
constexpr char FIRMWARE_VERSION[] = "1.2.3";
}

bool ServerConnection::discoverAndRegister(ServerRegistration& result)
{
    String registrationPath;
    if (!discover(result, registrationPath))
    {
        return false;
    }
    return registerDevice(result, registrationPath);
}

bool ServerConnection::fetchConfiguration(
    const ServerRegistration& registration,
    ServerRenderConfiguration& result
)
{
    const String token = loadDeviceToken(registration.serverId);
    if (token.isEmpty())
    {
        result.error = "Display-Token fehlt.";
        return false;
    }

    HTTPClient http;
    const String url = "http://" + registration.host + ":" + String(registration.port) +
        "/api/v1/displays/device/" + deviceId() + "/configuration";
    if (!http.begin(url))
    {
        result.error = "Konfigurationsadresse ist ungueltig.";
        return false;
    }
    http.setConnectTimeout(5000);
    http.setTimeout(8000);
    http.addHeader("Accept", "application/json");
    http.addHeader("X-Display-Token", token);

    const int statusCode = http.GET();
    const String responseBody = http.getString();
    http.end();
    if (statusCode != 200)
    {
        result.error = "Konfiguration fehlgeschlagen (HTTP " + String(statusCode) + ").";
        return false;
    }

    JsonDocument document;
    if (deserializeJson(document, responseBody))
    {
        result.error = "Konfigurationsantwort ist ungueltig.";
        return false;
    }
    if (String(document["status"] | "pending") != "paired")
    {
        result.error = "Display ist noch nicht gekoppelt.";
        return false;
    }

    result.version = document["configuration_version"] | 0;
    JsonObject render = document["render"].as<JsonObject>();
    result.title = String(render["title"] | "SmartHomeBoard");
    result.layout = String(render["layout"] | "list");
    result.generatedAt = String(render["generated_at"] | "");
    result.sleepMinutes = constrain(static_cast<int>(render["sleep_minutes"] | 5), 1, 1440);
    result.widgets.clear();

    for (JsonObject item : render["widgets"].as<JsonArray>())
    {
        ServerRenderWidget widget;
        widget.id = String(item["id"] | "");
        widget.label = String(item["label"] | "Wert");
        widget.value = String(item["value"] | "--");
        widget.unit = String(item["unit"] | "");
        widget.available = item["available"] | false;
        result.widgets.push_back(widget);
    }

    Serial.printf(
        "Konfiguration %u geladen: %u Werte, Schlafzeit %u Minuten\n",
        result.version,
        static_cast<unsigned>(result.widgets.size()),
        result.sleepMinutes
    );
    return true;
}

bool ServerConnection::discover(ServerRegistration& result, String& registrationPath)
{
    WiFiUDP udp;
    if (!udp.begin(0))
    {
        result.error = "UDP-Suche konnte nicht gestartet werden.";
        return false;
    }

    udp.beginPacket(IPAddress(255, 255, 255, 255), DISCOVERY_PORT);
    udp.write(reinterpret_cast<const uint8_t*>(DISCOVERY_REQUEST), strlen(DISCOVERY_REQUEST));
    udp.endPacket();

    const uint32_t startedAt = millis();
    while (millis() - startedAt < DISCOVERY_TIMEOUT_MS)
    {
        const int packetSize = udp.parsePacket();
        if (packetSize <= 0)
        {
            delay(50);
            continue;
        }

        String response;
        response.reserve(packetSize);
        while (udp.available())
        {
            response += static_cast<char>(udp.read());
        }

        JsonDocument document;
        if (deserializeJson(document, response) || document["service"] != "SmartHomeBoard")
        {
            continue;
        }

        result.host = udp.remoteIP().toString();
        result.port = document["api_port"] | 8787;
        result.serverId = document["server_id"] | "";
        registrationPath = document["registration_path"] | "/api/v1/displays/register";
        udp.stop();
        Serial.printf("SmartHomeBoard-Server gefunden: %s:%u\n", result.host.c_str(), result.port);
        return true;
    }

    udp.stop();
    result.error = "Kein SmartHomeBoard-Server im WLAN gefunden.";
    return false;
}

bool ServerConnection::registerDevice(
    ServerRegistration& result,
    const String& registrationPath
)
{
    JsonDocument requestDocument;
    requestDocument["device_id"] = deviceId();
    requestDocument["name"] = "M5Paper";
    requestDocument["model"] = "M5Paper";
    requestDocument["firmware_version"] = FIRMWARE_VERSION;

    const String savedToken = loadDeviceToken(result.serverId);
    if (!savedToken.isEmpty())
    {
        requestDocument["device_token"] = savedToken;
    }

    String requestBody;
    serializeJson(requestDocument, requestBody);

    HTTPClient http;
    const String url = "http://" + result.host + ":" + String(result.port) + registrationPath;
    if (!http.begin(url))
    {
        result.error = "Serveradresse konnte nicht geoeffnet werden.";
        return false;
    }
    http.setConnectTimeout(5000);
    http.setTimeout(8000);
    http.addHeader("Content-Type", "application/json");

    const int statusCode = http.POST(requestBody);
    const String responseBody = http.getString();
    http.end();

    if (statusCode != 200)
    {
        result.error = "Registrierung fehlgeschlagen (HTTP " + String(statusCode) + ").";
        Serial.printf("Registrierung fehlgeschlagen: %d %s\n", statusCode, responseBody.c_str());
        return false;
    }

    JsonDocument responseDocument;
    if (deserializeJson(responseDocument, responseBody))
    {
        result.error = "Serverantwort ist ungueltig.";
        return false;
    }

    result.status = responseDocument["status"] | "pending";
    result.pairingCode = responseDocument["pairing_code"] | "";
    result.configurationVersion = responseDocument["configuration_version"] | 0;

    const String issuedToken = responseDocument["device_token"] | "";
    if (!issuedToken.isEmpty())
    {
        saveDeviceToken(result.serverId, issuedToken);
    }

    Serial.printf("M5Paper registriert, Status: %s\n", result.status.c_str());
    return true;
}

String ServerConnection::loadDeviceToken(const String& serverId)
{
    if (!preferences.begin("shb-server", true))
    {
        return "";
    }
    const String savedServerId = preferences.getString("server-id", "");
    const String token = savedServerId == serverId
        ? preferences.getString("device-token", "")
        : "";
    preferences.end();
    return token;
}

void ServerConnection::saveDeviceToken(const String& serverId, const String& token)
{
    if (!preferences.begin("shb-server", false))
    {
        return;
    }
    preferences.putString("server-id", serverId);
    preferences.putString("device-token", token);
    preferences.end();
}

String ServerConnection::deviceId()
{
    String mac = WiFi.macAddress();
    mac.toLowerCase();
    mac.replace(":", "");
    return "m5paper-" + mac;
}
