#include "wifi/wifi.h"

#include <WiFi.h>

bool EspWifiManager::connect(const char* ssid, const char* pass, uint32_t timeoutMs)
{
    // Already connected — do not block or reset the link on a repeat call.
    if (WiFi.status() == WL_CONNECTED)
        return true;

    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid, pass);

    const unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && (millis() - start) < timeoutMs)
    {
        delay(200);
    }

    return WiFi.status() == WL_CONNECTED;
}

bool EspWifiManager::isConnected() const
{
    return WiFi.status() == WL_CONNECTED;
}