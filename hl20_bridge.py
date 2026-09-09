#!/usr/bin/env python3
"""HL-20 serial <-> local countdown clock. 38400 8N1.

Listens on USB RX (white). Sends 0 / Total on USB TX (yellow).

  python hl20_bridge.py --port COM5 --pens "Pen 1:500,Pen 2:450,Pen 3:300"
  Open http://127.0.0.1:8765/
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import serial
except ImportError:
    print("pip install pyserial")
    sys.exit(1)

HOST = "0.0.0.0"
PORT = 8765
LINE_RE = re.compile(
    r"(?P<kind>TOTAL|PARTIAL)\s+WEIGHT(?P<blocked>\s+BLOCKED)?"
    r".{0,48}?Kg\s*(?P<kg>-?\d+)",
    re.IGNORECASE,
)

lock = threading.RLock()
ser_lock = threading.Lock()
ser_port = None
KEYS = {
    "zero": "00001000",
    "total": "00400000",
}
state = {
    "totalKg": None,
    "partialKg": None,
    "liveKg": None,
    "liveKind": None,
    "blocked": False,
    "source": "hl20",
    "error": "no-signal",
    "ts": 0,
    "targetKg": 0.0,
    "startKg": None,
    "dumpedKg": 0.0,
    "remainingKg": 0.0,
    "penIndex": 0,
    "penName": "",
    "stable": False,
    "autoArmed": False,
    "lastAdvanceTs": 0.0,
    "lastChangeTs": 0.0,
    "lastLive": None,
    "packets": 0,
    "hits": 0,
    "log": [],
}
pens = []


def key_frame(payload8):
    cs = 82 + sum(int(c) for c in payload8)
    return b"\x02" + ("%s%02d" % (payload8, cs)).encode("ascii") + b"\x05"


def send_key(name):
    payload = KEYS.get(name)
    if not payload:
        return "unknown key"
    pkt = key_frame(payload)
    with ser_lock:
        if ser_port is None:
            log("send %s failed: serial not open" % name)
            return "serial not open"
        try:
            ser_port.write(pkt)
            ser_port.flush()
        except Exception as exc:
            log("send %s failed: %s" % (name, exc))
            return str(exc)
    log("sent %s" % name)
    return None


def log(msg):
    line = time.strftime("%H:%M:%S") + "  " + msg
    print(line, flush=True)
    with lock:
        state["log"] = (state["log"] + [line])[-30:]


def snapshot():
    with lock:
        return {
            "ok": True,
            "kg": state["remainingKg"] if state["autoArmed"] else state["liveKg"],
            "totalKg": state["totalKg"],
            "partialKg": state["partialKg"],
            "liveKg": state["liveKg"],
            "liveKind": state["liveKind"],
            "targetKg": state["targetKg"],
            "startKg": state["startKg"],
            "dumpedKg": round(state["dumpedKg"], 1),
            "remainingKg": round(state["remainingKg"], 1),
            "penIndex": state["penIndex"],
            "penName": state["penName"],
            "penCount": len(pens),
            "blocked": state["blocked"],
            "stable": state["stable"],
            "autoArmed": state["autoArmed"],
            "source": state["source"],
            "error": state["error"],
            "ts": state["ts"],
            "packets": state["packets"],
            "log": list(state["log"]),
            "pens": pens,
        }


def apply_live(kind, kg, blocked):
    now = time.time()
    should_advance = False
    announce = False
    live = float(kg)
    with lock:
        state["ts"] = int(now)
        state["blocked"] = blocked
        state["error"] = None
        state["packets"] += 1
        if kind == "TOTAL":
            state["totalKg"] = live
        else:
            state["partialKg"] = live
        prev = state["lastLive"]
        state["liveKg"] = live
        state["liveKind"] = kind
        if prev is None or abs(prev - live) >= 1:
            state["lastChangeTs"] = now
            state["lastLive"] = live
            announce = True
        state["stable"] = (now - state["lastChangeTs"]) >= 1.2
        if state["autoArmed"] and state["startKg"] is not None:
            dumped = abs(state["startKg"] - live)
            state["dumpedKg"] = dumped
            state["remainingKg"] = state["targetKg"] - dumped
        elif state["autoArmed"]:
            state["remainingKg"] = state["targetKg"]
            state["dumpedKg"] = 0.0
        should_advance = (
            state["autoArmed"]
            and state["targetKg"] > 0
            and state["remainingKg"] <= 0
            and state["dumpedKg"] >= max(10.0, state["targetKg"] * 0.5)
            and state["stable"]
            and (now - state["lastAdvanceTs"]) > 4
        )
        pkt = state["packets"]
        armed = state["autoArmed"]
        rem = state["remainingKg"]
    if announce or pkt == 1:
        extra = "  remain %.0f" % rem if armed else ""
        log("%s %.0f kg%s" % (kind, live, extra))
    if should_advance:
        finish_pen(True)


def start_pen(index=None):
    with lock:
        if index is not None:
            state["penIndex"] = max(0, min(index, max(0, len(pens) - 1)))
        if not pens:
            state["penName"] = "Pen"
            if state["targetKg"] <= 0:
                state["targetKg"] = 500.0
        else:
            p = pens[state["penIndex"]]
            state["penName"] = p["name"]
            state["targetKg"] = float(p["kg"])
        state["startKg"] = state["liveKg"]
        state["dumpedKg"] = 0.0
        state["remainingKg"] = state["targetKg"]
        state["autoArmed"] = True
        state["lastAdvanceTs"] = time.time()
        tgt = state["targetKg"]
        name = state["penName"]
        start = state["startKg"]
        kind = state["liveKind"]
    log("START %s  target %.0f kg  %s %s" % (name, tgt, kind or "wagon", start))


def finish_pen(auto):
    with lock:
        if not state["autoArmed"]:
            return
        dumped = state["dumpedKg"]
        name = state["penName"]
        idx = state["penIndex"]
        if idx < len(pens):
            pens[idx]["actualKg"] = round(dumped, 1)
        state["autoArmed"] = False
        state["lastAdvanceTs"] = time.time()
        nxt = idx + 1
    log("DONE %s  actual %.0f kg  (%s)" % (name, dumped, "auto" if auto else "manual"))
    if nxt < len(pens):
        start_pen(nxt)
    else:
        log("Load finished")


def parse_pens(spec):
    out = []
    if not spec.strip():
        return out
    for i, part in enumerate(spec.split(",")):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, kg = part.rsplit(":", 1)
        else:
            name, kg = "Pen %d" % (i + 1), part
        out.append({"name": name.strip(), "kg": float(kg), "actualKg": None})
    return out


PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Feedout clock</title>
<style>
html,body{margin:0;background:#111;color:#f3f3f3;font-family:system-ui,sans-serif}
body{padding:16px}
.pen{opacity:.75;text-transform:uppercase;font-size:14px;letter-spacing:.04em}
.remain{font-size:min(28vw,160px);font-weight:650;line-height:1;font-variant-numeric:tabular-nums}
.low{color:#e9c46a}.done{color:#95d5b2}
.meta{margin:8px 0 18px;opacity:.8}
.row{display:flex;gap:10px;flex-wrap:wrap}
button{font-size:1.15rem;padding:14px 18px;border:0;border-radius:14px;background:#2d6a4f;color:#fff}
.ghost{background:#333}
.here{font-weight:700}
.err{color:#e9c46a}
</style>
</head>
<body>
<p class="pen" id="pen">clock</p>
<div class="remain" id="kg">---</div>
<p class="meta" id="meta">js starting</p>
<div class="row">
<button id="bStart">Start pen</button>
<button class="ghost" id="bDone">Record and next</button>
<button class="ghost" id="bPrev">Back</button>
</div>
<div class="row" style="margin-top:10px">
<button class="ghost" id="bZero">Zero on mixer</button>
<button class="ghost" id="bTotal">Total on mixer</button>
</div>
<ul id="pens"></ul>
<pre class="meta" id="log"></pre>
<script>
document.getElementById('meta').textContent = 'js ok, fetching weight';
async function tick(){
  try{
    var ctrl = new AbortController();
    var t = setTimeout(function(){ ctrl.abort(); }, 2500);
    var r = await fetch('/weight?t=' + Date.now(), {cache:'no-store', signal: ctrl.signal});
    clearTimeout(t);
    var j = await r.json();
    var rem = j.autoArmed ? j.remainingKg : j.liveKg;
    var el = document.getElementById('kg');
    el.textContent = (rem === null || rem === undefined) ? '---' : Math.round(rem);
    el.className = 'remain' + (j.autoArmed && rem<=0 ? ' done' : (j.autoArmed && rem<=40 ? ' low' : ''));
    document.getElementById('pen').textContent =
      (j.penName || 'Pen') + (j.penCount ? ('  ' + (j.penIndex+1) + '/' + j.penCount) : '');
    var bits = [];
    if (j.autoArmed) bits.push('target ' + Math.round(j.targetKg) + ' dumped ' + Math.round(j.dumpedKg));
    bits.push((j.liveKind || 'wagon') + ' ' + (j.liveKg==null ? '-' : Math.round(j.liveKg)));
    bits.push(j.stable ? 'steady' : 'moving');
    bits.push((j.packets||0) + ' pkts');
    if (j.error) bits.push(j.error);
    document.getElementById('meta').textContent = bits.join(' | ');
    document.getElementById('meta').className = 'meta' + (j.error ? ' err' : '');
    document.getElementById('pens').innerHTML = (j.pens||[]).map(function(p,i){
      return '<li class="' + (i===j.penIndex ? 'here' : '') + '">' + p.name + '  ' + Math.round(p.kg) + ' kg'
        + (p.actualKg!=null ? (' -> ' + p.actualKg) : '') + '</li>';
    }).join('');
    document.getElementById('log').textContent = (j.log||[]).slice(-8).join(' / ');
  }catch(e){
    document.getElementById('meta').textContent = 'no /weight (' + e + ')';
    document.getElementById('meta').className = 'meta err';
  }
}
function post(path){ fetch(path,{method:'POST'}).then(tick); }
document.getElementById('bStart').onclick = function(){ post('/start'); };
document.getElementById('bDone').onclick = function(){ post('/done'); };
document.getElementById('bPrev').onclick = function(){ post('/prev'); };
document.getElementById('bZero').onclick = function(){ post('/zero'); };
document.getElementById('bTotal').onclick = function(){ post('/total'); };
setInterval(tick, 300);
tick();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("http  " + (fmt % args), flush=True)

    def _send(self, code, ctype, raw):
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/clock"):
            self._send(200, "text/html; charset=utf-8", PAGE)
            return
        if path.startswith("/weight"):
            with lock:
                state["hits"] += 1
            try:
                body = json.dumps(snapshot())
            except Exception as exc:
                body = json.dumps({"ok": False, "error": str(exc), "liveKg": None})
            self._send(200, "application/json", body)
            return
        self._send(404, "application/json", json.dumps({"error": "not found"}))

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = {}
        if raw:
            try:
                body = json.loads(raw.decode())
            except Exception:
                body = {}
        qs = parse_qs(urlparse(self.path).query)
        if path == "/start":
            idx = body.get("penIndex", (qs.get("pen") or [None])[0])
            start_pen(int(idx) if idx is not None and str(idx).isdigit() else None)
        elif path == "/done":
            finish_pen(False)
        elif path == "/prev":
            with lock:
                nxt = max(0, state["penIndex"] - 1)
            start_pen(nxt)
        elif path == "/target":
            kg = float(body.get("kg") or (qs.get("kg") or [0])[0] or 0)
            with lock:
                state["targetKg"] = kg
            start_pen()
        elif path == "/zero":
            send_key("zero")
        elif path == "/total":
            send_key("total")
        else:
            self._send(404, "application/json", json.dumps({"error": "not found"}))
            return
        self._send(200, "application/json", json.dumps(snapshot()))


def reader(port, baud):
    global ser_port
    try:
        ser = serial.Serial(port, baud, timeout=0.2)
    except Exception as exc:
        log("cannot open %s: %s" % (port, exc))
        with lock:
            state["error"] = str(exc)
        return
    with ser_lock:
        ser_port = ser
    buf = ""
    log("listening %s %s" % (port, baud))
    while True:
        try:
            with ser_lock:
                chunk = ser.read(256)
        except Exception as exc:
            log("serial error: %s" % exc)
            time.sleep(0.5)
            continue
        if not chunk:
            with lock:
                if state["ts"] and time.time() - state["ts"] > 3:
                    state["error"] = "stale"
            continue
        buf += chunk.decode("latin-1", errors="replace")
        if len(buf) > 4000:
            buf = buf[-2000:]
        last = None
        for m in LINE_RE.finditer(buf):
            rec = (m.group("kind").upper(), int(m.group("kg")), bool(m.group("blocked")))
            if rec == last:
                continue
            last = rec
            apply_live(*rec)
        if len(buf) > 800:
            buf = buf[-400:]


def main():
    global pens
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=38400)
    p.add_argument("--pens", default="")
    p.add_argument("--http", type=int, default=PORT)
    args = p.parse_args()
    pens = parse_pens(args.pens)
    if pens:
        with lock:
            state["penName"] = pens[0]["name"]
            state["targetKg"] = pens[0]["kg"]
        log("pens: " + ", ".join("%s %.0f" % (x["name"], x["kg"]) for x in pens))

    httpd = ThreadingHTTPServer((HOST, args.http), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=reader, args=(args.port, args.baud), daemon=True).start()
    print("Clock  http://127.0.0.1:%s/" % args.http, flush=True)
    print("If the page says connecting, open this instead:", flush=True)
    print("  http://127.0.0.1:%s/weight" % args.http, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("stop")


if __name__ == "__main__":
    main()
