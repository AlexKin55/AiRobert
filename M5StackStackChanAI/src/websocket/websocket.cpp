#include "websocket/websocket.h"

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

void EspWebsocketClient::handleEvent(WStype_t type, uint8_t* payload, size_t length)
{
    switch (type)
    {
        case WStype_CONNECTED:
            if (m_onConnected) m_onConnected();
            break;

        case WStype_DISCONNECTED:
            if (m_onDisconnected)
            {
                const std::string reason = payload ? reinterpret_cast<const char*>(payload) : "";
                m_onDisconnected(0, reason);
            }
            break;

        case WStype_TEXT:
            if (m_onMessage)
            {
                size_t n = length;
                // Some library versions include the trailing NUL in length.
                if (n > 0 && payload[n - 1] == 0) --n;
                m_onMessage(payload, n, false);
            }
            break;

        case WStype_BIN:
            if (m_onMessage) m_onMessage(payload, length, true);
            break;

        default:
            break;
    }
}