#ifndef WIFI_H_
#define WIFI_H_

#include <cstdint>

// Wi-Fi management for ESP32 based on Arduino <WiFi.h>.
class EspWifiManager
{
    public:
    // Connects to an access point. Blocks until the link is established or
    // timeoutMs elapses. Returns true on success.
    bool connect(const char* ssid, const char* pass, uint32_t timeoutMs);

    // Current connection state.
    bool isConnected() const;
};

#endif  // WIFI_H_