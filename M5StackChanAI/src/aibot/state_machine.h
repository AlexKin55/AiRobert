#ifndef AIBOT_STATE_MACHINE_H_
#define AIBOT_STATE_MACHINE_H_

#include <atomic>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "move/move.h"
#include "screen/screen.h"
#include "sound/sound.h"
#include "touch/touch.h"
#include "websocket/websocket.h"
#include "wifi/wifi.h"

// Binary audio frame (server -> robot): byte[0] = type, byte[1] = codec,
// then raw PCM payload. Codec 1 = raw PCM int16 LE, 16 kHz mono. An empty
// payload [type][codec] is the end-of-playback marker.
static constexpr uint8_t kAudioFrameType = 1;
static constexpr uint8_t kAudioCodecPcm = 1;

// Robot application state machine: connects to the AiService /robot WebSocket
// and executes commands from the server.
//
// Server -> robot:
//   text  {"type":"movement","axis":"left|right|up|down","degrees":N}
//         {"type":"movement","axis":"center"}
//         {"type":"emotion","name":"happy|angry|sad|doubt|sleepy|neutral"}
//   binary [kAudioFrameType][kAudioCodecPcm][raw PCM] — playback chunks;
//         an empty payload = end of stream (see protocol.robot_audio_frame).
// Robot -> server (text JSON):
//   {"type":"hb","timestamp":...}
//   {"type":"ack","command":"MOVE:left:60","timestamp":...}
//
// The microphone is DISABLED — audio comes only from the camera through the
// server (the robot only plays it back). Incoming PCM frames are queued and
// played by a dedicated task: playRaw reads directly from the frame buffer,
// so buffers are freed only after the speaker actually consumed them
// (M5.Speaker.isPlaying(0)) — otherwise the playback crackles/stutters.
// loop() must be called regularly.
class RobotStateMachine
{
    public:
    // Initializes the screen/movement/speaker, connects to Wi-Fi and starts
    // the WebSocket connection (async). Returns false if Wi-Fi failed.
    bool begin();

    // Services the WebSocket and the reconnect/heartbeat timers.
    void loop();

    private:
    struct PlaybackMsg
    {
        std::vector<uint8_t> data;  // raw PCM bytes (int16 LE)
    };

    void onWsConnected();
    void onWsDisconnected(uint16_t code, const std::string& reason);
    void onWsMessage(const uint8_t* data, size_t size, bool binary);
    void onWsBinary(const uint8_t* data, size_t size);

    void handleCommand(const std::string& text);
    void handleMovement(int pan, int tilt);
    void handleEmotion(const std::string& name);

    // Queues an incoming PCM chunk for playback (dropped when full).
    void enqueuePlayback(const uint8_t* data, size_t size);
    // Drains and frees all queued playback chunks (called on disconnect so
    // stale audio is not played after a reconnect).
    void flushPlayback();
    // Dedicated playback task body (see playbackTaskEntry).
    void playbackLoop();
    // Waits until the speaker finishes its DMA tail (max ~2 s).
    static void waitSpeakerIdle();

    static void playbackTaskEntry(void* arg);

    void sendHeartbeat();
    void sendAck(const std::string& command);
    // Sends a head-touch event {"type":"touch","action":...} to the server.
    void sendTouchEvent(TouchGesture gesture);

    EspWifiManager wifi_;
    EspWebsocketClient ws_;
    EspScreen screen_;
    EspMovement movement_;
    EspSound sound_;

    // Playback queue (FreeRTOS, opaque: cast to QueueHandle_t in the .cpp
    // where FreeRTOS headers are already loaded via Arduino.h). Fed by binary
    // audio frames, drained by the playback task. A session runs from the
    // first chunk until the empty (EOF) frame or the queue timeout
    // (PLAYBACK_IDLE_TIMEOUT_MS).
    void* playQueue_ = nullptr;

    // Millis of the last heartbeat / reconnect attempt.
    uint32_t lastHbMs_ = 0;
    uint32_t lastReconnectMs_ = 0;
    // Total head-touch events reported to the server.
    uint32_t touchEvents_ = 0;
};

#endif  // AIBOT_STATE_MACHINE_H_