#ifndef SCREEN_H_
#define SCREEN_H_

#include <cstddef>
#include <cstdint>

#include <Avatar.h>

// Robot face emotions (mimicry). Value order matches the Expression enum of
// the M5Stack-Avatar library.
enum class Emotion
{
    Neutral,  // calm expression
    Happy,    // joy
    Angry,    // anger
    Sad,      // sadness
    Doubt,    // doubt
    Sleepy,   // sleepiness
};

// Shows emotions on the robot display via the animated M5Stack-Avatar.
class EspScreen
{
    public:
    bool begin();
    // Sets the current emotion on the avatar's face.
    void setEmotion(Emotion emotion);
    // Returns the current emotion.
    Emotion getEmotion() const;
    // Shows a speech bubble with text over the avatar.
    void setSpeechText(const char* text);
    // Clears the speech bubble text.
    void clearSpeechText();

    // Draws an RGB565 frame (e.g. camera preview) full screen.
    void showFrame(uint16_t width, uint16_t height, const void* rgb565);

    private:
    m5avatar::Avatar avatar_;
    Emotion emotion_ = Emotion::Neutral;
    bool initialized_ = false;
};

#endif  // SCREEN_H_