# Mixer Clock — ESP32

Phone joins WiFi **MixerClock** / password **feedout1**, then opens **http://192.168.4.1/**

iPhone and iPad work this way. Web Bluetooth does not work in Safari, so the mixer is a hotspot, not a Bluetooth speaker.

## Hardware

- ESP32 DevKit
- MAX3232 RS232 module (required — the HL-20 loom is ± RS232, not 3.3 V)
- 12 V → 5 V buck from the brown wire
- Radio **unplugged**

| HL-20 | MAX3232 / ESP32 |
|---|---|
| green GND | GND (common) |
| white (weight out) | MAX3232 T1IN ← from mixer, then TTL to ESP32 GPIO 16 (RX2) |
| yellow (keys in) | ESP32 GPIO 17 (TX2) → MAX3232 TTL in → RS232 out to yellow |
| brown 12 V | buck 5 V → ESP32 VIN |

Typical MAX3232: mixer white → T1IN, T1OUT → GPIO 16. GPIO 17 → R1IN, R1OUT → mixer yellow.

## Flash

Arduino IDE: board **ESP32 Dev Module**, upload `mixer_clock/mixer_clock.ino`.

## Drop

1. Fill wagon.
2. Phone on MixerClock Wi-Fi, open the clock.
3. **Zero total** once.
4. **Start pen** — mixer zeros, clock at 0.
5. Dump until **−target** (pen 300 → −300). Hold 2 s steady, it records and moves on.
6. **Next pen** if feed is still in the trough from yesterday.
