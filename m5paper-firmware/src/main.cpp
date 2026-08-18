#include <Arduino.h>
#include <M5EPD.h>
#include <WiFi.h>
#include <esp_sleep.h>

#include "WifiProvisioning.h"
#include "ServerConnection.h"

namespace
{
constexpr int SCREEN_WIDTH = 540;
constexpr int SCREEN_HEIGHT = 960;

// Modellwerte fuer die angezeigte Akku-Restlaufzeit. Die Berechnung verwendet
// den fuer dieses Display serverseitig konfigurierten Aktualisierungstakt.
constexpr float M5_BATTERY_CAPACITY_MAH = 1150.0F;
constexpr float ESTIMATED_ACTIVE_CURRENT_MA = 150.0F;
constexpr float ESTIMATED_ACTIVE_SECONDS = 4.0F;
constexpr float ESTIMATED_SLEEP_CURRENT_MA = 1.5F;

M5EPD_Canvas mainCanvas(&M5.EPD);

bool wifiSetupRequestedAtBoot()
{
    const uint32_t startedAt = millis();

    while (millis() - startedAt < 1200)
    {
        M5.update();
        if (M5.BtnP.pressedFor(700))
        {
            Serial.println("WLAN-Einrichtung wurde ueber die mittlere Taste angefordert.");
            return true;
        }
        delay(20);
    }

    return false;
}

void showServerScreen(const String& ssid, const ServerRegistration& registration)
{
    mainCanvas.fillCanvas(0);
    mainCanvas.setTextColor(15);
    mainCanvas.setFreeFont(&FreeSansBold18pt7b);
    mainCanvas.drawCentreString("WLAN verbunden", SCREEN_WIDTH / 2, 170, 1);

    mainCanvas.setFreeFont(&FreeSans12pt7b);
    mainCanvas.drawCentreString(ssid, SCREEN_WIDTH / 2, 245, 1);
    mainCanvas.drawCentreString(
        "IP: " + WiFi.localIP().toString(),
        SCREEN_WIDTH / 2,
        290,
        1
    );

    mainCanvas.drawLine(56, 360, 484, 360, 10);

    mainCanvas.setFreeFont(&FreeSansBold18pt7b);
    if (registration.host.isEmpty())
    {
        mainCanvas.drawCentreString("Server nicht gefunden", SCREEN_WIDTH / 2, 425, 1);
        mainCanvas.setFreeFont(&FreeSans12pt7b);
        mainCanvas.drawCentreString(registration.error, SCREEN_WIDTH / 2, 500, 1);
        mainCanvas.drawCentreString("Server starten und M5Paper neu starten.", SCREEN_WIDTH / 2, 555, 1);
    }
    else if (registration.status == "pending")
    {
        mainCanvas.drawCentreString("Kopplungscode", SCREEN_WIDTH / 2, 420, 1);
        mainCanvas.setFreeFont(&FreeSansBold18pt7b);
        mainCanvas.drawCentreString(registration.pairingCode, SCREEN_WIDTH / 2, 510, 1);
        mainCanvas.setFreeFont(&FreeSans12pt7b);
        mainCanvas.drawCentreString("In App oder Webportal bestaetigen", SCREEN_WIDTH / 2, 590, 1);
    }
    else
    {
        mainCanvas.drawCentreString("Display gekoppelt", SCREEN_WIDTH / 2, 435, 1);
        mainCanvas.setFreeFont(&FreeSans12pt7b);
        mainCanvas.drawCentreString("Konfiguration wird vom Server geladen.", SCREEN_WIDTH / 2, 520, 1);
        mainCanvas.drawCentreString(
            "Konfigurationsstand: " + String(registration.configurationVersion),
            SCREEN_WIDTH / 2,
            565,
            1
        );
    }

    mainCanvas.setFreeFont(&FreeSans12pt7b);
    mainCanvas.drawCentreString(
        registration.host.isEmpty()
            ? ""
            : "Server: " + registration.host + ":" + String(registration.port),
        SCREEN_WIDTH / 2,
        700,
        1
    );

    mainCanvas.pushCanvas(0, 0, UPDATE_MODE_GC16);
}

String shortened(const String& text, size_t maximum)
{
    if (text.length() <= maximum) return text;
    return text.substring(0, maximum > 3 ? maximum - 3 : maximum) + "...";
}

float interpolateBatteryLevel(
    float voltage,
    float lowVoltage,
    float highVoltage,
    float lowLevel,
    float highLevel
)
{
    const float position =
        (voltage - lowVoltage) / (highVoltage - lowVoltage);
    return lowLevel + position * (highLevel - lowLevel);
}

float estimateBatteryLevel(float voltage)
{
    // Vereinfachte LiPo-Entladekurve. Die Spannung kann waehrend der
    // WLAN-Verbindung etwas niedriger ausfallen, daher bleibt dies ein
    // gut lesbarer Naeherungswert und keine exakte Kapazitaetsmessung.
    if (voltage >= 4.15F) return 1.00F;
    if (voltage >= 4.05F) return interpolateBatteryLevel(voltage, 4.05F, 4.15F, 0.90F, 1.00F);
    if (voltage >= 3.95F) return interpolateBatteryLevel(voltage, 3.95F, 4.05F, 0.75F, 0.90F);
    if (voltage >= 3.85F) return interpolateBatteryLevel(voltage, 3.85F, 3.95F, 0.55F, 0.75F);
    if (voltage >= 3.75F) return interpolateBatteryLevel(voltage, 3.75F, 3.85F, 0.30F, 0.55F);
    if (voltage >= 3.65F) return interpolateBatteryLevel(voltage, 3.65F, 3.75F, 0.15F, 0.30F);
    if (voltage >= 3.50F) return interpolateBatteryLevel(voltage, 3.50F, 3.65F, 0.05F, 0.15F);
    if (voltage >= 3.40F) return interpolateBatteryLevel(voltage, 3.40F, 3.50F, 0.00F, 0.05F);
    return 0.0F;
}

String formatRemainingBatteryRuntime(float voltage, uint16_t sleepMinutes)
{
    const float cycleSeconds = static_cast<float>(sleepMinutes) * 60.0F;
    const float activeSeconds = min(ESTIMATED_ACTIVE_SECONDS, cycleSeconds);
    const float averageCurrentMa =
        (
            ESTIMATED_ACTIVE_CURRENT_MA * activeSeconds +
            ESTIMATED_SLEEP_CURRENT_MA * (cycleSeconds - activeSeconds)
        ) /
        cycleSeconds;
    const int totalHours = static_cast<int>(floorf(
        M5_BATTERY_CAPACITY_MAH * estimateBatteryLevel(voltage) /
        averageCurrentMa
    ));

    if (totalHours < 1) return "unter 1 h";
    if (totalHours < 24) return String(totalHours) + " h";
    return String(totalHours / 24) + " T " + String(totalHours % 24) + " h";
}

void showPaperConfiguration(const ServerRenderConfiguration& configuration)
{
    mainCanvas.fillCanvas(0);
    mainCanvas.setTextColor(15);
    mainCanvas.setFreeFont(&FreeSansBold18pt7b);
    mainCanvas.drawString(shortened(configuration.title, 26), 28, 28);
    mainCanvas.drawLine(28, 92, 512, 92, 12);

    if (configuration.widgets.empty())
    {
        mainCanvas.setFreeFont(&FreeSansBold18pt7b);
        mainCanvas.drawCentreString("Noch keine Werte", SCREEN_WIDTH / 2, 330, 1);
        mainCanvas.setFreeFont(&FreeSans12pt7b);
        mainCanvas.drawCentreString("Konfiguration in der iOS-App oeffnen", SCREEN_WIDTH / 2, 410, 1);
        mainCanvas.drawCentreString("und Serverwerte hinzufuegen.", SCREEN_WIDTH / 2, 455, 1);
    }
    else if (configuration.layout == "grid")
    {
        constexpr int boxWidth = 236;
        constexpr int boxHeight = 174;
        for (size_t index = 0; index < configuration.widgets.size() && index < 8; ++index)
        {
            const int column = index % 2;
            const int row = index / 2;
            const int x = 28 + column * 248;
            const int y = 126 + row * 188;
            const ServerRenderWidget& widget = configuration.widgets[index];
            mainCanvas.drawRoundRect(x, y, boxWidth, boxHeight, 12, 10);
            mainCanvas.setFreeFont(&FreeSans12pt7b);
            mainCanvas.drawString(shortened(widget.label, 18), x + 14, y + 15);
            mainCanvas.setFreeFont(&FreeSansBold18pt7b);
            const String valueText = widget.value + (widget.unit.isEmpty() ? "" : " " + widget.unit);
            mainCanvas.drawCentreString(shortened(valueText, 15), x + boxWidth / 2, y + 82, 1);
        }
    }
    else
    {
        for (size_t index = 0; index < configuration.widgets.size() && index < 8; ++index)
        {
            const int y = 120 + index * 94;
            const ServerRenderWidget& widget = configuration.widgets[index];
            mainCanvas.drawLine(28, y + 84, 512, y + 84, 3);
            mainCanvas.setFreeFont(&FreeSans12pt7b);
            mainCanvas.drawString(shortened(widget.label, 25), 36, y + 16);
            mainCanvas.setFreeFont(&FreeSansBold18pt7b);
            const String valueText = widget.value + (widget.unit.isEmpty() ? "" : " " + widget.unit);
            const String fittedValue = shortened(valueText, 15);
            mainCanvas.drawString(fittedValue, 500 - mainCanvas.textWidth(fittedValue), y + 11);
        }
    }

    String timestamp = configuration.generatedAt;
    timestamp.replace("T", " ");
    if (timestamp.length() > 16) timestamp = timestamp.substring(0, 16);
    mainCanvas.setFreeFont(&FreeSans12pt7b);
    mainCanvas.drawLine(28, 865, 512, 865, 3);
    mainCanvas.drawString("Stand: " + timestamp, 28, 878);
    const String interval = "Takt: " + String(configuration.sleepMinutes) + " Min.";
    mainCanvas.drawString(interval, 512 - mainCanvas.textWidth(interval), 878);

    const float batteryVoltage = M5.getBatteryVoltage() / 1000.0F;
    const String batteryText =
        "Akku " + String(batteryVoltage, 2) + " V  |  Restlaufzeit ca. " +
        formatRemainingBatteryRuntime(batteryVoltage, configuration.sleepMinutes);
    mainCanvas.drawCentreString(batteryText, SCREEN_WIDTH / 2, 916, 1);

    // Das Panel vor dem neuen Vollbild elektrisch sauber auf Weiss setzen.
    // Ein GC16-Canvas-Update allein kann nach Neustart oder Flashen sichtbare
    // horizontale Graustufenbaender des vorherigen Bildes stehen lassen.
    M5.EPD.Clear(true);
    mainCanvas.pushCanvas(0, 0, UPDATE_MODE_GC16);
}

[[noreturn]] void sleepForMinutes(uint16_t minutes)
{
    const uint32_t seconds = static_cast<uint32_t>(minutes) * 60U;
    Serial.printf("RTC-Abschaltung fuer %u Minuten\n", minutes);
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    delay(100);
    M5.EPD.StandBy();

    // Im Akkubetrieb schaltet der M5Paper seine Hauptversorgung ab. Der
    // BM8563-RTC schaltet sie nach dem konfigurierten Intervall wieder ein.
    // Bei USB-Versorgung bleibt der ESP32 aktiv; dort übernimmt direkt danach
    // der interne Deep-Sleep-Timer als Fallback.
    M5.shutdown(seconds);
    delay(50);
    esp_sleep_enable_timer_wakeup(
        static_cast<uint64_t>(seconds) * 1000000ULL
    );
    esp_deep_sleep_start();
    while (true) delay(1000);
}
}

void setup()
{
    Serial.begin(115200);
    delay(250);

    // Touch, SD, Serial, Battery ADC und I2C.
    M5.begin(true, false, true, true, true);
    M5.EPD.SetRotation(90);
    M5.TP.SetRotation(90);
    M5.RTC.begin();
    if (!mainCanvas.createCanvas(SCREEN_WIDTH, SCREEN_HEIGHT))
    {
        Serial.println("Display-Canvas konnte nicht angelegt werden.");
        return;
    }

    WifiProvisioning provisioning(mainCanvas);
    const bool forceWifiSetup = wifiSetupRequestedAtBoot();

    if (!provisioning.connectOrConfigure(forceWifiSetup))
    {
        Serial.println("WLAN-Einrichtung wurde nicht abgeschlossen.");
        return;
    }

    ServerConnection serverConnection;
    ServerRegistration registration;
    if (!serverConnection.discoverAndRegister(registration))
    {
        showServerScreen(provisioning.connectedSsid(), registration);
        sleepForMinutes(5);
    }
    if (registration.status == "pending")
    {
        showServerScreen(provisioning.connectedSsid(), registration);
        sleepForMinutes(1);
    }

    ServerRenderConfiguration configuration;
    if (!serverConnection.fetchConfiguration(registration, configuration))
    {
        registration.error = configuration.error;
        showServerScreen(provisioning.connectedSsid(), registration);
        sleepForMinutes(5);
    }

    showPaperConfiguration(configuration);
    sleepForMinutes(configuration.sleepMinutes);
}

void loop()
{
    delay(1000);
}
