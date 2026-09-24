#include "move/move.h"

#include <M5Unified.h>
#include <M5StackChan.h>

namespace {

// Empirical factor converting "degrees" to Motion units
// (as in the working project /home/alex/Work/Robot/src/move.cpp).
constexpr int kDegToMotion = 10;
// Smooth movement speed (0-1000).
constexpr int kMotionSpeed = 900;
// Movement completion wait timeout, ms.
constexpr uint32_t kMotionTimeoutMs = 1500;

// Waits until the movement finishes (or the timeout elapses).
void waitMotion()
{
    const uint32_t start = millis();
    while (M5StackChan.Motion.isMoving() &&
           (millis() - start) < kMotionTimeoutMs)
    {
        delay(2);
    }
}

int clampAngle(int v, int lo, int hi)
{
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

}  // namespace

bool EspMovement::begin()
{
    if (initialized_)
    {
        return true;
    }

    if (!M5.Display.width())
    {
        auto cfg = M5.config();
        M5.begin(cfg);
    }
    M5.Power.setExtPower(true);
    M5StackChan.begin();

    center();
    initialized_ = true;
    return true;
}

void EspMovement::apply()
{
    if (!initialized_)
    {
        return;
    }
    M5StackChan.Motion.moveX(panDeg_ * kDegToMotion, kMotionSpeed);
    M5StackChan.Motion.moveY(tiltDeg_ * kDegToMotion, kMotionSpeed);
    waitMotion();
}

void EspMovement::center()
{
    panDeg_ = 0;
    tiltDeg_ = 0;
    if (!initialized_)
    {
        return;
    }
    M5StackChan.Motion.goHome(kMotionSpeed);
    waitMotion();
}

void EspMovement::turnLeft(int degrees)
{
    panDeg_ = clampAngle(panDeg_ + degrees, config_.minAngle, config_.maxAngle);
    apply();
}

void EspMovement::turnRight(int degrees)
{
    panDeg_ = clampAngle(panDeg_ - degrees, config_.minAngle, config_.maxAngle);
    apply();
}

void EspMovement::turnUp(int degrees)
{
    tiltDeg_ = clampAngle(tiltDeg_ + degrees, config_.minAngle, config_.maxAngle);
    apply();
}

void EspMovement::turnDown(int degrees)
{
    tiltDeg_ = clampAngle(tiltDeg_ - degrees, config_.minAngle, config_.maxAngle);
    apply();
}

void EspMovement::setPan(int degrees)
{
    panDeg_ = clampAngle(degrees, config_.minAngle, config_.maxAngle);
    apply();
}

void EspMovement::setTilt(int degrees)
{
    tiltDeg_ = clampAngle(degrees, config_.minAngle, config_.maxAngle);
    apply();
}

int EspMovement::panDeg() const
{
    return panDeg_;
}

int EspMovement::tiltDeg() const
{
    return tiltDeg_;
}

void EspMovement::stop()
{
    center();
}