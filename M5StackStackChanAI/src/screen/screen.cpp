#include "screen/screen.h"

#include <M5Unified.h>

using namespace m5avatar;

namespace {

// Maps our emotion to the M5Stack-Avatar face expression.
Expression toAvatarExpression(Emotion e)
{
    switch (e)
    {
        case Emotion::Happy: return Expression::Happy;
        case Emotion::Angry: return Expression::Angry;
        case Emotion::Sad: return Expression::Sad;
        case Emotion::Doubt: return Expression::Doubt;
        case Emotion::Sleepy: return Expression::Sleepy;
        case Emotion::Neutral:
        default: return Expression::Neutral;
    }
}

}  // namespace

bool EspScreen::begin()
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

    // Start the avatar face animation and set a neutral expression.
    avatar_.init();
    avatar_.setExpression(Expression::Neutral);

    initialized_ = true;
    return true;
}

void EspScreen::setEmotion(Emotion emotion)
{
    emotion_ = emotion;
    if (!initialized_)
    {
        return;
    }
    avatar_.setExpression(toAvatarExpression(emotion));
}

Emotion EspScreen::getEmotion() const
{
    return emotion_;
}

void EspScreen::setSpeechText(const char* text)
{
    if (!initialized_)
    {
        return;
    }
    avatar_.setSpeechText(text);
}

void EspScreen::clearSpeechText()
{
    if (!initialized_)
    {
        return;
    }
    avatar_.setSpeechText("");
}

void EspScreen::showFrame(uint16_t width, uint16_t height, const void* rgb565)
{
    if (!initialized_ || rgb565 == nullptr)
    {
        return;
    }
    M5.Display.pushImage(0, 0, width, height,
                         static_cast<const uint16_t*>(rgb565));
}