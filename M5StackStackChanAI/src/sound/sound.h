#ifndef SOUND_H_
#define SOUND_H_

#include <cstddef>
#include <cstdint>

// Sound playback through the robot speakers (M5Unified Speaker, I2S).
class EspSound
{
    public:
    struct Config
    {
        uint32_t sampleRate = 16000;  // Hz
        uint16_t channels = 1;
    };

    // Initializes the speaker with the default configuration.
    bool begin();
    // Initializes the speaker with the given configuration.
    bool begin(const Config& config);
    // Whether the speaker is enabled.
    bool isEnabled() const;
    // Releases the speaker. On CoreS3 it must be called before the mic
    // captures audio, because the speaker and the mic share I2S resources.
    void end();
    // Stops the current playback.
    void stop();

    // Ensures the speaker is initialized and ready to play. If the mic shut
    // it down (shared I2S on CoreS3) — reinitializes it. Returns true when
    // playback is possible.
    bool ensureReady();
    // Plays a tone of the given frequency (Hz) and duration (ms).
    bool playTone(uint16_t frequencyHz, uint16_t durationMs);
    // Plays a PCM sample (int16 mono) at the configured rate.
    bool playSample(const int16_t* data, size_t samples);

    private:
    Config config_;
    bool enabled_ = false;
};

#endif  // SOUND_H_