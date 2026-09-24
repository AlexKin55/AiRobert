#ifndef CONFIG_H_
#define CONFIG_H_

#include <cstdint>

// Firmware log level (src/log/log.h):
//   0=off, 1=errors, 2=warnings, 3=info (default),
//   4=debug (frequent/detailed messages: chunks, frames, WS frames).
// Overridable by build flag -DLOG_LEVEL=4 in platformio.ini.
#ifndef LOG_LEVEL
#define LOG_LEVEL 3
#endif

// ---------------------------------------------------------------------------
// Robot firmware configuration (app src/aibot).
// Every value can be overridden with build flags (-DWIFI_SSID="..." etc.)
// without editing the code. Test-only parameters live in config/test_config.h.
// ---------------------------------------------------------------------------

// Access point.
#ifndef WIFI_SSID
#define WIFI_SSID "MGTS_GPON_AEDE"
#endif

#ifndef WIFI_PASS
#define WIFI_PASS "GxhpbRa7"
#endif

// Timeout of a single access-point connection attempt, ms.
#ifndef WIFI_CONNECT_TIMEOUT_MS
#define WIFI_CONNECT_TIMEOUT_MS 20000u
#endif

// Delay between connection retries after a link loss, ms.
#ifndef WIFI_RECONNECT_DELAY_MS
#define WIFI_RECONNECT_DELAY_MS 5000u
#endif

// WebSocket server the robot connects to.
#ifndef WS_HOST
#define WS_HOST "192.168.1.8"
#endif

#ifndef WS_PORT
#define WS_PORT 9001u
#endif

#ifndef WS_PATH
#define WS_PATH "/"
#endif

// Heartbeat (robot -> server), ms. Every 15 s the robot sends a text frame
// "HB" so the router/Wi-Fi does not drop an idle TCP session (NAT dies after
// ~2 minutes without traffic). The server only logs HB and NEVER drops the
// connection when HB stops arriving.
#ifndef WS_HEARTBEAT_INTERVAL_MS
#define WS_HEARTBEAT_INTERVAL_MS 15000u
#endif

// Microphone capture settings.
#ifndef MIC_SAMPLE_RATE
#define MIC_SAMPLE_RATE 16000u
#endif

// Audio chunk length (seconds) used to send the recording to the server.
// Continuous streaming during recording causes I2S interference on CoreS3
// (clicks/whistles), so audio is buffered locally and sent in packets.
// 1 s of PCM = 32 KB — smaller packets, shorter send from the I2S callback.
#ifndef MIC_AUDIO_CHUNK_SECONDS
#define MIC_AUDIO_CHUNK_SECONDS 1u
#endif

#ifndef MIC_CHANNELS
#define MIC_CHANNELS 1u
#endif

// Capture frame = 320 samples (20 ms @16 kHz) — a fixed PCM frame.
#ifndef MIC_FRAME_SAMPLES
#define MIC_FRAME_SAMPLES 320u
#endif

// Noise gate: RMS threshold; audio is forwarded to the client above it.
#ifndef MIC_NOISE_THRESHOLD
#define MIC_NOISE_THRESHOLD 100.0f
#endif

// How many frames are still forwarded after the level drops below the gate.
#ifndef MIC_HANGOVER_FRAMES
#define MIC_HANGOVER_FRAMES 4u
#endif

// ---------------------------------------------------------------------------
// VAD: the robot itself starts recording on a sharp noise rise and stops on
// silence or after VAD_MAX_SEGMENT_MS, sending RECORD:start/stop to the server.
// ---------------------------------------------------------------------------

// Minimum RMS threshold for speech detection (int16 scale). Must be ABOVE the
// room noise floor (~700-1300): threshold adaptation (noiseFloor*1.6) only
// works while the floor stays below the current threshold. With a threshold
// of 900 and a floor of ~1200 the noise was treated as speech permanently —
// endless false recordings and the robot stopped listening to real speech.
// 1400 is the proven value.
#ifndef VAD_MIN_THRESHOLD
#define VAD_MIN_THRESHOLD 1400.0f
#endif

// Background-noise estimate adaptation speed (0..1, 20 ms frame).
#ifndef VAD_NOISE_ADAPT
#define VAD_NOISE_ADAPT 0.03f
#endif

// Consecutive frames above the threshold required to start recording.
// 5 frames (100 ms) filter out single clicks and short noise bursts.
#ifndef VAD_START_HANGOVER_FRAMES
#define VAD_START_HANGOVER_FRAMES 5u
#endif

// Silence (ms) after the last speech — end of the phrase. 1000 ms: if the
// silence between words is longer than a second, the question is considered
// asked and goes for recognition.
#ifndef VAD_SILENCE_MS
#define VAD_SILENCE_MS 1000u
#endif

// Maximum phrase length, ms (forced stop). 10000 ms (x2 of 5000) lets a long
// phrase be heard completely (5 s used to cut speech mid-sentence).
#ifndef VAD_MAX_SEGMENT_MS
#define VAD_MAX_SEGMENT_MS 10000u
#endif

// Playback idle timeout (ms): if no more PCM chunks arrive from the server
// (including the zero-length end marker), the robot releases the I2S speaker
// by itself and returns the microphone to listening (VAD).
#ifndef PLAYBACK_IDLE_TIMEOUT_MS
#define PLAYBACK_IDLE_TIMEOUT_MS 6000u
#endif

#endif  // CONFIG_H_