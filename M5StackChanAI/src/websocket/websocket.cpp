#include "websocket/websocket.h"

#include "log/log.h"

EspWebsocketClient::EspWebsocketClient()
{
    // All library events are forwarded to handleEvent(), which invokes the
    // user callbacks.
    m_ws.onEvent([this](WStype_t type, uint8_t* payload, size_t length) {
        handleEvent(type, payload, length);
    });
}

EspWebsocketClient::~EspWebsocketClient() = default;

bool EspWebsocketClient::connect(const char* host, uint16_t port, const char* path)
{
    m_ws.begin(host, port, path);
    return true;
}

bool EspWebsocketClient::isConnected()
{
    std::lock_guard<std::recursive_mutex> lock(m_wsMutex);
    return m_ws.isConnected();
}

void EspWebsocketClient::disconnect()
{
    std::lock_guard<std::recursive_mutex> lock(m_wsMutex);
    m_ws.disconnect();
}

bool EspWebsocketClient::sendText(const std::string& text)
{
    // Serialize all WebSocketsClient operations coming from different tasks
    // (the I2S mic sends PCM, loopTask sends text/ACKs and receives data):
    // the library is not thread-safe.
    std::lock_guard<std::recursive_mutex> lock(m_wsMutex);
    return m_ws.sendTXT(text.c_str());
}

bool EspWebsocketClient::sendBinary(const uint8_t* data, size_t size)
{
    std::lock_guard<std::recursive_mutex> lock(m_wsMutex);
    return m_ws.sendBIN(data, size);
}

void EspWebsocketClient::loop()
{
    // Receiving is also under the mutex: while the I2S task sends a PCM
    // chunk, loopTask waits for that send to finish, and vice versa. The
    // recursive mutex lets callbacks (invoked inside m_ws.loop()) send
    // ACK/replies without a deadlock.
    std::lock_guard<std::recursive_mutex> lock(m_wsMutex);
    m_ws.loop();
}

void EspWebsocketClient::setConnectedCallback(ConnectedCallback cb)
{
    m_onConnected = std::move(cb);
}

void EspWebsocketClient::setDisconnectedCallback(DisconnectedCallback cb)
{
    m_onDisconnected = std::move(cb);
}

void EspWebsocketClient::setMessageCallback(MessageCallback cb)
{
    m_onMessage = std::move(cb);
}

// Leveled logging for link diagnostics (LOG_LEVEL in config/config.h):
//   LOG_D — frames/ping/pong (frequent), LOG_I — connection events,
//   LOG_W — drops and library errors, LOG_E — unrecoverable failures.
void EspWebsocketClient::handleEvent(WStype_t type, uint8_t* payload, size_t length)
{
    switch (type)
    {
        case WStype_CONNECTED:
            LOG_I("[ws] connected\n");
            if (m_onConnected) m_onConnected();
            break;

        case WStype_DISCONNECTED:
        {
            const std::string reason =
                payload ? reinterpret_cast<const char*>(payload) : "";
            LOG_W("[ws] disconnected: %s\n", reason.c_str());
            if (m_onDisconnected) m_onDisconnected(0, reason);
            break;
        }

        case WStype_ERROR:
            // Library-level error: payload contains a short reason (e.g.
            // "error: no data, timeout" or "disconnected").
            LOG_W("[ws] error: %s\n",
                  payload ? reinterpret_cast<const char*>(payload) : "");
            break;

        case WStype_PING:
            LOG_D("[ws] ping received (%d B)\n", static_cast<int>(length));
            break;

        case WStype_PONG:
            LOG_D("[ws] pong received (%d B)\n", static_cast<int>(length));
            break;

        case WStype_TEXT:
            LOG_D("[ws] text frame: %d B\n", static_cast<int>(length));
            if (m_onMessage)
            {
                size_t n = length;
                // Some library versions include the trailing NUL in length.
                if (n > 0 && payload[n - 1] == 0) --n;
                m_onMessage(payload, n, false);
            }
            break;

        case WStype_BIN:
            LOG_D("[ws] binary frame: %d B\n", static_cast<int>(length));
            if (m_onMessage) m_onMessage(payload, length, true);
            break;

        default:
            break;
    }
}