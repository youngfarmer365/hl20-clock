# Yard Mixer Clock (HL-20)

Laptop USB serial bridge + iPad yard clock.

```
pip install pyserial
python hl20_bridge.py --port COM5
```

Open `http://127.0.0.1:8765/` on the laptop, or the laptop LAN IP on the iPad (MixerClock Wi-Fi).

This is the **full** Farm Manager clock: login, Loads / Premixes, fill, feedout, offline cache, PWA.

The old 16 KB file was only a simple pen countdown.
