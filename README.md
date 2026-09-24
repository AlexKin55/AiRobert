# AiRobert

Voice assistant "Robert" built on M5Stack: the robot hears through the camera,
the server recognizes speech (Yandex SpeechKit) and drives the robot.

## Project layout

| Directory | What it is | Stack |
|---|---|---|
| [`M5StackStackChanAI/`](M5StackStackChanAI/) | Robot firmware (Stack Chan enclosure) | ESP32, C++ |
| [`M5StackUnitV2-M12/`](M5StackUnitV2-M12/) | UnitV2 camera firmware: microphones + snapshots | Python 3.8 (Linux), arecord, cv2 |
| [`AiService/`](AiService/) | Middleware server: camera ⇄ robot + Yandex STT | Python 3, FastAPI |

## How it works

```
M5StackUnitV2-M12 ──WS /camera──▶ AiService ──WS /robot──▶ M5StackStackChanAI
   (microphones + VAD)              │  └── Yandex STT (speech -> text)
   (snapshots on demand)            └── records/*_in.wav (+ *_in.txt)
```

- The camera captures audio via `arecord` (16 kHz mono); an adaptive VAD sends
  only speech segments. A loud trigger ("Robert") lowers the threshold, so a
  quiet continuation of the dialogue is recorded too.
- The server receives JSON messages (base64 PCM), writes WAV segments,
  streams each utterance to Yandex SpeechKit STT and saves the text next to
  the recording (`records/<time>_in.txt`).
- The robot receives commands/audio over a second WebSocket (JSON).

## Quick start

1. Server:
   ```
   cd AiService
   pip install -r requirements.txt
   export YANDEX_API_KEY=... YANDEX_FOLDER_ID=...   # or config/settings.json
   ./scripts/run_server.sh                          # ws://0.0.0.0:9002
   ```
2. Camera:
   ```
   cd M5StackUnitV2-M12
   pip install -r requirements.txt                  # + sudo apt install alsa-utils
   ./scripts/run_camera.sh [SECONDS]                # 0 = run forever
   ```
3. Robot: build and flash [`M5StackStackChanAI/`](M5StackStackChanAI/) (PlatformIO/arduino), set the server address in `config/config.h`.

## Protocol

Both WebSockets use text JSON messages (binary data is base64-encoded, every
message carries `timestamp` in µs):

- **Camera → server**: `hello`, `audio` (PCM), `image` (JPEG), `face`, `emotion`, `hb`.
- **Server → camera**: `ok`, `error`, `mute`, `unmute`, `capture`.
- **Server → robot**: `audio` (playback PCM, `""` = end), `movement`, `emotion`.
- **Robot → server**: `hb`, `ack`.

Details in [`AiService/README.md`](AiService/README.md).
