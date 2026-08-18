#pragma once

#include <Arduino.h>
#include <Preferences.h>
#include <vector>

struct ServerRegistration
{
    String host;
    uint16_t port = 0;
    String serverId;
    String status;
    String pairingCode;
    uint32_t configurationVersion = 0;
    String error;
};

struct ServerRenderWidget
{
    String id;
    String label;
    String value;
    String unit;
    bool available = false;
};

struct ServerRenderConfiguration
{
    String title = "SmartHomeBoard";
    String layout = "list";
    String generatedAt;
    uint16_t sleepMinutes = 5;
    uint32_t version = 0;
    std::vector<ServerRenderWidget> widgets;
    String error;
};

class ServerConnection
{
public:
    bool discoverAndRegister(ServerRegistration& result);
    bool fetchConfiguration(
        const ServerRegistration& registration,
        ServerRenderConfiguration& result
    );

private:
    Preferences preferences;

    bool discover(ServerRegistration& result, String& registrationPath);
    bool registerDevice(ServerRegistration& result, const String& registrationPath);
    String loadDeviceToken(const String& serverId);
    void saveDeviceToken(const String& serverId, const String& token);
    static String deviceId();
};
