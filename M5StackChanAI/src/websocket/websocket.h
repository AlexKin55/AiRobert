#ifndef WEBSOCKET_H_
#define WEBSOCKET_H_

#include <cstddef>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>

#include <WebSocketsClient.h>

// WebSocket client over Wi-Fi for ESP32 (links2004/WebSockets library).
class EspWebsocketClient
{
    public:
    using ConnectedCallback = std::function<void()>;
    using DisconnectedCallback = std::function<void(uint16_t code, const std::string& reason)>;
    using MessageCallback = std::function<void(const uint8_t* data, size_t size, bool binary)>;

    EspWebsocketClient();
    ~EspWebsocketClient();

    // Starts connecting to ws://host:port/path. The connection is
    // established asynchronously — the connected event arrives in loop(),
    // which must be called from the main loop (e.g. in a test) until
    // CONNECTED appears.
    bool connect(const char* host, uint16_t port, const char* path = "/");

    // Whether the connection is currently alive.
    bool isConnected();

    // Closes the connection.
    void disconnect();

    // Sends a text / binary message.
    bool sendText(const std::string& text);
    bool sendBinary(const uint8_t* data, size_t size);

    // Services the WebSocket network (events/data). Call regularly.
    void loop();

    // Event handler registration.
    void setConnectedCallback(ConnectedCallback cb);
    void setDisconnectedCallback(DisconnectedCallback cb);
    void setMessageCallback(MessageCallback cb);

    private:
    void handleEvent(WStype_t type, uint8_t* payload, size_t length);

    // Mutex for ALL WebSocketsClient operations (send and receive): PCM
    // chunks are sent from the mic I2S task, while text commands/ACKs and
    // m_ws.loop() (receive) come from loopTask. The WebSockets library is
    // not thread-safe. Recursive: loop() invokes callbacks that send ACKs
    // through the same mutex (re-locking by the same thread is allowed).
    std::recursive_mutex m_wsMutex;

    WebSocketsClient m_ws;
    ConnectedCallback m_onConnected;
    DisconnectedCallback m_onDisconnected;
    MessageCallback m_onMessage;
};

#endif  // WEBSOCKET_H_