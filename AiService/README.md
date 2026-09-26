# AiService

Middleware service (Python 3, FastAPI) between the **camera** and the **robot**.
It listens on **two** WebSockets on one port; the protocol is **JSON** (binary
data is embedded in JSON via base64):

| WebSocket | Direction | Payload |
|-----------|-----------|---------|
| `/camera` | camera → server | microphone audio (`audio`, base64 PCM), pictures (`image`, base64 JPEG), `face`/`emotion` events |
| `/camera` | server → camera | `mute`/`unmute` (microphone), `capture` (request a picture) |
| `/robot`  | server → robot | playback audio as **binary frames** `[type][codec][raw PCM]` (+ empty EOF frame), `movement`/`emotion` as JSON text |
| `/robot`  | robot → server | `hb`, `ack` (JSON text) |

The robot connects as a WS client directly to the server (over Wi-Fi), as
before; the camera is not relaying anything. The robot's microphone is
disabled — audio comes only from the camera.

AI processing is handled by [`processor.py`](src/processor.py): a SINGLE
voice channel of the Yandex AI Studio **Realtime API** — the camera audio
question goes straight into a persistent WebSocket session, the Speech
Realtime model recognizes it, generates the answer and synthesizes the speech
in one pass (no STT → GPT → TTS chain); the finished audio answer is played
back to the robot in the background. Object detection and face-id dialogue
remain stubs. The full "wiring" (protocol, two WebSockets, state machine,
recording, statistics) works for real.

## Run

```bash
pip install -r requirements.txt
./scripts/run_server.sh                 # ws://0.0.0.0:9002/camera and /robot
# or manually:
python3 -m src.server                   # from the AiService directory
```

Host/port: `HOST=... PORT=... ./scripts/run_server.sh` or the env vars
`AISERVICE_HOST` / `AISERVICE_PORT` (default `0.0.0.0:9002`, see
[`config/settings.json`](config/settings.json)).

Diagnostics: `GET /health` — state, connection flags, statistics (frames,
PCM bytes, robot commands, last face, last head-touch).

Test without the camera:

```bash
python3 scripts/test_camera.py --host 127.0.0.1 --port 9002 \
    --image /path/to/photo.jpg
```

## Layout

```
AiService/
├── config/
│   ├── settings.json      — service config (ports, WS paths, formats)
│   └── logging.json       — uvicorn log format
├── scripts/
│   ├── run_server.sh      — service launcher
│   └── test_camera.py     — camera test client (JSON protocol)
└── src/
    ├── server.py          — entry point: FastAPI + two WebSockets
    ├── config.py          — config loading (env substitution ${VAR})
    ├── protocol.py        — JSON protocol constants and helpers
    ├── websocket.py       — base WS session (send wrapper)
    ├── camera.py          — camera session: JSON parsing, mute/unmute/capture
    ├── robot.py           — robot session: JSON audio chunks, movement/emotion
    ├── state_machine.py   — event routing, service states
    ├── emotion_decay.py   — post-dialogue emotion decay (Neutral -> Doubt -> Sleepy)
    ├── recorder.py        — debug recording of audio (.wav), pictures (.jpg), model prompts
    ├── processor.py       — AI pipeline: Yandex Realtime (audio question -> audio answer)
    ├── yandex.py          — Yandex AI Studio Realtime API (WebSocket voice channel)
    ├── callbacks.py       — Realtime function-calling tools & handlers (emotion, weather)
    └── aiservice.py       — (legacy module)
```

## Debug media recording

For debugging the server saves media to `recording.record_dir` (default
`records/`, override with env `RECORD_DIR`). Files are readable with standard
tools:

- `<reception time>_in.wav` — audio from the camera (first chunk = creation time);
- `<time>_in.txt` — recognized question transcript from the Realtime session (next to the WAV);
- `<time>_out.wav` — outgoing audio to the robot (playback);
- `<time>_tts.wav` — answer PCM from the Realtime session (one per answer);
- `<time>_prompt.txt` — model exchange: system prompt + question transcript + answer text;
- `<time>_img.jpg` — pictures from the camera.

`recording.save_audio` / `save_images` toggle media saving;
`recording.save_tts_audio` / `save_prompts` toggle the answer/prompt debug
files; `recording.audio_rotate_seconds` rotates the incoming WAV (0 = one
file per camera connection session).

The `_in.wav` file is finalized when the utterance ends (a pause in audio
longer than `AUDIO_SEGMENT_GAP` = 3 s), so it can be played while the server
keeps running; each speech segment produces its own WAV (and TXT when the
Realtime session returns the question transcript).

## Yandex Realtime: аудио-вопрос → аудио-ответ

The camera sends audio only during speech (its VAD); every chunk goes into a
**single persistent Realtime session** — no STT → GPT → TTS chain:

1. `Processor.start()` opens one WebSocket session of the Yandex AI Studio
   Realtime API
   (`wss://ai.api.cloud.yandex.net/v1/realtime/openai?model=gpt://<folder>/<model>`)
   and configures it: system prompt (`yandex.system_prompt`), output voice
   (`yandex.voice` / `role`), PCM formats (`yandex.realtime_input_rate` /
   `realtime_output_rate`, default 16000 Hz — no resampling) and the
   server-side VAD;
2. camera PCM goes into the session immediately
   (`input_audio_buffer.append`);
3. the server-side VAD detects the end of the utterance and the Speech
   Realtime model recognizes the speech, generates the answer and synthesizes
   it **in one pass**;
4. the finished answer arrives as `response.output_audio.delta` chunks (PCM
   16 kHz mono); the model reports the robot emotion as a FUNCTION CALL
   (`emotion(name)`, function calling / tools) instead of speaking it, so the
   answer audio never contains "Emotion: ..." — the tool schema and the
   server-side handler live in [`callbacks.py`](src/callbacks.py)
   (`TOOLS` / `dispatch`), the call sends the robot the EMOTION:<name>
   command, then the PCM is played back (`sm.send_robot_audio`). A trailing
   text `Emotion: <name>` line remains only as a fallback for models that
   ignore the tool (`split_emotion()`);
5. the question transcript (`conversation.item.input_audio_transcription
   .completed`) is saved next to the `_in.wav` (as `_in.txt`);
6. a lost connection is restored by the watchdog automatically
   (`Processor.ensure_running()`).

The Realtime parameters live in the `yandex` section of
[`config/settings.json`](config/settings.json): `realtime_model` (default
`speech-realtime-260528`), `voice`, `role`, rates, language, timeout. The
debug saving is controlled by `recording.save_tts_audio` / `save_prompts`.

The post-dialogue emotion decay also uses the same Realtime model but with a
one-shot TEXT → audio request (`Processor.say_emotion()` → `ask_audio()`): the
model both generates the phrase and speaks it in a single call.

Credentials: `YANDEX_API_KEY` / `YANDEX_FOLDER_ID` env vars or the `yandex`
section of [`config/settings.json`](config/settings.json). If Yandex is
unavailable or the credentials are invalid, errors are logged with a clear
category (no connection / bad key / forbidden) and the server keeps working —
when Yandex recovers, new utterances are answered as usual.

---

# Camera protocol (`/camera`)

The camera connects as a WebSocket client to `ws://HOST:PORT/camera`.
All messages are **text JSON**; binary data (PCM/JPEG) is base64.

## Camera → server

| type      | example                                                                |
|-----------|------------------------------------------------------------------------|
| `hello`   | `{"type":"hello","device":"unitv2","format":{"audio":{"rate":16000,"channels":1,"bits":16},"image":{"codec":"jpeg"}}}` |
| `audio`   | `{"type":"audio","audio":"<base64 PCM int16 LE 16kHz mono>"}`          |
| `image`   | `{"type":"image","image":"<base64 JPEG>"}`                             |
| `face`    | `{"type":"face","face_id":"alex","confidence":0.97}`                   |
| `emotion` | `{"type":"emotion","emotion":"happy"}` — face emotion from the camera  |
| `hb`      | `{"type":"hb"}`                                                        |

## Server → camera

| type      | format                                     |
|-----------|--------------------------------------------|
| `ok`      | `{"type":"ok","detail":"ok"}` — reply to every message |
| `error`   | `{"type":"error","detail":"..."}` — malformed message |
| `mute`    | `{"type":"mute"}` — turn the mic off (the robot is playing) |
| `unmute`  | `{"type":"unmute"}` — turn the mic on      |
| `capture` | `{"type":"capture"}` — take a snapshot and send `image` |

---

# Robot protocol (`/robot`)

The robot (firmware) connects as a WebSocket client to `ws://HOST:PORT/robot`
(in the firmware: `WS_HOST`/`WS_PORT`/`WS_PATH`). The robot's microphone is
disabled — audio comes only from the camera.

## Server → robot

| message   | format                                                                 |
|-----------|------------------------------------------------------------------------|
| `audio`   | **binary** `[type=1][codec=1][raw PCM int16 LE 16 kHz mono]` — playback chunk; `[type][codec]` with empty payload — EOF marker |
| `movement`| text `{"type":"movement","axis":"left","degrees":60}` / `{"type":"movement","axis":"center"}` |
| `emotion` | text `{"type":"emotion","name":"happy"}` (happy/angry/sad/doubt/sleepy/neutral) |

Audio is sent as **binary frames** (no base64/JSON) so the ESP32 plays the
PCM directly from the WS frame buffer without decoding — no stutter. Chunk —
`robot.play_chunk_seconds` (0.15 s = 4800 B, stays under the robot WS client
receive limit of ~8 KB). Delivery pace — `robot.play_speed` (1.0 = real time):
the pause between frames equals the frame duration / speed; the stream ends
with an empty `[type][codec]` frame (EOF).

## Robot → server

| type    | example                                    |
|---------|--------------------------------------------|
| `hb`    | `{"type":"hb"}`                            |
| `ack`   | `{"type":"ack","command":"MOVE:left:60"}` — movement done |
| `touch` | `{"type":"touch","action":"press"}` — head touch: `press`, `release`, `swipe_forward`, `swipe_backward` (Si12T sensor) |

---

# Camera ↔ robot link (UART)

The robot and the camera are connected directly over UART. This channel
handles two tasks, so they are **not** processed by AiService:

- **Echo cancellation (hardware half-duplex).** Before playing audio the
  robot sends the camera a UART command "mic off", after playback ends (with a
  decay pause) — "mic on". No software safeguards are needed in the service.
- **Face tracking.** The camera detects the face and sends the robot offset
  coordinates over UART directly (closed servo loop needs minimal latency).
  AiService only receives `face` events (face id) for dialogue logic.

---

# State machine

[`state_machine.py`](src/state_machine.py):

```
DISCONNECTED — nothing connected
IDLE         — peers connected, no active audio stream
STREAMING    — the camera is streaming audio chunks
```

Camera events (audio/picture/face/emotion) → `Processor`. Camera PCM goes
into the persistent Yandex Realtime session; when the model finishes the
audio answer, `state_machine` receives it via the `on_answer` callback and
plays it back to the robot (`sm.send_robot_audio`) with the optional emotion
command (`Emotion: <name>` from the answer text). The recognized question
transcript comes via `on_user_text`, speech start via `on_speech_started`.
Robot commands are sent via `sm.send_robot_move/emotion/audio`. All playback
is serialized by a lock — the main answers and the decay phrases never mix.

When a dialogue ends, [`emotion_decay.py`](src/emotion_decay.py) schedules the
post-dialogue emotion decay: after `neutral_min` the robot gets Neutral, then
Sad (`sad_min`), then Sleepy (`sleepy_min`) — the delays (in MINUTES), the
`prompt` and the sound files live in the separate `emotion_decay` section of
[`config/settings.json`](config/settings.json); each stage asks the Realtime
model for a short phrase (one-shot TEXT → audio) and plays it back. A new
dialogue cancels the countdown.

# Roadmap

1. Real object detection in [`processor.detect_objects()`](src/processor.py)
   (YOLO/ONNX) and face-id handling.
2. ~~Dialogue logic: Yandex Realtime audio → audio (no STT/GPT/TTS chain).~~
   Done — see [`processor.py`](src/processor.py) and
   [`state_machine._on_realtime_answer()`](src/state_machine.py).
3. Robot notification when Yandex is unavailable (voice prompt).
4. A `set` command to change camera resolution/fps on the fly.