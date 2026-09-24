// AiBot robot firmware entry point (ESP32, M5Stack CoreS3).
//
// Boots the robot state machine: screen (avatar), movements (pan-tilt),
// speaker, Wi-Fi and the WebSocket connection to the AiService /robot
// endpoint. All commands (audio playback, movements, emotions) arrive over
// the WebSocket as text JSON (see aibot/state_machine.h).
#include <Arduino.h>
#include <WiFi.h>

#include "aibot/state_machine.h"
#include "log/log.h"

RobotStateMachine g_robot;

void setup()
{
    Serial.begin(115200);
    delay(300);

    // Do not let the Wi-Fi stack enter power save: with Wi-Fi sleep enabled
    // the ESP32 periodically drops the TCP/WebSocket link (the server sees
    // "robot offline" exactly when the answer is ready).
    WiFi.setSleep(false);

    LOG_I("[main] AiBot robot firmware starting\n");
    if (!g_robot.begin())
    {
        LOG_E("[main] initialization failed (check Wi-Fi / WS settings in "
              "config/config.h)\n");
    }
}

void loop()
{
    g_robot.loop();
    delay(10);
}