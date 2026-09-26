// Robot firmware state machine: connects to the AiService /robot WebSocket
// and executes commands from the server.
//
// Server -> robot:
//   text  {"type":"movement","axis":"left|right|up|down","degrees":N}
//         {"type":"movement","axis":"center"}
//         {"type":"emotion","name":"happy|angry|sad|doubt|sleepy|neutral"}
//   binary [type=1][codec=1][raw PCM int16 LE 16 kHz mono] — playback chunks;
//         an empty payload [type][codec] = end-of-stream marker.
// Robot -> server (text JSON):
//   {"type":"hb","timestamp":...}          (heartbeat)
//   {"type":"ack","command":"MOVE:...","timestamp":...}   (movement done)
//
// Audio is delivered as BINARY frames (no base64/JSON): the PCM is queued and
// played by a dedicated task with its own stack, so loopTask is never blocked
// by a large playRaw call. M5.Speaker.playRaw does NOT copy the data — the
// speaker task reads the samples directly from the chunk buffer, therefore a
// chunk is freed only after the speaker really consumed it
// (M5.Speaker.isPlaying(0)); freeing earlier causes crackling/stuttering.
#include "aibot/state_machine.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <deque>

#include <Arduino.h>
#include <M5Unified.h>
#include <WiFi.h>

#include "config/config.h"
#include "log/log.h"
#include "touch/touch.h"

namespace {

// ---------------------------------------------------------------------------
// Minimal JSON field extraction for our fixed protocol subset (no external
// library): finds "<key>": and returns the quoted string / integer value.
// ---------------------------------------------------------------------------
bool jsonFindString(const std::string& json, const char* key, std::string& out)
{
    const std::string pat = std::string("\"") + key + "\":";
    const size_t pos = json.find(pat);
    if (pos == std::string::npos)
    {
        return false;
    }
    size_t i = pos + pat.size();
    while (i < json.size() && (json[i] == ' ' || json[i] == '\t'))
    {
        ++i;
    }
    if (i >= json.size() || json[i] != '"')
    {
        return false;
    }
    const size_t start = ++i;
    while (i < json.size() && json[i] != '"')
    {
        if (json[i] == '\\' && i + 1 < json.size())
        {
            ++i;  // skip the escaped character
        }
        ++i;
    }
    if (i >= json.size())
    {
        return false;
    }
    out = json.substr(start, i - start);
    return true;
}

bool jsonFindInt(const std::string& json, const char* key, long& out)
{
    const std::string pat = std::string("\"") + key + "\":";
    const size_t pos = json.find(pat);
    if (pos == std::string::npos)
    {
        return false;
    }
    size_t i = pos + pat.size();
    while (i < json.size() && (json[i] == ' ' || json[i] == '\t'))
    {
        ++i;
    }
    long sign = 1;
    if (i < json.size() && json[i] == '-')
    {
        sign = -1;
        ++i;
    }
    if (i >= json.size() || json[i] < '0' || json[i] > '9')
    {
        return false;
    }
    long value = 0;
    while (i < json.size() && json[i] >= '0' && json[i] <= '9')
    {
        value = value * 10 + (json[i] - '0');
        ++i;
    }
    out = sign * value;
    return true;
}

Emotion emotionFromName(const std::string& name)
{
    if (name == "happy")
    {
        return Emotion::Happy;
    }
    if (name == "angry")
    {
        return Emotion::Angry;
    }
    if (name == "sad")
    {
        return Emotion::Sad;
    }
    if (name == "doubt")
    {
        return Emotion::Doubt;
    }
    if (name == "sleepy")
    {
        return Emotion::Sleepy;
    }
    return Emotion::Neutral;
}

const char* emotionName(Emotion e)
{
    switch (e)
    {
        case Emotion::Happy: return "Happy";
        case Emotion::Angry: return "Angry";
        case Emotion::Sad: return "Sad";
        case Emotion::Doubt: return "Doubt";
        case Emotion::Sleepy: return "Sleepy";
        case Emotion::Neutral: return "Neutral";
    }
    return "Unknown";
}

// Protocol value of a touch gesture (robot -> server {"type":"touch",
// "action":...}): press/release/swipe_forward/swipe_backward.
const char* touchActionName(TouchGesture gesture)
{
    switch (gesture)
    {
        case TouchGesture::Press:
            return "press";
        case TouchGesture::Release:
            return "release";
        case TouchGesture::SwipeForward:
            return "swipe_forward";
        case TouchGesture::SwipeBackward:
            return "swipe_backward";
        case TouchGesture::None:
        default:
            return "none";
    }
}

uint64_t nowUs()
{
    return static_cast<uint64_t>(micros());
}

// Applies the playback gain to int16 PCM in place (saturation to int16
// range). The buffer stays owned by its PlaybackMsg until the speaker
// actually consumed it — no extra copy, no use-after-free.
void applyPlaybackGain(std::vector<uint8_t>& pcm, float gain)
{
    const size_t n = pcm.size() / sizeof(int16_t);
    if (n == 0)
    {
        return;
    }
    int16_t* samples = reinterpret_cast<int16_t*>(pcm.data());
    for (size_t i = 0; i < n; ++i)
    {
        const long v = static_cast<long>(samples[i]) * gain;
        samples[i] = static_cast<int16_t>(
            std::max(-32768L, std::min(32767L, v)));
    }
}

}  // namespace

bool RobotStateMachine::begin()
{
    screen_.begin();
    movement_.begin();
    TouchSensor::begin();

    // Connect to the access point (blocking, up to WIFI_CONNECT_TIMEOUT_MS).
    if (!wifi_.connect(WIFI_SSID, WIFI_PASS, WIFI_CONNECT_TIMEOUT_MS))
    {
        LOG_E("[robot] Wi-Fi connect failed (check WIFI_SSID/WIFI_PASS)\n");
        return false;
    }
    LOG_I("[robot] Wi-Fi connected\n");

    ws_.setConnectedCallback([this]() { onWsConnected(); });
    ws_.setDisconnectedCallback([this](uint16_t code, const std::string& reason) {
        onWsDisconnected(code, reason);
    });
    ws_.setMessageCallback([this](const uint8_t* data, size_t size, bool binary) {
        onWsMessage(data, size, binary);
    });

    lastHbMs_ = millis();
    lastReconnectMs_ = 0;
    LOG_I("[robot] connecting to ws://%s:%u%s ...\n",
          WS_HOST, static_cast<unsigned>(WS_PORT), WS_PATH);
    return ws_.connect(WS_HOST, WS_PORT, WS_PATH);
}

void RobotStateMachine::loop()
{
    const uint32_t now = millis();

    // Wi-Fi link recovery.
    if (!wifi_.isConnected())
    {
        if (now - lastReconnectMs_ >= WIFI_RECONNECT_DELAY_MS)
        {
            lastReconnectMs_ = now;
            LOG_W("[robot] Wi-Fi lost, reconnecting...\n");
            wifi_.connect(WIFI_SSID, WIFI_PASS, WIFI_CONNECT_TIMEOUT_MS);
        }
        return;  // nothing to do without a link
    }

    // WebSocket reconnect (the connection is established asynchronously).
    if (!ws_.isConnected())
    {
        if (now - lastReconnectMs_ >= WIFI_RECONNECT_DELAY_MS)
        {
            lastReconnectMs_ = now;
            LOG_W("[robot] WS not connected, connecting to %s:%u%s ...\n",
                  WS_HOST, static_cast<unsigned>(WS_PORT), WS_PATH);
            ws_.connect(WS_HOST, WS_PORT, WS_PATH);
        }
    }
    else if (now - lastHbMs_ >= WS_HEARTBEAT_INTERVAL_MS)
    {
        // Heartbeat so the router/NAT does not drop the idle TCP session.
        lastHbMs_ = now;
        sendHeartbeat();
    }

    // Service the WebSocket network (events/data arrive here).
    ws_.loop();

    // Head touch sensor: report touches/swipes to the server (text JSON
    // {"type":"touch","action":"..."}, see sendTouchEvent).
    const TouchGesture gesture = TouchSensor::update();
    if (gesture != TouchGesture::None)
    {
        sendTouchEvent(gesture);
    }
}

void RobotStateMachine::onWsConnected()
{
    LOG_I("[robot] WS connected: %s:%u%s (local IP: %s)\n",
          WS_HOST, static_cast<unsigned>(WS_PORT), WS_PATH,
          WiFi.localIP().toString().c_str());
    lastHbMs_ = millis();
}

void RobotStateMachine::onWsDisconnected(uint16_t code, const std::string& reason)
{
    LOG_W("[robot] WS disconnected (%u): %s\n", code, reason.c_str());
    // Drop queued playback: after a reconnect the server starts a new stream,
    // stale chunks must not be played. The playback task itself survives.
    flushPlayback();
    sound_.stop();
}

void RobotStateMachine::onWsMessage(const uint8_t* data, size_t size, bool binary)
{
    if (binary)
    {
        onWsBinary(data, size);
        return;
    }
    handleCommand(std::string(reinterpret_cast<const char*>(data), size));
}

void RobotStateMachine::onWsBinary(const uint8_t* data, size_t size)
{
    // Binary audio frame: [0]=type, [1]=codec, [2..]=raw PCM (int16 LE).
    if (size < 2)
    {
        LOG_W("[robot] short binary frame (%d B)\n", static_cast<int>(size));
        return;
    }
    const uint8_t type = data[0];
    const uint8_t codec = data[1];
    if (type != kAudioFrameType || codec != kAudioCodecPcm)
    {
        LOG_W("[robot] unknown audio frame type=%u codec=%u\n",
              static_cast<unsigned>(type), static_cast<unsigned>(codec));
        return;
    }
    enqueuePlayback(data + 2, size - 2);
}

void RobotStateMachine::handleCommand(const std::string& text)
{
    std::string type;
    if (!jsonFindString(text, "type", type))
    {
        LOG_W("[robot] non-JSON text: %s\n", text.c_str());
        return;
    }
    if (type == "movement")
    {
        std::string axis;
        long degrees = 0;
        jsonFindString(text, "axis", axis);
        jsonFindInt(text, "degrees", degrees);
        handleMovement(axis, static_cast<int>(degrees));
        return;
    }
    if (type == "emotion")
    {
        std::string name;
        if (jsonFindString(text, "name", name))
        {
            handleEmotion(name);
        }
        return;
    }
    LOG_D("[robot] unknown command type: %s\n", type.c_str());
}

void RobotStateMachine::handleMovement(const std::string& axis, int degrees)
{
    LOG_I("[robot] movement: %s %d\n", axis.c_str(), degrees);
    if (axis == "left")
    {
        movement_.turnLeft(degrees);
    }
    else if (axis == "right")
    {
        movement_.turnRight(degrees);
    }
    else if (axis == "up")
    {
        movement_.turnUp(degrees);
    }
    else if (axis == "down")
    {
        movement_.turnDown(degrees);
    }
    else if (axis == "center")
    {
        movement_.center();
    }
    else
    {
        LOG_W("[robot] movement: unknown axis %s\n", axis.c_str());
        return;
    }

    char cmd[64];
    if (axis == "center")
    {
        snprintf(cmd, sizeof(cmd), "MOVE:center");
    }
    else
    {
        snprintf(cmd, sizeof(cmd), "MOVE:%s:%d", axis.c_str(), degrees);
    }
    sendAck(cmd);
}

void RobotStateMachine::handleEmotion(const std::string& name)
{
    const Emotion e = emotionFromName(name);
    LOG_I("[robot] emotion: '%s' -> %s\n", name.c_str(), emotionName(e));
    screen_.setEmotion(e);
}

// ---------------------------------------------------------------------------
// Playback: a dedicated task drains the queue, one playSample per chunk.
// ---------------------------------------------------------------------------

void RobotStateMachine::enqueuePlayback(const uint8_t* data, size_t size)
{
    QueueHandle_t q = static_cast<QueueHandle_t>(playQueue_);
    if (q == nullptr)
    {
        // 4 slots are enough: the server sends frames with real-time pacing,
        // the speaker plays them at the same rate.
        q = xQueueCreate(4, sizeof(PlaybackMsg*));
        playQueue_ = static_cast<void*>(q);
        if (q != nullptr)
        {
            // 64 KB stack: playRaw + DMA I2S output for long chunks.
            // Priority 2 as M5Unified recommends, to avoid speaker noise.
            // Arduino-ESP32 core v3 (ESP-IDF 5) uses xTaskCreateUniversal;
            // older cores still have xTaskCreatePinnedToCore.
#if ESP_IDF_VERSION_MAJOR >= 5
            xTaskCreateUniversal(&RobotStateMachine::playbackTaskEntry,
                                 "playpcm", 65536, this, 2, nullptr, 0);
#else
            xTaskCreatePinnedToCore(&RobotStateMachine::playbackTaskEntry,
                                    "playpcm", 65536, this, 2, nullptr, 0);
#endif
        }
    }
    if (q == nullptr)
    {
        return;
    }
    auto* msg = new PlaybackMsg();
    msg->data.assign(data, data + size);
    if (xQueueSend(q, &msg, 0) != pdTRUE)
    {
        delete msg;  // queue full — skip (do not accumulate)
    }
}

void RobotStateMachine::flushPlayback()
{
    QueueHandle_t q = static_cast<QueueHandle_t>(playQueue_);
    if (q == nullptr)
    {
        return;
    }
    PlaybackMsg* msg = nullptr;
    while (xQueueReceive(q, &msg, 0) == pdTRUE)
    {
        delete msg;
    }
}

void RobotStateMachine::playbackTaskEntry(void* arg)
{
    static_cast<RobotStateMachine*>(arg)->playbackLoop();
}

void RobotStateMachine::waitSpeakerIdle()
{
    for (int i = 0; i < 200 && M5.Speaker.isPlaying(); ++i)
    {
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

void RobotStateMachine::playbackLoop()
{
    // Chunks published to the speaker (in order). playRaw reads the buffers
    // directly while playing, so each chunk is freed only after the speaker
    // actually consumed it (isPlaying(0) = number of chunks still held).
    std::deque<PlaybackMsg*> inflight;

    while (true)
    {
        PlaybackMsg* msg = nullptr;
        const QueueHandle_t q = static_cast<QueueHandle_t>(playQueue_);
        // Wait for the next chunk; a no-data timeout closes the session.
        if (q == nullptr || xQueueReceive(q, &msg,
                         pdMS_TO_TICKS(PLAYBACK_IDLE_TIMEOUT_MS)) != pdTRUE)
        {
            waitSpeakerIdle();
            for (auto* m : inflight)
            {
                delete m;
            }
            inflight.clear();
            LOG_W("[robot] playback idle timeout\n");
            continue;
        }
        if (msg == nullptr)
        {
            continue;
        }

        const uint8_t* data = msg->data.data();
        const size_t size = msg->data.size();
        if (size >= sizeof(int16_t))
        {
            // Make sure the speaker is initialized before playing.
            if (!sound_.ensureReady())
            {
                LOG_W("[robot] speaker not ready, chunk skipped\n");
                delete msg;
                continue;
            }
            // Amplify the quiet TTS PCM (config PLAYBACK_GAIN); in-place, so
            // the buffer stays alive until the speaker read it (isPlaying(0)).
            if (PLAYBACK_GAIN > 0.0f && PLAYBACK_GAIN != 1.0f)
            {
                applyPlaybackGain(msg->data, PLAYBACK_GAIN);
            }
            const int16_t* samples =
                reinterpret_cast<const int16_t*>(msg->data.data());
            const size_t count = size / sizeof(int16_t);
            sound_.playSample(samples, count);
            LOG_D("[robot] played pcm chunk: %u samples (%.2f s)\n",
                  static_cast<unsigned>(count),
                  static_cast<double>(count) / MIC_SAMPLE_RATE);

            inflight.push_back(msg);
            // How many chunks the speaker currently really reads/holds.
            const size_t flying = M5.Speaker.isPlaying(0);
            // Free all but the freshest few (at least one — the just
            // published one); the rest have been read completely.
            const size_t keep = std::max<size_t>(flying, 1u);
            while (inflight.size() > keep)
            {
                delete inflight.front();
                inflight.pop_front();
            }
        }
        else
        {
            // Empty payload — the server's end-of-playback marker. Wait until
            // the speaker finishes its tail, then close the session.
            waitSpeakerIdle();
            for (auto* m : inflight)
            {
                delete m;
            }
            inflight.clear();
            delete msg;
            LOG_I("[robot] playback done (eof)\n");
        }
    }
}

void RobotStateMachine::sendHeartbeat()
{
    // The robot reports its own IP in the heartbeat, so the server can show
    // it in logs (the WS peer address may be empty in some uvicorn versions).
    char msg[160];
    const String ip = WiFi.localIP().toString();
    snprintf(msg, sizeof(msg),
             "{\"type\":\"hb\",\"ip\":\"%s\",\"timestamp\":%llu}",
             ip.c_str(), static_cast<unsigned long long>(nowUs()));
    const bool ok = ws_.sendText(msg);
    LOG_D("[robot] hb sent (ip=%s) -> %s\n",
          ip.c_str(), ok ? "ok" : "no connection");
}

void RobotStateMachine::sendAck(const std::string& command)
{
    char msg[160];
    snprintf(msg, sizeof(msg),
             "{\"type\":\"ack\",\"command\":\"%s\",\"timestamp\":%llu}",
             command.c_str(), static_cast<unsigned long long>(nowUs()));
    ws_.sendText(msg);
    LOG_D("[robot] ack: %s\n", command.c_str());
}

void RobotStateMachine::sendTouchEvent(TouchGesture gesture)
{
    ++touchEvents_;
    char msg[96];
    snprintf(msg, sizeof(msg),
             "{\"type\":\"touch\",\"action\":\"%s\",\"timestamp\":%llu}",
             touchActionName(gesture),
             static_cast<unsigned long long>(nowUs()));
    const bool ok = ws_.sendText(msg);
    LOG_I("[robot] touch #%u: %s -> %s\n",
          static_cast<unsigned>(touchEvents_), touchActionName(gesture),
          ok ? "ok" : "no connection");
}