#include "WifiProvisioning.h"

#include <WiFi.h>
#include <ctype.h>

namespace
{
constexpr int SCREEN_WIDTH = 540;
constexpr int SCREEN_HEIGHT = 960;
constexpr int CONTENT_LEFT = 28;
constexpr int CONTENT_RIGHT = 512;

constexpr int PASSWORD_FIELD_X = 28;
constexpr int PASSWORD_FIELD_Y = 158;
constexpr int PASSWORD_FIELD_W = 484;
constexpr int PASSWORD_FIELD_H = 64;

constexpr int KEY_HEIGHT = 64;
constexpr int KEY_GAP = 4;
constexpr int KEY_ROW_1_Y = 300;
constexpr int KEY_ROW_2_Y = 374;
constexpr int KEY_ROW_3_Y = 448;
constexpr int CONTROL_ROW_Y = 540;
constexpr int ACTION_ROW_Y = 640;
}

WifiProvisioning::WifiProvisioning(M5EPD_Canvas& targetCanvas)
    : canvas(targetCanvas),
      fieldCanvas(&M5.EPD)
{
}

const String& WifiProvisioning::connectedSsid() const
{
    return activeSsid;
}

bool WifiProvisioning::connectOrConfigure(bool forceSetup)
{
    String savedSsid;
    String savedPassword;

    if (!forceSetup && loadCredentials(savedSsid, savedPassword))
    {
        if (connectTo(savedSsid, savedPassword, false))
        {
            return true;
        }

        drawMessage(
            "WLAN nicht erreichbar",
            "Gespeichertes Netzwerk wurde nicht gefunden.",
            "Tippen, um ein Netzwerk auszuwaehlen."
        );
        waitForTap();
    }

    return runInteractiveSetup();
}

bool WifiProvisioning::loadCredentials(String& ssid, String& password)
{
    if (!preferences.begin("shb-paper", true))
    {
        return false;
    }

    ssid = preferences.getString("wifi-ssid", "");
    password = preferences.getString("wifi-pass", "");
    preferences.end();

    return !ssid.isEmpty();
}

void WifiProvisioning::saveCredentials(const String& ssid, const String& password)
{
    if (!preferences.begin("shb-paper", false))
    {
        return;
    }

    preferences.putString("wifi-ssid", ssid);
    preferences.putString("wifi-pass", password);
    preferences.end();
}

bool WifiProvisioning::connectTo(
    const String& ssid,
    const String& password,
    bool showFailure
)
{
    // Bei der automatischen Verbindung bleibt das vorhandene E-Paper-Bild
    // sichtbar. Nur der interaktive Einrichtungsablauf zeigt einen Hinweis.
    if (showFailure)
    {
        drawMessage(
            "WLAN wird verbunden",
            ssid,
            "Bitte warten ..."
        );
    }

    WiFi.mode(WIFI_STA);
    WiFi.disconnect(false, false);
    delay(150);
    WiFi.begin(ssid.c_str(), password.c_str());

    const uint32_t startedAt = millis();

    while (WiFi.status() != WL_CONNECTED && millis() - startedAt < 18000)
    {
        delay(200);
    }

    if (WiFi.status() == WL_CONNECTED)
    {
        activeSsid = ssid;
        Serial.printf("WLAN verbunden: %s, IP %s, RSSI %d dBm\n",
                      ssid.c_str(),
                      WiFi.localIP().toString().c_str(),
                      WiFi.RSSI());
        return true;
    }

    WiFi.disconnect(false, false);

    if (showFailure)
    {
        drawMessage(
            "Verbindung fehlgeschlagen",
            "Passwort und Empfang pruefen.",
            "Tippen, um es erneut zu versuchen."
        );
        waitForTap();
    }

    return false;
}

bool WifiProvisioning::runInteractiveSetup()
{
    while (true)
    {
        if (!scanNetworks())
        {
            drawMessage(
                "Keine WLAN-Netze gefunden",
                "Pruefe den Router und den Empfang.",
                "Tippen zum erneuten Suchen."
            );
            waitForTap();
            continue;
        }

        const int selectedIndex = chooseNetwork();

        if (selectedIndex < 0)
        {
            continue;
        }

        String password;
        const NetworkEntry& selected = networks[selectedIndex];

        if (selected.secured && !enterPassword(selected, password))
        {
            continue;
        }

        if (connectTo(selected.ssid, password, true))
        {
            saveCredentials(selected.ssid, password);
            return true;
        }
    }
}

bool WifiProvisioning::scanNetworks()
{
    drawMessage(
        "WLAN-Netze werden gesucht",
        "Der Scan kann einige Sekunden dauern.",
        ""
    );

    WiFi.mode(WIFI_STA);
    WiFi.disconnect(false, false);
    WiFi.scanDelete();
    delay(150);

    const int found = WiFi.scanNetworks(false, true);
    networkCount = 0;

    for (int index = 0; index < found && networkCount < MAX_NETWORKS; ++index)
    {
        String ssid = WiFi.SSID(index);

        if (ssid.isEmpty())
        {
            continue;
        }

        bool duplicate = false;
        for (uint8_t existing = 0; existing < networkCount; ++existing)
        {
            if (networks[existing].ssid == ssid)
            {
                duplicate = true;
                if (WiFi.RSSI(index) > networks[existing].rssi)
                {
                    networks[existing].rssi = WiFi.RSSI(index);
                }
                break;
            }
        }

        if (duplicate)
        {
            continue;
        }

        networks[networkCount].ssid = ssid;
        networks[networkCount].rssi = WiFi.RSSI(index);
        networks[networkCount].secured = WiFi.encryptionType(index) != WIFI_AUTH_OPEN;
        ++networkCount;
    }

    WiFi.scanDelete();

    for (uint8_t left = 0; left < networkCount; ++left)
    {
        for (uint8_t right = left + 1; right < networkCount; ++right)
        {
            if (networks[right].rssi > networks[left].rssi)
            {
                NetworkEntry temporary = networks[left];
                networks[left] = networks[right];
                networks[right] = temporary;
            }
        }
    }

    return networkCount > 0;
}

int WifiProvisioning::chooseNetwork()
{
    uint8_t page = 0;
    const uint8_t pageCount =
        (networkCount + NETWORKS_PER_PAGE - 1) /
        NETWORKS_PER_PAGE;

    while (true)
    {
        drawNetworkPage(page);
        TouchPoint tap = waitForTap();

        const int firstRowY = 126;
        const int rowHeight = 76;

        if (tap.x >= CONTENT_LEFT && tap.x <= CONTENT_RIGHT && tap.y >= firstRowY)
        {
            int row = (tap.y - firstRowY) / rowHeight;
            if (row >= 0 && row < NETWORKS_PER_PAGE)
            {
                int index = page * NETWORKS_PER_PAGE + row;
                if (index < networkCount)
                {
                    return index;
                }
            }
        }

        if (contains(tap, 28, 820, 140, 70) && page > 0)
        {
            --page;
        }
        else if (contains(tap, 200, 820, 140, 70))
        {
            return -1;
        }
        else if (contains(tap, 372, 820, 140, 70) && page + 1 < pageCount)
        {
            ++page;
        }
    }
}

void WifiProvisioning::drawNetworkPage(uint8_t page)
{
    canvas.fillCanvas(0);
    canvas.setTextColor(15);
    canvas.setFreeFont(&FreeSansBold18pt7b);
    canvas.drawString("WLAN auswaehlen", CONTENT_LEFT, 34);

    canvas.setFreeFont(&FreeSans9pt7b);
    canvas.drawString("Tippe auf das Netzwerk, das dieses Display verwenden soll.", CONTENT_LEFT, 86);
    canvas.drawLine(CONTENT_LEFT, 112, CONTENT_RIGHT, 112, 10);

    const int firstRowY = 126;
    const int rowHeight = 76;
    const int firstIndex = page * NETWORKS_PER_PAGE;

    for (uint8_t row = 0; row < NETWORKS_PER_PAGE; ++row)
    {
        int index = firstIndex + row;
        if (index >= networkCount)
        {
            break;
        }

        int y = firstRowY + row * rowHeight;
        canvas.drawRoundRect(CONTENT_LEFT, y, 484, 64, 10, 10);
        canvas.setFreeFont(&FreeSans12pt7b);

        String label = networks[index].secured ? "* " : "  ";
        label += networks[index].ssid;
        if (label.length() > 28)
        {
            label = label.substring(0, 27) + "...";
        }
        canvas.drawString(label, 44, y + 14);

        canvas.setFreeFont(&FreeSans9pt7b);
        String signal = signalText(networks[index].rssi);
        int signalWidth = canvas.textWidth(signal);
        canvas.drawString(signal, CONTENT_RIGHT - signalWidth - 16, y + 19);
    }

    drawButton(28, 820, 140, 70, page > 0 ? "Zurueck" : "-");
    drawButton(200, 820, 140, 70, "Neu suchen");
    drawButton(372, 820, 140, 70,
               firstIndex + NETWORKS_PER_PAGE < networkCount ? "Weiter" : "-");

    canvas.pushCanvas(0, 0, UPDATE_MODE_GC16);
}

bool WifiProvisioning::enterPassword(
    const NetworkEntry& network,
    String& password
)
{
    KeyboardMode mode = KeyboardMode::lower;
    drawKeyboard(network.ssid, password, mode);

    while (true)
    {
        TouchPoint tap = waitForTap();

        const char* rows[3];
        if (mode == KeyboardMode::symbols)
        {
            rows[0] = "1234567890";
            rows[1] = "!@#$%&*()-";
            rows[2] = "._+=?/\\:;";
        }
        else
        {
            rows[0] = "QWERTZUIOP";
            rows[1] = "ASDFGHJKL";
            rows[2] = "YXCVBNM";
        }

        const int rowY[3] = {KEY_ROW_1_Y, KEY_ROW_2_Y, KEY_ROW_3_Y};
        const int rowX[3] = {10, 35, 85};
        const int keyWidth = 48;
        bool keyHandled = false;

        for (int row = 0; row < 3 && !keyHandled; ++row)
        {
            int length = strlen(rows[row]);
            for (int column = 0; column < length; ++column)
            {
                int x = rowX[row] + column * (keyWidth + KEY_GAP);
                if (!contains(tap, x, rowY[row], keyWidth, KEY_HEIGHT))
                {
                    continue;
                }

                if (password.length() < 63)
                {
                    char character = rows[row][column];
                    if (mode == KeyboardMode::lower)
                    {
                        character = static_cast<char>(tolower(character));
                    }
                    password += character;
                    updatePasswordField(password);
                }
                keyHandled = true;
                break;
            }
        }

        if (keyHandled)
        {
            continue;
        }

        if (contains(tap, 20, CONTROL_ROW_Y, 100, 66))
        {
            if (mode == KeyboardMode::lower)
                mode = KeyboardMode::upper;
            else if (mode == KeyboardMode::upper)
                mode = KeyboardMode::symbols;
            else
                mode = KeyboardMode::lower;

            drawKeyboard(network.ssid, password, mode);
        }
        else if (contains(tap, 130, CONTROL_ROW_Y, 230, 66))
        {
            if (password.length() < 63)
            {
                password += ' ';
                updatePasswordField(password);
            }
        }
        else if (contains(tap, 370, CONTROL_ROW_Y, 150, 66))
        {
            if (!password.isEmpty())
            {
                password.remove(password.length() - 1);
                updatePasswordField(password);
            }
        }
        else if (contains(tap, 28, ACTION_ROW_Y, 220, 76))
        {
            return false;
        }
        else if (contains(tap, 292, ACTION_ROW_Y, 220, 76))
        {
            return true;
        }
    }
}

void WifiProvisioning::drawKeyboard(
    const String& ssid,
    const String& password,
    KeyboardMode mode
)
{
    canvas.fillCanvas(0);
    canvas.setTextColor(15);
    canvas.setFreeFont(&FreeSansBold18pt7b);
    canvas.drawString("WLAN-Passwort", CONTENT_LEFT, 28);

    canvas.setFreeFont(&FreeSans12pt7b);
    String networkText = "Netzwerk: " + ssid;
    if (networkText.length() > 34)
    {
        networkText = networkText.substring(0, 33) + "...";
    }
    canvas.drawString(networkText, CONTENT_LEFT, 92);

    canvas.drawRoundRect(
        PASSWORD_FIELD_X,
        PASSWORD_FIELD_Y,
        PASSWORD_FIELD_W,
        PASSWORD_FIELD_H,
        10,
        10
    );

    const char* rows[3];
    if (mode == KeyboardMode::symbols)
    {
        rows[0] = "1234567890";
        rows[1] = "!@#$%&*()-";
        rows[2] = "._+=?/\\:;";
    }
    else
    {
        rows[0] = "QWERTZUIOP";
        rows[1] = "ASDFGHJKL";
        rows[2] = "YXCVBNM";
    }

    const int rowY[3] = {KEY_ROW_1_Y, KEY_ROW_2_Y, KEY_ROW_3_Y};
    const int rowX[3] = {10, 35, 85};
    const int keyWidth = 48;

    for (int row = 0; row < 3; ++row)
    {
        int length = strlen(rows[row]);
        for (int column = 0; column < length; ++column)
        {
            char character = rows[row][column];
            if (mode == KeyboardMode::lower)
            {
                character = static_cast<char>(tolower(character));
            }
            String label(character);
            int x = rowX[row] + column * (keyWidth + KEY_GAP);
            drawKey(x, rowY[row], keyWidth, KEY_HEIGHT, label);
        }
    }

    String modeLabel = mode == KeyboardMode::lower
        ? "ABC"
        : (mode == KeyboardMode::upper ? "123" : "abc");

    drawButton(20, CONTROL_ROW_Y, 100, 66, modeLabel);
    drawButton(130, CONTROL_ROW_Y, 230, 66, "Leerzeichen");
    drawButton(370, CONTROL_ROW_Y, 150, 66, "Loeschen");
    drawButton(28, ACTION_ROW_Y, 220, 76, "Abbrechen");
    drawButton(292, ACTION_ROW_Y, 220, 76, "Verbinden", true);

    canvas.setFreeFont(&FreeSans9pt7b);
    canvas.drawString(
        "Passwort pruefen und danach Verbinden tippen.",
        CONTENT_LEFT,
        760
    );

    canvas.pushCanvas(0, 0, UPDATE_MODE_GC16);
    updatePasswordField(password);
}

void WifiProvisioning::updatePasswordField(const String& password)
{
    fieldCanvas.deleteCanvas();
    if (!fieldCanvas.createCanvas(PASSWORD_FIELD_W - 8, PASSWORD_FIELD_H - 8))
    {
        return;
    }

    fieldCanvas.fillCanvas(0);
    fieldCanvas.setTextColor(15);
    fieldCanvas.setFreeFont(&FreeSansBold18pt7b);

    String visiblePassword = password;
    const int availableWidth = PASSWORD_FIELD_W - 28;
    while (!visiblePassword.isEmpty() && fieldCanvas.textWidth(visiblePassword) > availableWidth)
    {
        visiblePassword.remove(0, 1);
    }

    fieldCanvas.drawString(visiblePassword, 10, 7);
    fieldCanvas.pushCanvas(
        PASSWORD_FIELD_X + 4,
        PASSWORD_FIELD_Y + 4,
        UPDATE_MODE_DU
    );
}

void WifiProvisioning::drawMessage(
    const String& title,
    const String& detail,
    const String& footer
)
{
    canvas.fillCanvas(0);
    canvas.setTextColor(15);
    canvas.setFreeFont(&FreeSansBold18pt7b);
    canvas.drawCentreString(title, SCREEN_WIDTH / 2, 270, 1);

    canvas.setFreeFont(&FreeSans12pt7b);
    canvas.drawCentreString(detail, SCREEN_WIDTH / 2, 350, 1);

    if (!footer.isEmpty())
    {
        canvas.setFreeFont(&FreeSans9pt7b);
        canvas.drawCentreString(footer, SCREEN_WIDTH / 2, 430, 1);
    }

    canvas.pushCanvas(0, 0, UPDATE_MODE_GC16);
}

void WifiProvisioning::drawButton(
    int x,
    int y,
    int w,
    int h,
    const String& label,
    bool emphasized
)
{
    if (emphasized)
    {
        canvas.fillRoundRect(x, y, w, h, 10, 15);
        canvas.setTextColor(0);
    }
    else
    {
        canvas.drawRoundRect(x, y, w, h, 10, 10);
        canvas.setTextColor(15);
    }

    canvas.setFreeFont(&FreeSans12pt7b);
    int textWidth = canvas.textWidth(label);
    canvas.drawString(label, x + (w - textWidth) / 2, y + 18);
    canvas.setTextColor(15);
}

void WifiProvisioning::drawKey(
    int x,
    int y,
    int w,
    int h,
    const String& label
)
{
    canvas.fillRoundRect(x, y, w, h, 8, 15);
    canvas.setTextColor(0);
    canvas.setFreeFont(&FreeSansBold18pt7b);
    int textWidth = canvas.textWidth(label);
    canvas.drawString(label, x + (w - textWidth) / 2, y + 12);
    canvas.setTextColor(15);
}

WifiProvisioning::TouchPoint WifiProvisioning::waitForTap()
{
    while (true)
    {
        if (M5.TP.available())
        {
            M5.TP.update();

            if (!M5.TP.isFingerUp() && M5.TP.getFingerNum() > 0)
            {
                tp_finger_t finger = M5.TP.readFinger(0);
                TouchPoint result{
                    static_cast<int16_t>(finger.x),
                    static_cast<int16_t>(finger.y)
                };
                waitForRelease();
                return result;
            }
        }

        delay(15);
    }
}

void WifiProvisioning::waitForRelease()
{
    const uint32_t startedAt = millis();
    while (millis() - startedAt < 1500)
    {
        if (M5.TP.available())
        {
            M5.TP.update();
            if (M5.TP.isFingerUp())
            {
                break;
            }
        }
        delay(15);
    }
    M5.TP.flush();
    delay(80);
}

bool WifiProvisioning::contains(
    const TouchPoint& point,
    int x,
    int y,
    int w,
    int h
)
{
    return point.x >= x &&
           point.x < x + w &&
           point.y >= y &&
           point.y < y + h;
}

String WifiProvisioning::signalText(int32_t rssi)
{
    if (rssi >= -55) return "sehr gut";
    if (rssi >= -67) return "gut";
    if (rssi >= -75) return "mittel";
    return "schwach";
}
