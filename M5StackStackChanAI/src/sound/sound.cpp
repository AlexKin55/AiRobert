#include "sound/sound.h"

#include <M5Unified.h>

bool EspSound::begin()
{
    return begin(Config{});
}

bool EspSound::begin(const Config& config)
{
    config_ = config;

    if (!M5.Display.width())
    {
        auto cfg = M5.config();
        M5.begin(cfg);
    }
    M5.Power.setExtPower(true);

    auto spkCfg = M5.Speaker.config();
    spkCfg.sample_rate = config_.sampleRate;
    M5.Speaker.config(spkCfg);
    M5.Speaker.setVolume(200);

    enabled_ = M5.Speaker.begin();
    return enabled_;
}

bool EspSound::isEnabled() const
{
    return enabled_;
}

void EspSound::end()
{
    if (enabled_)
    {
        M5.Speaker.end();
        enabled_ = false;
    }
}

void EspSound::stop()
{
    if (enabled_)
    {
        M5.Speaker.stop();
    }
}

bool EspSound::ensureReady()
{
    // Switching the shared CoreS3 I2S bus to the speaker: release the port
    // from the mic (if it is still listening) and start the speaker.
    // M5.Speaker.begin() is idempotent and switches the codec itself (AW88298).
    if (M5.Mic.isRunning())
    {
        M5.Mic.end();
        vTaskDelay(pdMS_TO_TICKS(20));  // let the driver release the port
    }
    enabled_ = M5.Speaker.begin();
    return enabled_;
}

bool EspSound::playTone(uint16_t frequencyHz, uint16_t durationMs)
{
    if (!enabled_)
    {
        return false;
    }
    M5.Speaker.tone(frequencyHz, durationMs);
    return true;
}

bool EspSound::playSample(const int16_t* data, size_t samples)
{
    if (!enabled_)
    {
        return false;
    }
    // IMPORTANT: the 4th argument of playRaw is bool stereo. Passing the
    // channel count (1) is wrong: 1 == true, and mono PCM would be played as
    // stereo (2x speed, "chipmunk" voice). Ours is always mono.
    //
    // We pin the channel (0) and do not interrupt the current sound: all PCM
    // chunks of one playback go into a single stream queue without gaps or
    // clicks at the joints (with channel=-1 each playRaw would start a new
    // stream, and chunks would overlap/click).
    M5.Speaker.playRaw(data, samples, config_.sampleRate, false, 1, 0, false);
    return true;
}