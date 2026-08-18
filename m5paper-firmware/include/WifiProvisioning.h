#pragma once

#include <Arduino.h>
#include <M5EPD.h>
#include <Preferences.h>

class WifiProvisioning
{
public:
    explicit WifiProvisioning(M5EPD_Canvas& canvas);

    bool connectOrConfigure(bool forceSetup = false);
    const String& connectedSsid() const;

private:
    static constexpr uint8_t MAX_NETWORKS = 24;
    static constexpr uint8_t NETWORKS_PER_PAGE = 8;

    struct NetworkEntry
    {
        String ssid;
        int32_t rssi = -100;
        bool secured = true;
    };

    enum class KeyboardMode
    {
        lower,
        upper,
        symbols
    };

    struct TouchPoint
    {
        int16_t x;
        int16_t y;
    };

    M5EPD_Canvas& canvas;
    M5EPD_Canvas fieldCanvas;
    Preferences preferences;
    NetworkEntry networks[MAX_NETWORKS];
    uint8_t networkCount = 0;
    String activeSsid;

    bool loadCredentials(String& ssid, String& password);
    void saveCredentials(const String& ssid, const String& password);
    bool connectTo(const String& ssid, const String& password, bool showFailure);
    bool runInteractiveSetup();
    bool scanNetworks();
    int chooseNetwork();
    bool enterPassword(const NetworkEntry& network, String& password);

    void drawNetworkPage(uint8_t page);
    void drawKeyboard(const String& ssid, const String& password, KeyboardMode mode);
    void updatePasswordField(const String& password);
    void drawMessage(const String& title, const String& detail, const String& footer = "");
    void drawButton(int x, int y, int w, int h, const String& label, bool emphasized = false);
    void drawKey(int x, int y, int w, int h, const String& label);

    TouchPoint waitForTap();
    void waitForRelease();
    static bool contains(const TouchPoint& point, int x, int y, int w, int h);
    static String signalText(int32_t rssi);
};
