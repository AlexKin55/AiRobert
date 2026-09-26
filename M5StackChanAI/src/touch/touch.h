#ifndef AIBOT_TOUCH_H_
#define AIBOT_TOUCH_H_

// Head touch sensor (Si12T, M5Stack CoreS3 In_I2C): detects touching and
// swiping the robot's head. The firmware polls update() in the main loop and
// sends the resulting gesture to the AiService over WebSocket
// ({"type":"touch","action":...}).
//
// Gestures:
//   Press          — the head was touched;
//   Release        — the touch ended;
//   SwipeForward   — a swipe toward the user (head pushed away);
//   SwipeBackward  — a swipe away from the user (head pulled toward).
//
// Ported from AI_StackChan_Ex (driver/HeadTouchSensor.cpp); the low-level
// Si12T driver lives in lib/Si12T.

enum class TouchGesture {
    None,
    Press,
    Release,
    SwipeForward,
    SwipeBackward,
};

namespace TouchSensor {

// Initializes the Si12T sensor (idempotent; safe when absent).
void begin();

// Polls the sensor (internally rate-limited to ~20 Hz). Returns the gesture
// that happened since the last call, or TouchGesture::None.
TouchGesture update();

// True when the Si12T sensor was found and initialized.
bool isAvailable();

// "Press" / "Release" / "SwipeForward" / "SwipeBackward" / "None".
const char* gestureName(TouchGesture gesture);

}  // namespace TouchSensor

#endif  // AIBOT_TOUCH_H_