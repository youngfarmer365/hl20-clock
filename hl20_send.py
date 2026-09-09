#!/usr/bin/env python3
"""Replay in-cab 0 / Total on the YELLOW wire (HL-20 RX).

Radio OFF or unplugged so two transmitters do not fight.

USB GND -> green (GND)
USB TX  -> yellow   (the -4 V quiet wire, NOT the jumping white wire)

  python hl20_send.py --port COM5 zero
  python hl20_send.py --port COM5 total
"""
from __future__ import annotations

import argparse
import sys
import time

try:
    import serial
except ImportError:
    print("pip install pyserial")
    sys.exit(1)

# Captured 2026-09-09 on yellow @ 38400 8N1 while pressing in-cab keys.
# STX + 8 digits + checksum + 0x05
# checksum = 82 + sum of the 8 payload digits
KEYS = {
    "zero": "00001000",
    "0": "00001000",
    "total": "00400000",
}


def frame(payload8: str) -> bytes:
    if len(payload8) != 8 or not payload8.isdigit():
        raise ValueError("payload must be 8 digits")
    cs = 82 + sum(int(c) for c in payload8)
    return b"\x02" + ("%s%02d" % (payload8, cs)).encode("ascii") + b"\x05"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=38400)
    p.add_argument("key", choices=sorted(KEYS))
    p.add_argument("--repeat", type=int, default=1)
    args = p.parse_args()
    pkt = frame(KEYS[args.key])
    print("sending %s  %s" % (args.key, pkt.hex(" ")))
    ser = serial.Serial(args.port, args.baud, timeout=0.2)
    try:
        for i in range(args.repeat):
            ser.write(pkt)
            ser.flush()
            time.sleep(0.15)
    finally:
        ser.close()
    print("done")


if __name__ == "__main__":
    main()
