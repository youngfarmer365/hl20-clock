#!/usr/bin/env python3
"""HL-20 serial -> local countdown clock for feedout.

  python hl20_bridge.py --port COM5
  Open http://127.0.0.1:8765/
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
KEYS = {"zero": "00001000", "total": "00400000"}
RAW_KEYS = {"enter": bytes.fromhex("023030303030303830383a05")}
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
    "lastPacketTs": 0.0,
    "lastLive": None,
    "packets": 0,
    "log": [],
    "mixName": "",
    "ingredients": [],
}
mixes = []
pens: list[dict] = []

SB_URL = "https://bjzvjmaiyuvjmyhozbpq.supabase.co"
SB_ANON = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJqenZqbWFpeXV2am15aG96YnBxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODUyNDE3MjgsImV4cCI6MjEwMDgxNzcyOH0."
    "AkB9U_QWODouWtTAJr10yaz6Qj9-Deki4NMLxhtHb3o"
)


def jwt_sub(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return data.get("sub")
    except Exception:
        return None


def sb_json(token: str, method: str, path: str, query: dict | None = None, body=None, timeout: float = 8):
    url = SB_URL + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    raw_body = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=raw_body, method=method)
    req.add_header("apikey", SB_ANON)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    if raw_body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else None
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("%s %s %s" % (exc.code, path, err[:180]))


def _in_filter(ids: list[str]) -> str:
    return "in.(%s)" % ",".join(ids)


def diet_lines_py(token: str, diet_id: str) -> list[dict]:
    if not diet_id:
        return []
    rows = sb_json(token, "GET", "/rest/v1/diet_ingredients", {
        "diet_id": "eq.%s" % diet_id,
        "select": "percent,sort_order,ingredient_id",
        "order": "sort_order.asc",
    }, timeout=6) or []
    ids = [r["ingredient_id"] for r in rows if r.get("ingredient_id")]
    names = {}
    if ids:
        ings = sb_json(token, "GET", "/rest/v1/ingredients", {
            "id": _in_filter(ids),
            "select": "id,name",
        }, timeout=6) or []
        names = {x["id"]: x["name"] for x in ings}
    out = []
    for r in rows:
        pct = float(r.get("percent") or 0)
        if pct <= 0:
            continue
        out.append({
            "id": r["ingredient_id"],
            "name": names.get(r["ingredient_id"], "Feed"),
            "percent": pct,
        })
    return out


def fm_snapshot(token: str) -> dict:
    uid = jwt_sub(token)
    if not uid:
        raise RuntimeError("bad login token")
    members = sb_json(token, "GET", "/rest/v1/farm_members", {
        "user_id": "eq.%s" % uid,
        "select": "farm_id",
        "limit": "1",
    }, timeout=6) or []
    if not members:
        raise RuntimeError("no farm on this login")
    farm_id = members[0]["farm_id"]

    try:
        rpc = sb_json(token, "POST", "/rest/v1/rpc/mixer_clock_snapshot", body={}, timeout=6)
        if isinstance(rpc, dict) and (rpc.get("loads") is not None or rpc.get("premixes") is not None):
            loads = rpc.get("loads") or []
            for load in loads:
                load.setdefault("program_id", load.get("programId"))
                load.setdefault("pens", [])
                load.setdefault("recipe", [])
            premixes = rpc.get("premixes") or []
            for p in premixes:
                p.setdefault("dietId", p.get("diet_id") or p.get("id"))
                p.setdefault("batchKg", p.get("batch_kg") or 500)
                p.setdefault("lines", [])
            log("farm RPC %s loads %s premixes" % (len(loads), len(premixes)))
            return {"farmId": rpc.get("farmId") or farm_id, "loads": loads, "premixes": premixes}
    except Exception as exc:
        log("farm RPC skip: %s" % exc)

    loads_rows = sb_json(token, "GET", "/rest/v1/feed_loads", {
        "farm_id": "eq.%s" % farm_id,
        "select": "id,name,program_id",
        "order": "created_at.desc",
    }, timeout=8) or []
    load_ids = [r["id"] for r in loads_rows]
    pens_by_load: dict[str, list] = {}
    if load_ids:
        lp = sb_json(token, "GET", "/rest/v1/feed_load_pens", {
            "load_id": _in_filter(load_ids),
            "select": "load_id,pen_id,daily_amount_kg,sort_order",
            "order": "sort_order.asc",
        }, timeout=8) or []
        pen_ids = list({r["pen_id"] for r in lp if r.get("pen_id")})
        names = {}
        if pen_ids:
            pr = sb_json(token, "GET", "/rest/v1/pens", {
                "id": _in_filter(pen_ids),
                "select": "id,name",
            }, timeout=6) or []
            names = {p["id"]: p["name"] for p in pr}
        for r in lp:
            pens_by_load.setdefault(r["load_id"], []).append({
                "id": r["pen_id"],
                "name": names.get(r["pen_id"], "Pen"),
                "kg": float(r.get("daily_amount_kg") or 0),
            })
    loads = [{
        "id": r["id"],
        "name": r["name"],
        "program_id": r.get("program_id"),
        "pens": pens_by_load.get(r["id"], []),
        "recipe": [],
    } for r in loads_rows]

    ings = sb_json(token, "GET", "/rest/v1/ingredients", {
        "farm_id": "eq.%s" % farm_id,
        "select": "id,name,premix_diet_id",
    }, timeout=6) or []
    try:
        diets = sb_json(token, "GET", "/rest/v1/diets", {
            "farm_id": "eq.%s" % farm_id,
            "select": "id,name,diet_type,is_active",
            "order": "name.asc",
        }, timeout=6) or []
    except Exception:
        diets = sb_json(token, "GET", "/rest/v1/diets", {
            "farm_id": "eq.%s" % farm_id,
            "select": "id,name",
            "order": "name.asc",
        }, timeout=6) or []

    typed = [d for d in diets if str(d.get("diet_type") or "").lower() == "premix" and d.get("is_active") is not False]
    linked = {i.get("premix_diet_id") for i in ings if i.get("premix_diet_id")}
    if not typed:
        typed = [d for d in diets if d.get("id") in linked]
    if not typed:
        typed = [d for d in diets if re.search(r"premix|pre mix", d.get("name") or "", re.I)]
    seen = {}
    premixes = []
    for d in typed:
        did = d.get("id")
        if not did or did in seen:
            continue
        seen[did] = True
        as_ing = next((i for i in ings if i.get("premix_diet_id") == did), None)
        premixes.append({
            "dietId": did,
            "name": d.get("name") or "Premix",
            "ingredientId": as_ing["id"] if as_ing else None,
            "batchKg": float(d.get("batch_kg") or 500),
            "lines": [],
        })
    for i in ings:
        pid = i.get("premix_diet_id")
        if pid and pid not in seen:
            seen[pid] = True
            premixes.append({
                "dietId": pid,
                "name": i.get("name") or "Premix",
                "ingredientId": i.get("id"),
                "batchKg": 500,
                "lines": [],
            })
    log("farm pull %s loads %s premixes" % (len(loads), len(premixes)))
    return {"farmId": farm_id, "loads": loads, "premixes": premixes}


def key_frame(payload8: str) -> bytes:
    cs = 82 + sum(int(c) for c in payload8)
    return b"\x02" + ("%s%02d" % (payload8, cs)).encode("ascii") + b"\x05"


def send_key(name: str, quiet: bool = False):
    pkt = RAW_KEYS.get(name)
    if pkt is None:
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
    if not quiet:
        log("sent %s" % name)
    return None


def zero_total() -> None:
    def run():
        log("ZERO TOTAL  hold Total 3s then Enter")
        for _ in range(25):
            if send_key("total", quiet=True):
                return
            time.sleep(0.12)
        time.sleep(0.2)
        for _ in range(3):
            send_key("enter", quiet=True)
            time.sleep(0.15)
        log("ZERO TOTAL  done")

    threading.Thread(target=run, daemon=True).start()


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')}  {msg}"
    print(line, flush=True)
    with lock:
        state["log"] = (state["log"] + [line])[-30:]


def snapshot() -> dict:
    with lock:
        return {
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
            "silentSec": round(max(0.0, time.time() - (state["lastPacketTs"] or 0)), 1) if state["lastPacketTs"] else None,
            "com": "open" if ser_port else "lost",
            "packets": state["packets"],
            "log": list(state["log"]),
            "pens": pens,
            "mixName": state["mixName"],
            "ingredients": list(state["ingredients"]),
            "mixes": mixes,
        }


def apply_live(kind: str, kg: int, blocked: bool) -> None:
    now = time.time()
    should_advance = False
    announce = False
    with lock:
        state["ts"] = int(now)
        state["lastPacketTs"] = now
        state["blocked"] = blocked
        state["error"] = None
        state["packets"] += 1
        if kind == "TOTAL":
            state["totalKg"] = float(kg)
        else:
            state["partialKg"] = float(kg)
        live = float(kg)
        live_kind = kind
        prev = state["lastLive"]
        state["liveKg"] = live
        state["liveKind"] = live_kind
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
        extra = f"  remain {rem:.0f}" if armed else ""
        log(f"{live_kind} {live:.0f} kg{extra}")
    if should_advance:
        finish_pen(auto=True)


def start_pen(index: int | None = None) -> None:
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
    log(f"START {name}  target {tgt:.0f} kg  {kind or 'wagon'} {start}")


def finish_pen(auto: bool) -> None:
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
    why = "auto" if auto else "manual"
    log(f"DONE {name}  actual {dumped:.0f} kg  ({why})")
    if nxt < len(pens):
        start_pen(nxt)
    else:
        log("Load finished")


def default_mixes() -> list[dict]:
    return [
        {
            "name": "High yield",
            "ingredients": [
                {"name": "Grass silage", "kg": 1800},
                {"name": "Maize silage", "kg": 900},
                {"name": "Blend", "kg": 420},
                {"name": "Straw", "kg": 80},
            ],
            "pens": [
                {"name": "Pen 1", "kg": 300, "actualKg": None},
                {"name": "Pen 2", "kg": 450, "actualKg": None},
                {"name": "Pen 3", "kg": 500, "actualKg": None},
            ],
        },
        {
            "name": "Far off dry",
            "ingredients": [
                {"name": "Grass silage", "kg": 1200},
                {"name": "Straw", "kg": 200},
                {"name": "Dry cow mineral", "kg": 25},
            ],
            "pens": [{"name": "Dry cows", "kg": 400, "actualKg": None}],
        },
    ]


def apply_load(payload: dict) -> None:
    global pens
    name = str(payload.get("name") or "Load")
    raw_pens = payload.get("pens") or []
    raw_ing = payload.get("ingredients") or []
    pens = [
        {"name": str(p.get("name") or "Pen"), "kg": float(p.get("kg") or 0), "actualKg": None}
        for p in raw_pens
        if float(p.get("kg") or 0) > 0
    ]
    with lock:
        state["mixName"] = name
        state["ingredients"] = [
            {"name": str(r.get("name") or "Feed"), "kg": float(r.get("kg") or 0)}
            for r in raw_ing
        ]
        state["penIndex"] = 0
        state["autoArmed"] = False
        if pens:
            state["penName"] = pens[0]["name"]
            state["targetKg"] = float(pens[0]["kg"])
    log("LOAD %s" % name)


def select_mix(index: int) -> None:
    global pens
    if not mixes:
        return
    i = max(0, min(index, len(mixes) - 1))
    mix = mixes[i]
    pens = [
        {"name": p["name"], "kg": float(p["kg"]), "actualKg": None}
        for p in mix.get("pens") or []
    ]
    with lock:
        state["mixName"] = mix["name"]
        state["ingredients"] = [
            {"name": r["name"], "kg": float(r["kg"])}
            for r in mix.get("ingredients") or []
        ]
        state["penIndex"] = 0
        state["autoArmed"] = False
        if pens:
            state["penName"] = pens[0]["name"]
            state["targetKg"] = float(pens[0]["kg"])
    log("MIX %s" % mix["name"])


def parse_pens(spec: str) -> list[dict]:
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
            name, kg = f"Pen {i + 1}", part
        out.append({"name": name.strip(), "kg": float(kg), "actualKg": None})
    return out


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="MixerClock">
<meta name="theme-color" content="#070707">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon.svg">
<title>Mixer Clock</title>
<link href="https://fonts.cdnfonts.com/css/ds-digital" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:#070707;color:#f4f4f4;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
body{min-height:100dvh;padding:max(10px,env(safe-area-inset-top)) 12px max(12px,env(safe-area-inset-bottom));display:flex;flex-direction:column;gap:10px}
.screen{display:none;flex:1;min-height:0;flex-direction:column;gap:10px}
.screen.on{display:flex}
.top{display:flex;justify-content:space-between;align-items:center;gap:10px}
.h{font-size:clamp(18px,2.6vw,26px);font-weight:800;letter-spacing:.08em;text-transform:uppercase}
.sub{color:#9aa0a6;font-weight:600;font-size:13px}
.live{font-family:"DS-Digital","DS Digital",monospace;color:#ffe14a;font-size:18px}
.kg{font-family:"DS-Digital","DS Digital",monospace;font-size:clamp(92px,22vw,220px);line-height:.82;color:#ffe14a;text-align:center;text-shadow:0 0 22px rgba(255,225,74,.18)}
.kg.done{color:#6dff7a}.kg.over{color:#ff4d4d}
.ingname{text-align:center;font-size:clamp(22px,4vw,40px);font-weight:800}
.hint{text-align:center;font-weight:700;color:#cfd3d8}
.bar{height:18px;border:2px solid #333;border-radius:999px;background:#151515;overflow:hidden}
.bar>i{display:block;height:100%;width:0;background:#ffe14a}
.bar.over>i{background:#ff4d4d}
.list{display:flex;flex-direction:column;gap:8px;flex:1;overflow:auto}
.card{display:flex;justify-content:space-between;align-items:center;gap:12px;width:100%;text-align:left;background:#121212;color:#fff;border:2px solid #2a2a2a;border-radius:14px;padding:16px;font-weight:700}
.card b{display:block;font-size:20px}
.card span{display:block;margin-top:4px;color:#9aa0a6;font-size:13px}
.card.done{border-color:#1f8a3b;background:#0d1a11}
.card.done b{color:#8dff9a}
.row{display:flex;gap:8px;flex-wrap:wrap}
button{appearance:none;font:700 17px/1.1 ui-sans-serif,system-ui,sans-serif;min-height:52px;padding:12px 14px;border-radius:12px;border:2px solid #ececec;background:#fff;color:#111;flex:1}
button.on{background:#fff;color:#111;border-color:#ececec}
button.ghost{background:#111;color:#fff;border-color:#3a3a3a}
button.good{background:#148a3a;color:#fff;border-color:#1fbb52}
button.danger{background:#c1121f;color:#fff;border-color:#ff6b6b;flex:0 0 30%;margin-left:auto}
.chip{flex:0 0 auto;min-height:42px;background:#111;color:#fff;border-color:#444}
input{width:100%;min-height:54px;border:2px solid #444;border-radius:12px;background:#111;color:#fff;font-size:22px;font-weight:800;text-align:center}
label{font-weight:700;color:#cfd3d8}
.warn{background:#2a1608;border:2px solid #c2410c;border-radius:12px;padding:12px;font-weight:700}
.tabs{display:flex;gap:8px}
.dot{width:8px;height:8px;border-radius:50%;background:#555;display:inline-block;margin-right:6px}
.dot.on{background:#6dff7a}
body.flash{background:#3a2a00}
body.flash-over{background:#4a0000}
#fault{display:none;background:#4a0000;color:#fff;font-weight:800;text-align:center;padding:8px;border-radius:10px}
#fault.on{display:block}
.ord{width:56px;min-height:44px;font-size:18px;flex:0 0 auto}
.cardwrap{display:flex;gap:8px;align-items:stretch}
@media (orientation:landscape){
  #sFill.on,#sClock.on{display:grid;grid-template-columns:1fr 220px;grid-template-rows:auto 1fr auto auto;gap:8px 14px;flex:1}
  .stage{grid-column:1;grid-row:2}
  .panel{grid-column:2;grid-row:1/4}
  .actions{grid-column:1/-1;grid-row:4}
  .kg{font-size:clamp(110px,28vh,240px)}
}
</style>
</head>
<body>
<div id="fault"></div>
<section class="screen on" id="sLogin">
  <div class="h">Mixer Clock</div>
  <p class="sub">Farm Manager login. After Refresh, Share → Add to Home Screen. Recipes stay on the iPad if the page is killed.</p>
  <form id="loginForm" action="#" method="post">
    <label>Email</label><input id="email" type="email" autocomplete="username" required>
    <label>Password</label><input id="pass" type="password" autocomplete="current-password" required>
    <p class="sub" id="loginErr" style="color:#ff6b6b"></p>
    <div class="row" style="margin-top:12px"><button type="submit" id="bLogin">Log in</button></div>
  </form>
</section>

<section class="screen" id="sHome">
  <div class="top">
    <div>
      <div class="h">Yard <span style="opacity:.35;font-size:11px;letter-spacing:0">v7</span></div>
      <div class="sub" id="syncTag"><span class="dot" id="netDot"></span>Never synced</div>
    </div>
    <div class="row" style="flex:0 0 auto">
      <button class="ghost" id="bRefresh" style="flex:0 0 auto">Refresh</button>
      <button class="ghost" id="bLogout" style="flex:0 0 auto">Log out</button>
    </div>
  </div>
  <div class="top"><div class="live" id="homeLive">--- kg</div><div class="sub" id="queueTag"></div></div>
  <div class="tabs">
    <button id="tabLoads">Loads</button>
    <button class="ghost" id="tabPremix">Premixes</button>
  </div>
  <div class="list" id="listBox"></div>
</section>

<section class="screen" id="sBuffer">
  <div class="h">Buffer</div>
  <p class="sub" id="bufName"></p>
  <p id="bufPens"></p>
  <label>Extra / short kg</label>
  <input id="bufKg" type="number" value="0">
  <div class="row" id="bufChips"></div>
  <p id="bufFill"></p>
  <div class="row"><button class="ghost" id="bBufBack">Back</button><button class="good" id="bBufGo">Start fill</button></div>
</section>

<section class="screen" id="sAmount">
  <div class="h">Premix amount</div>
  <p class="sub" id="amtName"></p>
  <label>Kg to mix</label>
  <input id="amtKg" type="number" value="500">
  <div class="row" id="amtChips"></div>
  <div class="list" id="amtLines"></div>
  <div class="row"><button class="ghost" id="bAmtBack">Back</button><button class="good" id="bAmtGo">Start fill</button></div>
</section>

<section class="screen" id="sFill">
  <div class="top">
    <div class="h" id="fillTitle">FILL</div>
    <div class="sub" id="fillIdx"></div>
  </div>
  <div class="stage">
    <div class="kg" id="fillKg">---</div>
    <div class="ingname" id="fillName">Zero first</div>
    <div class="hint" id="fillHint"></div>
    <div class="bar" id="fillBar"><i id="fillBarI"></i></div>
  </div>
  <div class="panel warn" id="rebalBox" style="display:none">
    <div id="rebalText"></div>
    <div class="row" style="margin-top:8px">
      <button class="ghost" id="bRebalNo">Keep plan</button>
      <button class="good" id="bRebalYes">Rebalance</button>
    </div>
  </div>
  <div class="actions row">
    <button class="ghost" id="bFillZero">Zero</button>
    <button class="good" id="bFillNext">Loaded</button>
    <button class="ghost" id="bFillBack">Back</button>
    <button class="danger" id="bFillZT">Zero total</button>
  </div>
  <div class="row"><button class="good" id="bAfterFill" style="display:none">Continue</button></div>
</section>

<section class="screen" id="sClock">
  <div class="top"><div class="h" id="pen">FEEDOUT</div><div class="sub" id="idx"></div></div>
  <div class="stage">
    <div class="kg" id="kg">---</div>
    <div class="hint" id="meta">connecting</div>
    <div class="bar" id="penBar"><i id="penBarI"></i></div>
  </div>
  <div class="actions row">
    <button class="good" id="bStart">Start pen</button>
    <button class="ghost" id="bDone">Next</button>
    <button class="ghost" id="bZero">Zero</button>
    <button class="danger" id="bZeroTotal">Zero total</button>
  </div>
  <div class="row"><button class="ghost" id="bClockHome">Yard</button><button class="ghost" id="bPrev">Back pen</button><button class="ghost" id="bTotal">Total</button><button class="good" id="bFinish">Finish load</button></div>
</section>

<section class="screen" id="sBay">
  <div class="h">Empty to bay</div>
  <p id="bayText"></p>
  <div class="row"><button class="good" id="bBayDone">Completed — add to stock</button></div>
  <p class="sub" id="bayMsg"></p>
  <div class="row"><button class="ghost" id="bBayHome">Yard</button></div>
</section>

<script>
var SB_URL='https://bjzvjmaiyuvjmyhozbpq.supabase.co';
var SB_KEY='eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJqenZqbWFpeXV2am15aG96YnBxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODUyNDE3MjgsImV4cCI6MjEwMDgxNzcyOH0.AkB9U_QWODouWtTAJr10yaz6Qj9-Deki4NMLxhtHb3o';
var sb=supabase.createClient(SB_URL,SB_KEY);
var farmId=null,tab='loads',loads=[],premixes=[],job=null,online=false,lastSync=null,lastLive=null,wake=null,hitTarget=false,hitOver=false,fetchFails=0,loggedOut=false,busy=false,refreshGen=0,lastErr='';
var CACHE='mc_cache_v7', QUEUE='mc_queue';
function today(){var d=new Date();return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');}
function doneKey(){return 'mc_done_'+today();}
function show(id){['sLogin','sHome','sBuffer','sAmount','sFill','sClock','sBay'].forEach(function(s){document.getElementById(s).className='screen'+(s===id?' on':'');}); if(id==='sFill'||id==='sClock') armWake();}
async function armWake(){
 try{ if(navigator.wakeLock) wake=await navigator.wakeLock.request('screen'); }catch(e){}
}
document.addEventListener('visibilitychange', function(){ if(document.visibilityState==='visible') armWake(); });
function beep(hi){
 try{
  var ctx=new (window.AudioContext||window.webkitAudioContext)();
  var o=ctx.createOscillator(), g=ctx.createGain();
  o.type='square'; o.frequency.value=hi?920:440; o.connect(g); g.connect(ctx.destination);
  g.gain.value=0.12; o.start(); o.stop(ctx.currentTime+0.16);
 }catch(e){}
}
function flash(over){
 document.body.classList.add(over?'flash-over':'flash');
 setTimeout(function(){ document.body.classList.remove('flash','flash-over'); }, 350);
}
function holdBtn(id, path){
 var btn=document.getElementById(id); if(!btn) return;
 var t=null;
 function go(){ post(path); }
 function down(e){ e.preventDefault(); go(); t=setInterval(go,150); }
 function up(){ if(t) clearInterval(t); t=null; }
 btn.addEventListener('pointerdown', down);
 ['pointerup','pointercancel','pointerleave'].forEach(function(ev){ btn.addEventListener(ev, up); });
}
function post(path,body){return fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});}
function loadCache(){try{return JSON.parse(localStorage.getItem(CACHE)||'null');}catch(e){return null;}}
function saveCache(){localStorage.setItem(CACHE,JSON.stringify({farmId:farmId,lastSync:lastSync,loads:loads,premixes:premixes}));}
function queue(){try{return JSON.parse(localStorage.getItem(QUEUE)||'[]');}catch(e){return [];}}
function setQueue(q){localStorage.setItem(QUEUE,JSON.stringify(q));}
function markDone(id){var a=[];try{a=JSON.parse(localStorage.getItem(doneKey())||'[]');}catch(e){} if(a.indexOf(id)<0) a.push(id); localStorage.setItem(doneKey(),JSON.stringify(a));}
function isDone(id){try{return JSON.parse(localStorage.getItem(doneKey())||'[]').indexOf(id)>=0;}catch(e){return false;}}
function fmtWhen(ts){if(!ts) return 'Never synced'; var d=new Date(ts); return 'Farm Manager '+d.toLocaleString();}
function withTimeout(p,ms){
  return new Promise(function(res,rej){
    var done=false;
    var t=setTimeout(function(){ if(!done){ done=true; rej(new Error('timeout')); } }, ms||8000);
    Promise.resolve(p).then(
      function(v){ if(!done){ done=true; clearTimeout(t); res(v); } },
      function(e){ if(!done){ done=true; clearTimeout(t); rej(e); } }
    );
  });
}

function programmeDay(prog){
 if(!prog||!prog.start_date) return 0;
 function idx(d){var a=new Date();a.setHours(0,0,0,0);return Math.floor((a-new Date(d+'T00:00:00'))/86400000);}
 return Math.max(0,idx(prog.start_date)-Number(prog.pause_days||0)-(prog.paused_on?Math.max(0,idx(prog.paused_on)):0));
}
function resolveBlend(day,phases){
 var s=(phases||[]).slice().sort(function(a,b){return a.sort_order-b.sort_order;});
 if(!s.length) return {from:null,to:null,fs:1,ts:0};
 var c=0;
 for(var i=0;i<s.length;i++){
  var p=s[i],n=s[i+1];
  if(day<c+Number(p.steady_days||0)) return {from:p.diet_id,to:p.diet_id,fs:1,ts:0};
  c+=Number(p.steady_days||0);
  if(n&&Number(p.transition_days||0)>0){
   if(day<c+Number(p.transition_days)){var ts=(day-c+1)/Number(p.transition_days);return {from:p.diet_id,to:n.diet_id,fs:1-ts,ts:ts};}
   c+=Number(p.transition_days);
  }
 }
 var last=s[s.length-1]; return {from:last.diet_id,to:last.diet_id,fs:1,ts:0};
}
async function authToken(){
 var s=await withTimeout(sb.auth.getSession(),4000);
 return s&&s.data&&s.data.session&&s.data.session.access_token;
}
async function pullViaLaptop(){
 var token=await authToken();
 if(!token) throw new Error('Not signed in');
 var r=await withTimeout(fetch('/fm-pull',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:token})}),20000);
 var d=await r.json();
 if(!r.ok||d.error) throw new Error((d&&d.error)||('HTTP '+r.status));
 farmId=d.farmId||farmId;
 loads=d.loads||[];
 premixes=d.premixes||[];
 applyLoadOrder(loads);
 lastSync=Date.now(); lastErr=''; online=true; saveCache();
}
async function dietLines(dietId){
 if(!dietId) return [];
 try{
  var token=await authToken();
  if(token){
   var r=await withTimeout(fetch('/fm-diet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:token,dietId:dietId})}),10000);
   var d=await r.json();
   if(r.ok&&d.lines) return d.lines;
  }
 }catch(e){}
 try{
  var di=await withTimeout(sb.from('diet_ingredients').select('percent,sort_order,ingredient_id').eq('diet_id',dietId).order('sort_order'),6000);
  var rows=di.data||[];
  var ids=rows.map(function(r){return r.ingredient_id;}).filter(Boolean), names={};
  if(ids.length){
   var ing=await withTimeout(sb.from('ingredients').select('id,name').in('id',ids),6000);
   (ing.data||[]).forEach(function(x){names[x.id]=x.name;});
  }
  return rows.map(function(r){return {id:r.ingredient_id,name:names[r.ingredient_id]||'Feed',percent:Number(r.percent)||0};}).filter(function(r){return r.percent>0;});
 }catch(e){ return []; }
}
async function fetchRecipe(load){
 if(!load||!load.program_id) return load.recipe||[];
 try{
  var prog=await withTimeout(sb.from('feeding_programs').select('start_date,status,pause_days,paused_on').eq('id',load.program_id).single(),6000);
  var ph=await withTimeout(sb.from('program_phases').select('sort_order,diet_id,steady_days,transition_days').eq('program_id',load.program_id).order('sort_order'),6000);
  var b=resolveBlend(programmeDay(prog.data||{}), ph.data||[]);
  var fromL=await dietLines(b.from);
  var toL=(b.to&&b.to!==b.from)?await dietLines(b.to):fromL;
  return blendDiets(fromL,toL,b.fs,b.ts);
 }catch(e){ return load.recipe||[]; }
}
function applyLoadOrder(next){
 var prev={};
 (loadCache()&&loadCache().loads||[]).forEach(function(l){ if(l.order) prev[l.id]=l.order; });
 next.forEach(function(l,i){ l.order = prev[l.id] || (i+1); });
 next.sort(function(a,b){return (a.order||99)-(b.order||99);});
}

async function pullPremixes(){
 var ings=[];
 try{
  var iq=await withTimeout(sb.from('ingredients').select('id,name,premix_diet_id').eq('farm_id',farmId),6000);
  ings=iq.data||[];
 }catch(e){}
 var rows=[];
 try{
  var dq=await withTimeout(sb.from('diets').select('id,name,diet_type,is_active').eq('farm_id',farmId).order('name'),6000);
  if(dq.error) throw dq.error;
  rows=dq.data||[];
 }catch(e){
  try{
   var d2=await withTimeout(sb.from('diets').select('id,name').eq('farm_id',farmId).order('name'),6000);
   rows=d2.data||[];
  }catch(e2){ rows=[]; }
 }
 var typed=rows.filter(function(d){ return String(d.diet_type||'').toLowerCase()==='premix' && d.is_active!==false; });
 if(!typed.length){
  var linked={};
  ings.forEach(function(i){ if(i.premix_diet_id) linked[i.premix_diet_id]=true; });
  typed=rows.filter(function(d){ return linked[d.id]; });
 }
 if(!typed.length) typed=rows.filter(function(d){ return /premix|pre mix/i.test(d.name||''); });
 var seen={}, plist=[];
 typed.forEach(function(d){
  if(seen[d.id]) return; seen[d.id]=true;
  var asIng=ings.find(function(x){return x.premix_diet_id===d.id;});
  plist.push({dietId:d.id,name:d.name,ingredientId:asIng?asIng.id:null,batchKg:500,lines:[]});
 });
 ings.forEach(function(i){
  if(!i.premix_diet_id||seen[i.premix_diet_id]) return;
  seen[i.premix_diet_id]=true;
  plist.push({dietId:i.premix_diet_id,name:i.name,ingredientId:i.id,batchKg:500,lines:[]});
 });
 premixes=plist;
}

async function pullLoads(){
 var lq=await withTimeout(sb.from('feed_loads').select('id,name,program_id').eq('farm_id',farmId).order('created_at',{ascending:false}),8000);
 if(lq.error) throw lq.error;
 var list=lq.data||[];
 var ids=list.map(function(l){return l.id;});
 var pensByLoad={}, names={};
 if(ids.length){
  try{
   var lp=await withTimeout(sb.from('feed_load_pens').select('load_id,pen_id,daily_amount_kg,sort_order').in('load_id',ids).order('sort_order'),6000);
   var penIds=[];
   (lp.data||[]).forEach(function(r){ if(penIds.indexOf(r.pen_id)<0) penIds.push(r.pen_id); });
   if(penIds.length){
    var pr=await withTimeout(sb.from('pens').select('id,name').in('id',penIds),6000);
    (pr.data||[]).forEach(function(p){names[p.id]=p.name;});
   }
   (lp.data||[]).forEach(function(r){
    (pensByLoad[r.load_id]=pensByLoad[r.load_id]||[]).push({id:r.pen_id,name:names[r.pen_id]||'Pen',kg:Number(r.daily_amount_kg)||0});
   });
  }catch(e){}
 }
 var next=list.map(function(load){
  return {id:load.id,name:load.name,program_id:load.program_id,pens:pensByLoad[load.id]||[],recipe:[]};
 });
 applyLoadOrder(next);
 loads=next;
}

async function pullCloud(){
 if(!farmId && !(await authToken())) throw new Error('no farm');
 await pullViaLaptop();
}
function blendDiets(fromL,toL,fs,ts){
 var map={};
 fromL.forEach(function(r){map[r.id]={name:r.name,p:r.percent*fs};});
 toL.forEach(function(r){if(!map[r.id]) map[r.id]={name:r.name,p:0}; map[r.id].p+=r.percent*ts; map[r.id].name=r.name;});
 var order=[]; (toL.length>=fromL.length?toL.concat(fromL):fromL.concat(toL)).forEach(function(r){if(order.indexOf(r.id)<0) order.push(r.id);});
 return order.map(function(id){return {id:id,name:map[id].name,percent:map[id].p};}).filter(function(r){return r.percent>0.0005;});
}
function mixKg(total,lines){
 var rows=lines.map(function(l){return {id:l.id,name:l.name,percent:l.percent,kg:Number(((total*l.percent)/100).toFixed(2)),planned:Number(((total*l.percent)/100).toFixed(2))};});
 var sum=rows.reduce(function(s,r){return s+r.kg;},0), drift=Number((total-sum).toFixed(2));
 if(rows.length&&Math.abs(drift)>=0.01){var i=0;for(var j=1;j<rows.length;j++) if(rows[j].kg>rows[i].kg) i=j; rows[i].kg=Number((rows[i].kg+drift).toFixed(2)); rows[i].planned=rows[i].kg;}
 var run=0; return rows.map(function(r){run+=r.kg;r.cum=Number(run.toFixed(2));return r;});
}
function rebuildCum(rows){
 var run=0; return rows.map(function(r){run+=Number(r.kg)||0; r.cum=Number(run.toFixed(2)); return r;});
}

async function flushQueue(){
 var q=queue(); if(!q.length||!farmId) return;
 var keep=[];
 for(var i=0;i<q.length;i++){
  try{
   var item=q[i];
   if(item.type==='run'){
    var run=await sb.from('feed_runs').insert({
     farm_id:farmId,load_id:item.loadId||null,load_name:item.name,program_id:item.programId||null,
     buffer_kg:item.buffer||0,pens_planned_kg:item.pensPlanned||0,pens_actual_kg:item.pensActual||0,
     fill_total_kg:item.fillTotal||0,started_at:item.startedAt,finished_at:item.finishedAt
    }).select('id').single();
    if(run.error||!run.data) throw run.error||new Error('run');
    if((item.ingredients||[]).length){
     await sb.from('feed_run_ingredients').insert(item.ingredients.map(function(r,idx){
      return {run_id:run.data.id,ingredient_id:r.id||null,ingredient_name:r.name,percent:r.percent||0,kg:r.actual!=null?r.actual:r.kg,cost:0,sort_order:idx};
     }));
    }
    if((item.pens||[]).length){
     await sb.from('feed_run_pens').insert(item.pens.map(function(p,idx){
      return {run_id:run.data.id,pen_id:p.id||null,pen_name:p.name,planned_kg:p.kg,actual_kg:p.actualKg!=null?p.actualKg:p.kg,sort_order:idx};
     }));
    }
   } else if(item.type==='premix'){
    var premixIng=item.ingredientId;
    if(!premixIng){
     var ins=await sb.from('ingredients').insert({farm_id:farmId,name:item.name,unit:'kg',cost_per_unit:0,premix_diet_id:item.dietId}).select('id').single();
     if(ins.error) throw ins.error; premixIng=ins.data.id;
    }
    for(var k=0;k<(item.ingredients||[]).length;k++){
     var line=item.ingredients[k];
     if(line.id&&line.kg>0) await adjustStock(line.id,-line.kg);
    }
    await adjustStock(premixIng,Number(item.amount)||0);
   }
  }catch(e){ keep.push(item); }
 }
 setQueue(keep);
}

async function afterLogin(force){
 if(loggedOut && !force){ show('sLogin'); return; }
 var user=null;
 try{
  var gu=await withTimeout(sb.auth.getUser(), 6000);
  user=gu && gu.data && gu.data.user;
 }catch(e){ user=null; }
 var cache=loadCache();
 if(!user && cache && !force && !loggedOut){ farmId=cache.farmId; loads=cache.loads||[]; premixes=cache.premixes||[]; lastSync=cache.lastSync; online=false; renderHome(); show('sHome'); return; }
 if(!user){ show('sLogin'); return; }
 loggedOut=false;
 if(cache&&cache.farmId){ farmId=cache.farmId; loads=cache.loads||[]; premixes=cache.premixes||[]; lastSync=cache.lastSync; renderHome(); show('sHome'); }
 try{
  var mem=await withTimeout(sb.from('farm_members').select('farm_id').eq('user_id',user.id).limit(1).maybeSingle(),8000);
  farmId=mem.data&&mem.data.farm_id;
  if(!farmId){ document.getElementById('loginErr').textContent='No farm on this login'; show('sLogin'); return; }
  await pullViaLaptop();
  online=true; lastErr=''; try{ await withTimeout(flushQueue(),8000); }catch(e){}
 }catch(e){
  online=false;
  lastErr=(e&&e.message)?e.message:'could not reach Farm Manager';
  if(cache){ farmId=cache.farmId; loads=cache.loads||[]; premixes=cache.premixes||[]; lastSync=cache.lastSync; }
 }
 renderHome(); show('sHome');
}

function renderHome(){
 try{
  document.getElementById('tabLoads').className=tab==='loads'?'on':'ghost';
  document.getElementById('tabPremix').className=tab==='premix'?'on':'ghost';
  document.getElementById('netDot').className='dot'+(online?' on':'');
  var status=fmtWhen(lastSync)+(online?'':' · offline')+(lastErr?(' · '+lastErr):'');
  document.getElementById('syncTag').innerHTML='<span class="dot '+(online?'on':'')+'"></span>'+status;
  var qn=queue().length;
  document.getElementById('queueTag').textContent=loads.length+' loads · '+premixes.length+' premixes'+(qn?(' · '+qn+' queued'):'');
  var box=document.getElementById('listBox');
  if(tab==='loads'){
   box.innerHTML=loads.length?loads.map(function(m,i){
    var done=isDone(m.id);
    var kg=(m.pens||[]).reduce(function(s,p){return s+Number(p.kg||0);},0);
    var nIng=(m.recipe&&m.recipe.length)?m.recipe.length+' ingredients':(m.pens||[]).length+' pens';
    return '<div class="cardwrap"><input class="ord" type="number" min="1" value="'+(m.order||(i+1))+'" onchange="setOrder('+i+',this.value)"><button class="card'+(done?' done':'')+'" onclick="openLoad('+i+')"><div><b>'+m.name+'</b><span>'+Math.round(kg)+' kg · '+nIng+(done?' · completed today':'')+'</span></div></button></div>';
   }).join(''):'<p class="sub">No loads in Farm Manager. Add them under Feeding → Loads, then Refresh.</p>';
  }else{
   box.innerHTML=premixes.length?premixes.map(function(m,i){
    var done=isDone('px-'+(m.dietId||m.id||i));
    return '<button class="card'+(done?' done':'')+'" onclick="openPremix('+i+')"><div><b>'+(m.name||'Premix')+'</b><span>Usual '+(m.batchKg||500)+' kg'+(done?' · mixed today':'')+'</span></div></button>';
   }).join(''):'<p class="sub">No premixes in Farm Manager. Open Feeding → Premixes, tap Save premix, then Refresh here.</p>';
  }
 }catch(e){
  lastErr=e.message||String(e);
  document.getElementById('listBox').innerHTML='<p class="sub">List error: '+lastErr+'</p>';
 }
}
function setOrder(i,v){
 loads[i].order=Math.max(1,Number(v)||1);
 loads.sort(function(a,b){return (a.order||99)-(b.order||99);});
 saveCache(); renderHome();
}
document.getElementById('tabLoads').onclick=function(){tab='loads';renderHome();};
document.getElementById('tabPremix').onclick=function(){tab='premix';renderHome();};
document.getElementById('bRefresh').onclick=async function(){
 var gen=++refreshGen;
 var btn=document.getElementById('bRefresh');
 btn.textContent='Wait';
 lastErr='';
 document.getElementById('syncTag').textContent='Refreshing…';
 var dog=setTimeout(function(){
  if(gen!==refreshGen) return;
  btn.textContent='Refresh';
  online=false;
  lastErr='laptop could not reach Farm Manager in 20s';
  renderHome();
 }, 22000);
 try{
  await pullViaLaptop();
  try{ await withTimeout(flushQueue(),8000); }catch(e){}
  if(gen!==refreshGen) return;
  online=true; lastErr='';
 }catch(e){
  if(gen!==refreshGen) return;
  online=false;
  lastErr=(e&&e.message)?e.message:'Refresh failed';
 }
 clearTimeout(dog);
 if(gen!==refreshGen) return;
 btn.textContent='Refresh';
 renderHome();
};

async function openLoad(i){
 var load=loads[i]; if(!load) return;
 var pens=load.pens||[];
 var pensSum=pens.reduce(function(s,p){return s+Number(p.kg||0);},0);
 job={kind:'load',id:load.id,name:load.name,programId:load.program_id,pens:pens,pensSum:pensSum,recipe:load.recipe||[],buffer:0,ingredients:[],fillIndex:0,fillAnchor:null,captureAnchor:false,lastAdv:0,rebalAsked:false};
 document.getElementById('bufName').textContent=load.name;
 document.getElementById('bufPens').textContent='Pens '+Math.round(pensSum)+' kg';
 document.getElementById('bufKg').value='0';
 document.getElementById('bufChips').innerHTML=[-100,-50,-25,0,25,50,100].map(function(n){return '<button class="chip" onclick="setBuf('+n+')">'+(n>0?('+'+n):n)+'</button>';}).join('');
 updBuf(); show('sBuffer');
 if(!job.recipe.length){
  document.getElementById('bufFill').textContent='Loading recipe…';
  load.recipe=await fetchRecipe(load);
  job.recipe=load.recipe||[];
  saveCache();
  updBuf();
 }
}
function setBuf(n){document.getElementById('bufKg').value=String(n);updBuf();}
function updBuf(){if(!job)return;var b=Number(document.getElementById('bufKg').value)||0;document.getElementById('bufFill').textContent='Fill total '+Math.round(job.pensSum+b)+' kg';}
document.getElementById('bufKg').oninput=updBuf;
document.getElementById('bBufBack').onclick=function(){show('sHome');};
document.getElementById('bBufGo').onclick=async function(){
 if(!job) return;
 if(!job.recipe.length) job.recipe=await fetchRecipe({id:job.id,program_id:job.programId,recipe:[]});
 job.buffer=Number(document.getElementById('bufKg').value)||0;
 job.ingredients=mixKg(Math.max(0,job.pensSum+job.buffer),job.recipe||[]);
 job.fillIndex=0; job.fillAnchor=null; job.captureAnchor=false; job.rebalAsked=false;
 await post('/apply',{name:job.name,pens:job.pens,ingredients:job.ingredients});
 show('sFill'); paintFill();
};
async function openPremix(i){
 var p=premixes[i]; if(!p) return;
 if(!p.lines||!p.lines.length){
  p.lines=await dietLines(p.dietId);
  premixes[i].lines=p.lines;
  saveCache();
 }
 job={kind:'premix',id:'px-'+p.dietId,name:p.name,dietId:p.dietId,ingredientId:p.ingredientId,amount:p.batchKg||500,lines:p.lines||[],ingredients:[],fillIndex:0,fillAnchor:null,captureAnchor:false,lastAdv:0,rebalAsked:false,pens:[]};
 document.getElementById('amtName').textContent=p.name;
 document.getElementById('amtKg').value=String(job.amount);
 document.getElementById('amtChips').innerHTML=[250,500,750,1000,1500].map(function(n){return '<button class="chip" onclick="setAmt('+n+')">'+n+'</button>';}).join('');
 updAmt(); show('sAmount');
}
function setAmt(n){document.getElementById('amtKg').value=String(n);updAmt();}
function updAmt(){
 if(!job||job.kind!=='premix')return;
 job.amount=Number(document.getElementById('amtKg').value)||0;
 job.ingredients=mixKg(job.amount,job.lines||[]);
 document.getElementById('amtLines').innerHTML=job.ingredients.map(function(r){return '<div class="card"><div><b>'+r.name+'</b></div><div>'+Math.round(r.kg)+' kg</div></div>';}).join('');
}
document.getElementById('amtKg').oninput=updAmt;
document.getElementById('bAmtBack').onclick=function(){show('sHome');};
document.getElementById('bAmtGo').onclick=async function(){
 updAmt(); if(!job.amount)return;
 localStorage.setItem('fm_premix_batch_'+job.dietId,String(job.amount));
 job.fillIndex=0; job.fillAnchor=null; job.captureAnchor=false; job.rebalAsked=false;
 await post('/apply',{name:job.name,pens:[],ingredients:job.ingredients});
 show('sFill'); paintFill();
};

function paintFill(){
 if(!job)return;
 var rows=job.ingredients||[], i=job.fillIndex, loaded=rows.length&&i>=rows.length, cur=rows[i];
 document.getElementById('fillTitle').textContent=job.name||'FILL';
 document.getElementById('fillIdx').textContent=rows.length?(Math.min(i+1,rows.length)+' / '+rows.length):'';
 document.getElementById('fillName').textContent=loaded?'Mix loaded':(cur?cur.name:'Zero first');
 document.getElementById('bAfterFill').textContent=job.kind==='premix'?'Empty to bay':'Start feedout';
 document.getElementById('bAfterFill').style.display=loaded?'':'none';
 document.getElementById('bFillNext').style.display=loaded?'none':'';
}
function fillProgress(live){ if(!job||job.fillAnchor==null||live==null) return null; return live-job.fillAnchor; }
function sealIng(idx, prog){
 if(!job||idx==null||!job.ingredients[idx]) return;
 var cur=job.ingredients[idx];
 var start=cur.cum-cur.kg;
 cur.actual=Math.max(0, Number((prog-start).toFixed(1)));
}
function maybeAutoFill(live){
 if(!job||!document.getElementById('sFill').className.includes('on'))return;
 if(job.captureAnchor&&live!=null){job.fillAnchor=live;job.captureAnchor=false; hitTarget=false; hitOver=false;}
 var cur=(job.ingredients||[])[job.fillIndex]; if(!cur||live==null)return;
 var prog=fillProgress(live); if(prog==null) return;
 checkRebalance(prog,cur);
 if(prog>=cur.cum-2 && !hitTarget){ hitTarget=true; beep(true); flash(false); }
 if(prog>cur.kg*1.05+(cur.cum-cur.kg) && !hitOver){ hitOver=true; beep(false); flash(true); }
 var now=Date.now();
 if(prog>=cur.cum-2 && now-(job.lastAdv||0)>2500){
  sealIng(job.fillIndex, prog);
  job.lastAdv=now; job.fillIndex=Math.min(job.ingredients.length,job.fillIndex+1);
  job.rebalAsked=false; hitTarget=false; hitOver=false; paintFill();
 }
}
function checkRebalance(prog,cur){
 if(!job||job.rebalAsked||!cur) return;
 var added=prog-(cur.cum-cur.kg);
 if(added<=cur.kg*1.05) { document.getElementById('rebalBox').style.display='none'; return; }
 job.rebalAsked=true;
 var extra=added-cur.kg;
 var pct=cur.percent||0;
 var newTotal=pct>0? added/(pct/100) : null;
 document.getElementById('rebalText').textContent=cur.name+' is +'+Math.round(extra)+' kg over (5% limit). Rebalance the rest of the mix to match?';
 document.getElementById('rebalBox').style.display=newTotal?'':'none';
 job.pendingTotal=newTotal; job.pendingAdded=added;
}
document.getElementById('bRebalNo').onclick=function(){document.getElementById('rebalBox').style.display='none';};
document.getElementById('bRebalYes').onclick=function(){
 if(!job||!job.pendingTotal) return;
 var i=job.fillIndex, rows=job.ingredients||[];
 if(rows[i]) rows[i].kg=job.pendingAdded;
 var rest=rows.slice(i+1);
 var restPct=rest.reduce(function(s,r){return s+(r.percent||0);},0);
 var restKg=job.pendingTotal*(restPct/100);
 rest.forEach(function(r){ r.kg=restPct? Number((restKg*((r.percent||0)/restPct)).toFixed(1)) : r.kg; });
 job.ingredients=rebuildCum(rows);
 document.getElementById('rebalBox').style.display='none';
 paintFill();
};
document.getElementById('bFillBack').onclick=function(){if(!job)return;job.fillIndex=Math.max(0,job.fillIndex-1);job.rebalAsked=false;paintFill();};
document.getElementById('bFillNext').onclick=function(){
 if(!job)return;
 var prog=fillProgress(lastLive);
 if(prog!=null) sealIng(job.fillIndex, prog);
 job.fillIndex=Math.min(job.ingredients.length,job.fillIndex+1);
 job.rebalAsked=false; hitTarget=false; hitOver=false; paintFill();
};
document.getElementById('bFillZero').addEventListener('pointerdown', function(){ if(job){ job.fillAnchor=null; job.captureAnchor=true; } });
function queueRun(pensFromClock){
 if(!job||job.kind!=='load') return;
 var pens=pensFromClock||job.pens||[];
 var ings=job.ingredients||[];
 var item={
  type:'run', loadId:job.id, name:job.name, programId:job.programId||null,
  buffer:job.buffer||0,
  pensPlanned:(job.pens||[]).reduce(function(s,p){return s+Number(p.kg||0);},0),
  pensActual:pens.reduce(function(s,p){return s+Number(p.actualKg!=null?p.actualKg:p.kg||0);},0),
  fillTotal:ings.reduce(function(s,r){return s+Number(r.actual!=null?r.actual:r.kg||0);},0),
  startedAt:job.startedAt||new Date().toISOString(),
  finishedAt:new Date().toISOString(),
  ingredients:ings, pens:pens
 };
 var q=queue(); q.push(item); setQueue(q);
 markDone(job.id);
 flushQueue().catch(function(){});
}
document.getElementById('bAfterFill').onclick=function(){
 if(!job)return;
 job.startedAt=new Date().toISOString();
 if(job.kind==='premix'){document.getElementById('bayText').textContent=(job.amount||0)+' kg of '+job.name+' is mixed. Empty into the bay, then tap Completed.';document.getElementById('bayMsg').textContent='';show('sBay');}
 else { show('sClock'); }
};

async function adjustStock(ingId,delta){
 var q=await sb.from('feed_stock').select('id,quantity_kg').eq('farm_id',farmId).eq('ingredient_id',ingId).maybeSingle();
 if(q.data){var next=Math.max(0,Number(q.data.quantity_kg)+delta);await sb.from('feed_stock').update({quantity_kg:next,updated_at:new Date().toISOString()}).eq('id',q.data.id);return next;}
 var start=Math.max(0,delta);await sb.from('feed_stock').insert({farm_id:farmId,ingredient_id:ingId,quantity_kg:start,updated_at:new Date().toISOString()});return start;
}
document.getElementById('bBayDone').onclick=async function(){
 if(!job) return;
 var item={type:'premix',name:job.name,dietId:job.dietId,ingredientId:job.ingredientId,amount:job.amount,ingredients:job.ingredients};
 document.getElementById('bayMsg').textContent='Saving…';
 try{
  if(!online) throw new Error('offline');
  await flushQueue();
  var q=queue(); q.push(item); setQueue(q); await flushQueue();
  if(queue().length) throw new Error('queued');
  markDone(job.id); document.getElementById('bayMsg').textContent='Saved to Farm Manager stock';
 }catch(e){
  var q2=queue(); q2.push(item); setQueue(q2);
  markDone(job.id);
  document.getElementById('bayMsg').textContent='No signal — stored on this iPad and will upload on Refresh';
 }
};
document.getElementById('bBayHome').onclick=function(){show('sHome');renderHome();};
async function doLogin(e){
 if(e) e.preventDefault();
 if(busy) return;
 var email=document.getElementById('email').value.trim();
 var pass=document.getElementById('pass').value;
 var err=document.getElementById('loginErr');
 var btn=document.getElementById('bLogin');
 if(!email||!pass){ err.textContent='Enter email and password'; return; }
 busy=true; loggedOut=false;
 btn.disabled=true; btn.textContent='Signing in…';
 err.textContent='';
 try{
  var res=await withTimeout(sb.auth.signInWithPassword({email:email,password:pass}), 10000);
  if(res.error) throw res.error;
  await afterLogin(true);
 }catch(ex){
  var msg=(ex&&ex.message)?ex.message:'Login failed';
  if(msg==='timeout' || /fetch|network|failed/i.test(msg)) msg='No internet. Turn on cellular / Wi-Fi Assist, then try again.';
  err.textContent=msg;
  show('sLogin');
 }
 busy=false;
 btn.disabled=false; btn.textContent='Log in';
}
document.getElementById('loginForm').onsubmit=doLogin;
document.getElementById('bLogin').onclick=function(e){ doLogin(e); };
document.getElementById('bLogout').onclick=function(){
 loggedOut=true;
 show('sLogin');
 document.getElementById('loginErr').textContent='';
 document.getElementById('bLogin').disabled=false;
 document.getElementById('bLogin').textContent='Log in';
 busy=false;
 withTimeout(sb.auth.signOut(), 4000).catch(function(){});
};
document.getElementById('bClockHome').onclick=function(){show('sHome');};
document.getElementById('bFinish').onclick=async function(){
 try{
  var r=await fetch('/weight?t='+Date.now()); var j=await r.json();
  queueRun(j.pens||job.pens);
 }catch(e){ queueRun(job.pens); }
 renderHome(); show('sHome');
};
document.getElementById('bStart').onclick=function(){post('/start');};
document.getElementById('bDone').onclick=function(){post('/done');};
document.getElementById('bPrev').onclick=function(){post('/prev');};
holdBtn('bZero','/zero'); holdBtn('bTotal','/total'); holdBtn('bFillZero','/zero');
document.getElementById('bZeroTotal').onclick=function(){post('/zero-total');};
document.getElementById('bFillZT').onclick=function(){post('/zero-total');};

function setFault(msg){
 var el=document.getElementById('fault');
 if(!msg){ el.className=''; el.textContent=''; return; }
 el.className='on'; el.textContent=msg;
}

async function tick(){
 try{
  var r=await fetch('/weight?t='+Date.now(),{cache:'no-store'}); var j=await r.json(); var live=j.liveKg;
  lastLive=live; fetchFails=0;
  if(j.com==='lost') setFault('COM5 lost');
  else if(j.silentSec==null || j.silentSec>=8 || j.error==='no-signal') setFault('HL-20 silent '+(j.silentSec!=null?Math.round(j.silentSec)+'s':'8s'));
  else setFault('');
  document.getElementById('homeLive').textContent=(live==null?'---':Math.round(live))+' kg';
  var rem=j.autoArmed?j.remainingKg:live;
  document.getElementById('kg').textContent=rem==null?'---':String(Math.round(rem));
  document.getElementById('kg').className='kg'+(j.autoArmed&&rem<=0?' done':'');
  document.getElementById('pen').textContent=j.penName||'FEEDOUT';
  document.getElementById('idx').textContent=j.penCount?((j.penIndex+1)+' / '+j.penCount):'';
  document.getElementById('meta').textContent=(j.autoArmed?('TO -'+Math.round(j.targetKg)+'   OUT '+Math.round(j.dumpedKg)):( (j.liveKind||'WAGON')+' '+(live==null?'-':Math.round(live)) ))+'   '+(j.stable?'STEADY':'MOVING');
  var p=j.autoArmed&&j.targetKg? Math.max(0,Math.min(100,(j.dumpedKg/j.targetKg)*100)):0;
  document.getElementById('penBarI').style.width=p+'%';
  if(j.autoArmed&&rem<=0&&j.stable&&!hitTarget){ hitTarget=true; beep(true); flash(false); }
  var prog=fillProgress(live);
  var cur=job&&job.ingredients?job.ingredients[job.fillIndex]:null;
  var shown=prog!=null?prog:live;
  document.getElementById('fillKg').textContent=shown==null?'---':String(Math.round(shown));
  var over=cur&&prog!=null&&prog>cur.cum*1.05;
  document.getElementById('fillKg').className='kg'+(over?' over':(cur&&prog!=null&&prog>=cur.cum-2?' done':''));
  if(job&&job.fillAnchor==null) document.getElementById('fillHint').textContent='Tap Zero, then load';
  else if(cur) document.getElementById('fillHint').textContent='Add '+Math.round(cur.kg)+' kg  ·  scale to '+Math.round(cur.cum);
  else document.getElementById('fillHint').textContent='Fill complete';
  var bp=0;
  if(cur&&prog!=null){ var start=cur.cum-cur.kg; bp=Math.max(0,Math.min(110,((prog-start)/Math.max(cur.kg,1))*100)); }
  document.getElementById('fillBarI').style.width=bp+'%';
  document.getElementById('fillBar').className='bar'+(over?' over':'');
  if(j.stable&&live!=null) maybeAutoFill(live);
 }catch(e){
  fetchFails++;
  if(fetchFails>=3) setFault('COM5 lost');
  document.getElementById('meta').textContent='NO MIXER';
  document.getElementById('fillHint').textContent='NO MIXER';
 }
}
setInterval(tick,250); tick();
if('serviceWorker' in navigator){
 navigator.serviceWorker.getRegistrations().then(function(rs){ rs.forEach(function(r){ r.update(); }); });
 navigator.serviceWorker.register('/sw.js').catch(function(){});
}
sb.auth.getSession().then(function(s){ afterLogin(false); });
</script>
</body>
</html>
"""

MANIFEST = """{
  "name": "Mixer Clock",
  "short_name": "MixerClock",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#070707",
  "theme_color": "#070707",
  "icons": [{"src": "/icon.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "any"}]
}"""

SERVICE_WORKER = """
const C='mc-v7';
self.addEventListener('install', e => {
  e.waitUntil(caches.open(C).then(c => c.addAll(['/icon.svg','/manifest.webmanifest'])));
  self.skipWaiting();
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k!==C).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (u.hostname.includes('supabase') || u.pathname.startsWith('/fm-') || u.pathname.startsWith('/weight') || u.pathname.startsWith('/zero') || u.pathname.startsWith('/start') || u.pathname.startsWith('/apply') || u.pathname === '/' || u.pathname === '/clock' || u.pathname === '/sw.js') {
    return;
  }
  e.respondWith(
    fetch(e.request).then(r => { const c=r.clone(); caches.open(C).then(cache => cache.put(e.request,c)); return r; })
      .catch(() => caches.match(e.request))
  );
});
"""

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="96" fill="#070707"/>
<text x="256" y="310" text-anchor="middle" font-size="220" font-family="monospace" fill="#ffe14a">kg</text>
</svg>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, body: dict):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
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
            page = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if path == "/manifest.webmanifest":
            raw = MANIFEST.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/sw.js":
            raw = SERVICE_WORKER.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/icon.svg":
            raw = ICON_SVG.encode()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path.startswith("/weight") or path.startswith("/mixes"):
            self._json(200, snapshot())
            return
        self._json(404, {"error": "not found"})

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
            idx = body.get("penIndex", qs.get("pen", [None])[0])
            start_pen(int(idx) if idx is not None and str(idx).isdigit() else None)
        elif path == "/done":
            finish_pen(auto=False)
        elif path == "/prev":
            with lock:
                nxt = max(0, state["penIndex"] - 1)
            start_pen(nxt)
        elif path == "/target":
            kg = float(body.get("kg") or qs.get("kg", [0])[0] or 0)
            with lock:
                state["targetKg"] = kg
            start_pen()
        elif path == "/zero":
            send_key("zero")
        elif path == "/total":
            send_key("total")
        elif path == "/enter":
            send_key("enter")
        elif path in ("/zero-total", "/zerototal"):
            zero_total()
        elif path == "/mix":
            idx = body.get("index", (qs.get("i") or [0])[0])
            try:
                select_mix(int(idx))
            except Exception:
                pass
        elif path == "/apply":
            apply_load(body)
        elif path == "/fm-pull":
            token = (body.get("token") or "").strip()
            if not token:
                self._json(401, {"error": "not signed in"})
                return
            try:
                self._json(200, fm_snapshot(token))
            except Exception as exc:
                log("fm-pull: %s" % exc)
                self._json(502, {"error": str(exc)})
            return
        elif path == "/fm-diet":
            token = (body.get("token") or "").strip()
            diet_id = body.get("dietId") or ""
            if not token:
                self._json(401, {"error": "not signed in"})
                return
            try:
                self._json(200, {"lines": diet_lines_py(token, diet_id)})
            except Exception as exc:
                self._json(502, {"error": str(exc)})
            return
        else:
            self._json(404, {"error": "not found"})
            return
        self._json(200, snapshot())


def reader(port: str, baud: int) -> None:
    global ser_port
    try:
        ser = serial.Serial(port, baud, timeout=0.2)
    except Exception as exc:
        log(f"cannot open {port}: {exc}")
        with lock:
            state["error"] = str(exc)
        return
    with ser_lock:
        ser_port = ser
    buf = ""
    log(f"listening {port} {baud}")
    while True:
        try:
            with ser_lock:
                chunk = ser.read(256)
        except Exception as exc:
            log(f"serial error: {exc}")
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


def main() -> None:
    global pens, mixes
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=38400)
    p.add_argument("--pens", default="", help="Shed 1:500,Shed 2:450")
    p.add_argument("--http", type=int, default=PORT)
    args = p.parse_args()
    mixes = default_mixes()
    pens = parse_pens(args.pens)
    if pens:
        with lock:
            state["penName"] = pens[0]["name"]
            state["targetKg"] = pens[0]["kg"]
        log("pens: " + ", ".join(f"{x['name']} {x['kg']:.0f}" for x in pens))
    elif mixes:
        select_mix(0)

    httpd = ThreadingHTTPServer((HOST, args.http), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=reader, args=(args.port, args.baud), daemon=True).start()
    print(f"Clock  http://127.0.0.1:{args.http}/", flush=True)
    print("Phone on the same Wi-Fi: http://<this-pc-ip>:8765/", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("stop")


if __name__ == "__main__":
    main()
