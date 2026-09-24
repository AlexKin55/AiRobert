#ifndef MOVE_H_
#define MOVE_H_

#include <cstdint>

// Robot head rotation control (pan-tilt servos) for ESP32.
// Horizontal (yaw): left/right. Vertical (pitch): up/down.
// Angles are integers (degrees); they are tracked in the internal state
// (panDeg()/tiltDeg()) and sent to the drives via M5StackChan. Integer values
// were chosen because the ESP32-S3 has no hardware FPU and float precision is
// redundant here — movement is quantized by the kDegToMotion factor.
class EspMovement
{
    public:
    struct Config
    {
        // Angle limits, degrees.
        int minAngle = -180;
        int maxAngle = 180;
    };

    bool begin();
    // Returns the head to the center position (pan=0, tilt=0).
    void center();

    // Relative turns from the current position.
    void turnLeft(int degrees);   // pan += degrees
    void turnRight(int degrees);  // pan -= degrees
    void turnUp(int degrees);     // tilt += degrees
    void turnDown(int degrees);   // tilt -= degrees

    // Absolute positioning.
    void setPan(int degrees);
    void setTilt(int degrees);

    // Current position (internal state), degrees.
    int panDeg() const;
    int tiltDeg() const;

    // Stops movement / returns to center.
    void stop();

    private:
    void apply();  // sends the current angles to the drives

    int panDeg_ = 0;
    int tiltDeg_ = 0;
    Config config_;
    bool initialized_ = false;
};

#endif  // MOVE_H_