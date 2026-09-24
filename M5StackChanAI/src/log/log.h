// Minimal leveled firmware logger (Serial output).
//
// The level is set in config/config.h (LOG_LEVEL) and can be overridden by
// the build flag -DLOG_LEVEL=N (e.g. in platformio.ini build_flags):
//   0 = OFF      — no logs;
//   1 = ERROR    — errors only;
//   2 = WARN     — errors and warnings;
//   3 = INFO     — normal operation (default);
//   4 = DEBUG    — detailed/frequent messages (chunks, frames, WS frames).
//
// Call format is like Serial.printf: LOG_I("[audio] done: %d\n", n);
// (the newline is included in the format string, as in the existing code).

#pragma once

#include <Arduino.h>

#include "config/config.h"

#define LOG_LEVEL_OFF 0
#define LOG_LEVEL_ERROR 1
#define LOG_LEVEL_WARN 2
#define LOG_LEVEL_INFO 3
#define LOG_LEVEL_DEBUG 4

#ifndef LOG_LEVEL
#define LOG_LEVEL LOG_LEVEL_INFO
#endif

#if LOG_LEVEL >= LOG_LEVEL_DEBUG
#define LOG_D(fmt, ...) Serial.printf((fmt), ##__VA_ARGS__)
#else
#define LOG_D(fmt, ...) ((void)0)
#endif

#if LOG_LEVEL >= LOG_LEVEL_INFO
#define LOG_I(fmt, ...) Serial.printf((fmt), ##__VA_ARGS__)
#else
#define LOG_I(fmt, ...) ((void)0)
#endif

#if LOG_LEVEL >= LOG_LEVEL_WARN
#define LOG_W(fmt, ...) Serial.printf((fmt), ##__VA_ARGS__)
#else
#define LOG_W(fmt, ...) ((void)0)
#endif

#if LOG_LEVEL >= LOG_LEVEL_ERROR
#define LOG_E(fmt, ...) Serial.printf((fmt), ##__VA_ARGS__)
#else
#define LOG_E(fmt, ...) ((void)0)
#endif