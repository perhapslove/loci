#!/usr/bin/env python3
"""Loci relay — the brain. Devices send photos here; the relay judges "did they find it?",
speaks the story back, and serves a web page for the route.

Standard library only. Run:
    python3 relay.py                      # 0.0.0.0:8787, route from ./stations.json

Judging backend, first that applies:
    1. ANTHROPIC_API_KEY set   → Anthropic API (fastest)
    2. `claude` CLI installed  → `claude -p` reads the photo (~6–8 s each, uses your Claude login)
    3. otherwise, or JUDGE=manual → judge by hand with the buttons on the web page

Environment: RELAY_PORT, RELAY_HOST (default 0.0.0.0; 127.0.0.1 = only this Mac and the tunnel), MODEL (default claude-sonnet-5-5), STATIONS (route file), JUDGE (manual),
CLAUDE_PROXY (default http://127.0.0.1:7890; skipped when nothing listens there), TTS_VOICE (default Tingting), WATCHER_TOKEN,
WATCHER_SPEAK_MISS=1, LOCI_DATA (where photos/audio are stored; default ./captures),
ASR_URL / ASR_KEY / ASR_MODEL (an OpenAI-compatible /v1/audio/transcriptions endpoint, e.g. a local
Whisper or Qwen-ASR server; without it the Watcher's push-to-talk gets a polite "I can't listen yet"),
ROUTES_DIR (route files players can switch to), LOCI_KEY (key required through the tunnel),
PRUNE_AT (default 23:30: nightly digest, then photos nobody kept are deleted).

Device endpoints (see docs/api.md):
    POST /upload                         XIAO ESP32S3 Sense: raw JPEG body, header X-Device
    POST /audio                          XIAO: raw WAV body
    POST /v1/watcher/vision              SenseCAP Watcher stock firmware, "Device AI Service"
    POST /v1/notification/event          SenseCAP Watcher HTTP push (task alarm with image)
    POST /v2/watcher/talk/audio_stream   SenseCAP Watcher push-to-talk → the spirit answers out loud
    GET  /api/v1/state                   any client (glasses, phone): compact progress
    POST /api/v1/photo                   any client: JPEG (raw or JSON {img}) → verdict (quest) or a look (wander)
    POST /api/v1/talk                    any client: {"text": "..."} or a recording → the spirit's reply
    POST /api/v1/keep                    any client: keep the last photo for the day's digest
    GET  /api/v1/routes, POST /api/v1/route    list / switch routes
    GET  /api/v1/digest                  the day in short: what was kept or solved
    GET  /play                           the phone client (src/phone/index.html)
    POST /api/v1/next                    any client: go to the next station, only once this one is solved
    POST /api/v1/log                     any client: {"msg": "..."} → one line in the relay log (for devices without a console)

Requests from outside (a Cloudflare tunnel, a page on another site, a domain name pointing here) may
only use /play and the /api/v1/* endpoints (with LOCI_KEY, if set); the operator page and buttons stay local.
"""

import array
import base64
import hashlib
import hmac
import io
import ipaddress
import itertools
import json
import math
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from moments import Journal

HERE = Path(__file__).resolve().parent
CAPTURES = Path(os.environ.get("LOCI_DATA", HERE / "captures"))
CAPTURES.mkdir(parents=True, exist_ok=True)
TTS_DIR = CAPTURES / "tts"
TTS_DIR.mkdir(exist_ok=True)
PORT = int(os.environ.get("RELAY_PORT", "8787"))
HOST = os.environ.get("RELAY_HOST", "0.0.0.0")   # 127.0.0.1 on a network you don't trust: only this Mac and the tunnel
MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
STATIONS_FILE = Path(os.environ.get("STATIONS", HERE / "stations.json"))
JUDGE = os.environ.get("JUDGE", "auto")
CLAUDE_BIN = shutil.which("claude")
CLAUDE_PROXY = os.environ.get("CLAUDE_PROXY", "http://127.0.0.1:7890")
TTS_VOICE = os.environ.get("TTS_VOICE", "Tingting")
WATCHER_TOKEN = os.environ.get("WATCHER_TOKEN", "")    # token set in the SenseCraft app; empty = don't check
WATCHER_SPEAK_MISS = os.environ.get("WATCHER_SPEAK_MISS", "0") == "1"  # also speak on a miss (triggers the alarm)
ASR_URL = os.environ.get("ASR_URL", "")
ASR_KEY = os.environ.get("ASR_KEY", "")
ASR_MODEL = os.environ.get("ASR_MODEL", "")
MIN_VOICED_S = 0.3   # less voice than this and ASR models make up whole sentences, so ask again instead
ROUTES_DIR = Path(os.environ.get("ROUTES_DIR", HERE / "routes"))   # route files a player can switch between
LOCI_KEY = os.environ.get("LOCI_KEY", "")   # when set, requests through the tunnel must carry it (?key= or X-Loci-Key)
FFMPEG = shutil.which("ffmpeg")             # phones record AAC/Opus; turned into 16 kHz PCM for the ASR
PHONE_PAGE = HERE.parent / "phone" / "index.html"
MAX_BODY = 25 * 1024 * 1024                       # a phone photo is ~1 MB, a recording a few MB
PRUNE_AT = os.environ.get("PRUNE_AT", "23:30")   # nightly: write the digest, delete photos nobody kept
PUBLIC_PATHS = {"/api/v1/state", "/api/v1/photo", "/api/v1/talk", "/api/v1/next", "/api/v1/log",
                "/api/v1/keep", "/api/v1/routes", "/api/v1/route", "/api/v1/digest", "/play"}  # all a tunnel may reach
PUBLIC_PREFIXES = ("/api/v1/voice/", "/api/v1/photo-file/")
SPIRIT = ("你是「地灵」，住在这座城市里的地方之灵，正陪一位玩家在城里走。"
          "用中文口语回答，像老朋友聊天，最多两句、40 个字以内。你的话会被念出来，所以不要表情符号、括号和列表。"
          "只聊这座城、这条路线和眼前的东西；不给医疗、法律、金钱方面的建议。")
LOOK = ("你是「地灵」，住在{city}的地方之灵，正陪玩家在城里漫游。这是玩家刚让你看的照片。"
        "挑一个最值得注意的细节，用口语说一两句（50 字以内）：它是什么、为什么有意思。"
        "不确定就只说你看到了什么，不要编历史和名字。另外判断照片里有没有能认出脸的人。"
        "只输出 JSON：{{\"text\": \"要念给玩家听的话\", \"people\": true 或 false}}")
KEEP_WORDS = ("收起来", "记下来", "记住这个", "留着", "keep")
NOT_KEEP = re.compile(r"(不要|不用|不必|别|不|没|勿)\s*(再)?\s*(收起来|记下来|记住|留着)|(don'?t|do not|not)\s+keep", re.I)
MANUAL_NO = "人工判定：不是这个。"


def wants_keep(text: str) -> bool:
    """"收起来" keeps; "不要收起来" / "别留着" / "don't keep" don't."""
    low = text.lower()
    return any(w in low for w in KEEP_WORDS) and not NOT_KEEP.search(low)

# The devices this project expects; the web page shows a slot for each.
DEVICE_KINDS = {
    "watcher": "SenseCAP Watcher（地灵本体）",
    "xiao": "XIAO ESP32S3 Sense（可穿戴）",
    "glasses": "Rokid 眼镜（玩家视角）",
    "phone": "手机（玩家视角）",
}

lock = threading.Lock()
journal = Journal(CAPTURES)
state = {
    "route": str(STATIONS_FILE),
    "station": 0,
    "photo": None,          # latest photo filename
    "photo_at": None,
    "photo_from": None,     # device id
    "audio": None,
    "verdict": None,        # {"match": True/False/None, "text": "...", "source": "..."}
    "solved": [],
    "epoch": 0,             # +1 on route switch / reset / a verdict by hand: a judgement still running is then stale
    "devices": {},          # id → {kind, first_seen, last_seen, last_event, ip, count}
    "log": [],
}
talks = {}                  # device id → last few (heard, reply) pairs, so the spirit remembers the chat


def load_route(path: Path | None = None) -> dict:
    """The route being played (or the one at `path`): a quest (fixed stations) or a wander (no stations,
    the spirit just looks)."""
    if path is None:
        with lock:
            path = Path(state["route"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("stations", [])
    data["code"] = path.stem
    data["mode"] = data.get("mode") or ("quest" if data["stations"] else "wander")
    return data


def load_stations():
    data = load_route()
    return data["stations"], data.get("title", "Loci")


def route_files() -> list[Path]:
    files = sorted(ROUTES_DIR.glob("*.json")) if ROUTES_DIR.is_dir() else []
    return files if STATIONS_FILE in files else [STATIONS_FILE] + files


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with lock:
        state["log"] = (state["log"] + [line])[-40:]


def seen(device_id: str, kind: str, event: str, ip: str = ""):
    now = time.time()
    with lock:
        d = state["devices"].setdefault(device_id, {"kind": kind, "first_seen": now, "count": 0})
        d.update(kind=kind, last_seen=now, last_event=event, ip=ip)
        d["count"] += 1
        first = d["count"] == 1
    if first:
        log(f"新设备接入：{device_id}（{DEVICE_KINDS.get(kind, kind)}）{ip}")


def device_kind(device_id: str, default: str) -> str:
    low = device_id.lower()
    for k in ("watcher", "glasses", "rokid", "xiao", "phone"):
        if low.startswith(k):
            return "glasses" if k == "rokid" else k
    return default


# ── judging ──────────────────────────────────────────────────────────────

def judge_backend():
    if JUDGE == "manual":
        return "manual"
    if API_KEY:
        return "api"
    if CLAUDE_BIN:
        return "claude-cli"
    return "manual"


def build_instruction(station: dict) -> str:
    if station.get("instruction"):                   # a full prompt supplied by the caller (wander looks)
        return station["instruction"]
    if station.get("mode") == "describe":
        return station["prompt"] + "\n另外判断照片里有没有能认出脸的人。\n只输出 JSON：{\"match\": true, \"text\": \"要念给玩家听的话\", \"people\": true 或 false}"
    return (
        "你是一个城市解谜游戏的裁判。玩家用胸前的小相机拍下他认为的答案。\n"
        f"这一站要找的东西：{station['target']}\n"
        f"判定标准：{station.get('criteria', '照片里清楚地拍到了上面这个东西')}\n"
        "拍得模糊、只拍到一部分但能认出来，算对；拍的是别的东西，算错。另外判断照片里有没有能认出脸的人。\n"
        "只输出 JSON：{\"match\": true 或 false, \"text\": \"一句话理由，对玩家说，20 字以内\", \"people\": true 或 false}"
    )


def parse_reply(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        return json.loads(m.group(0)) if m else {"match": None, "text": raw.strip()[:80]}
    except json.JSONDecodeError:
        return {"match": None, "text": raw.strip()[:80]}


def claude_env() -> dict:
    """Through CLAUDE_PROXY when it is listening (needed in mainland China), straight out when it isn't
    (abroad, with the proxy app closed) — otherwise every judgement fails on a dead proxy."""
    u = urlsplit(CLAUDE_PROXY) if CLAUDE_PROXY else None
    if u and u.hostname:
        try:
            socket.create_connection((u.hostname, u.port or 80), timeout=0.3).close()
            return dict(os.environ, HTTP_PROXY=CLAUDE_PROXY, HTTPS_PROXY=CLAUDE_PROXY)
        except OSError:
            pass
    return {k: v for k, v in os.environ.items() if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}


def ask_claude_cli(path: Path, instruction: str) -> dict:
    prompt = f"用 Read 工具看这张照片：{path} 。\n{instruction}\n只输出一行 JSON，不要别的话。"
    env = claude_env()
    t0 = time.time()
    r = subprocess.run([CLAUDE_BIN, "-p", "--setting-sources", "", "--model", MODEL,
                        "--allowedTools", "Read", "--output-format", "text", prompt],
                       capture_output=True, text=True, timeout=90, env=env, cwd=str(HERE))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-200:])
    out = parse_reply(r.stdout)
    out["source"] = f"claude-cli {time.time() - t0:.1f}s"
    return out


def ask_model(jpeg: bytes, station: dict, path: Path) -> dict:
    """Return {"match": bool|None, "text": str, "source": str}; match=None means "a human decides"."""
    backend = judge_backend()
    if backend == "manual":
        return {"match": None, "text": "没有可用的模型，请在网页上人工判。", "source": "none"}
    instruction = build_instruction(station)
    if backend == "claude-cli":
        try:
            return ask_claude_cli(path, instruction)
        except Exception as e:
            return {"match": None, "text": f"claude -p 失败（{type(e).__name__}），请人工判。", "source": "error"}
    body = {
        "model": MODEL,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(jpeg).decode()}},
            {"type": "text", "text": instruction},
        ]}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        out = parse_reply("".join(b.get("text", "") for b in resp.get("content", [])))
        out["source"] = f"model {time.time() - t0:.1f}s"
        return out
    except Exception as e:  # network, proxy, quota → fall back to a human
        return {"match": None, "text": f"模型调用失败（{type(e).__name__}），请人工判。", "source": "error"}


_seq = itertools.count()


def new_name(suffix: str) -> str:
    """Unique per file even when two photos arrive in the same millisecond."""
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}-{next(_seq) % 10000:04d}{suffix}"


def snapshot() -> tuple[Path, int, int, bool]:
    """Route file, station, play-through and whether that station is solved — read together, so a photo is
    judged against one consistent moment even if someone switches route while it is on its way."""
    with lock:
        return Path(state["route"]), state["station"], state["epoch"], state["station"] in state["solved"]


def judge(fname: str, jpeg: bytes, stations: list, idx: int, epoch: int, already_solved: bool) -> dict:
    """Judge a photo against station `idx` of the play-through `epoch` (both from snapshot())."""
    if already_solved:  # don't re-judge (or retell the story) until someone moves on
        verdict = {"match": None, "text": "这一站已经解开了，点「下一站」继续。", "source": "skip"}
    else:
        verdict = ask_model(jpeg, stations[idx], CAPTURES / fname)
    with lock:
        if state["epoch"] == epoch:      # the route was not switched or reset, nobody judged by hand
            if state["photo"] == fname:  # and no newer photo arrived
                state["verdict"] = verdict
            if verdict.get("match") is True and idx not in state["solved"]:
                state["solved"].append(idx)
        elif state["photo"] == fname and (state["verdict"] or {}).get("source") == "manual":
            verdict = {**verdict, "match": state["verdict"]["match"], "source": "manual"}   # the person wins
    log(f"站{idx + 1} 判定 {verdict.get('match')} · {verdict.get('text')} · {verdict.get('source')}")
    return verdict


def see(jpeg: bytes, device_id: str, lat=None, lon=None, taken=None, taken_route=None, taken_station=None) -> dict:
    """Every photo from every device comes in here: stored, judged on a quest or described on a wander,
    and recorded as a moment, so the nightly tidy-up knows about it. Returns the verdict plus what the
    spirit says (`say`) and the moment's id.

    A photo sent late (`taken`: the phone kept it while the relay was unreachable) says which route and
    station it was taken on. If the player has moved on since, it is only described, in that route's
    city, and filed under that route: it never solves a station it wasn't taken for."""
    path, idx, epoch, already_solved = snapshot()
    route = load_route(path)
    where = {"lat": lat, "lon": lon} if lat is not None else {}
    elsewhere = False
    if taken:
        where["taken_at"] = round(taken, 3)
        if taken_route:
            here = idx + 1
            elsewhere = taken_route != route["code"] or (route["mode"] == "quest" and taken_station not in (None, here))
            if elsewhere:
                then = next((f for f in route_files() if f.stem == taken_route), None)
                route = load_route(then) if then else {**route, "code": taken_route}
    fname = new_name(".jpg")
    (CAPTURES / fname).write_bytes(jpeg)
    if not elsewhere:
        with lock:
            if state["epoch"] == epoch:          # still the play-through this photo was taken in
                state.update(photo=fname, photo_at=time.time(), photo_from=device_id, verdict=None)
    describe = route["mode"] == "wander" or elsewhere
    log(f"收到照片 {len(jpeg) // 1024}KB 来自 {device_id}" + ("（补传，拍在别的路线/站）" if elsewhere else "（漫游）" if describe else ""))
    if describe:
        v = ask_model(jpeg, {"instruction": LOOK.format(city=route.get("city") or "这座城")}, CAPTURES / fname)
        with lock:
            if state["photo"] == fname:
                state["verdict"] = v
        say = v.get("text") or "我看到了，但说不上来。"
        m = journal.add(route["code"], "look", device_id, photo=fname, said=say, people=people_in(v), **where)
        log(f"漫游看图 · {say[:30]} · 有人={people_in(v)} · {v.get('source')}")
        return {"verdict": v, "say": say, "moment": m["id"]}
    v = judge(fname, jpeg, route["stations"], idx, epoch, already_solved)
    with lock:      # recorded under the same lock /judge uses, so a verdict by hand can't fall in between
        cur = state["verdict"] or {}
        if state["photo"] == fname and cur.get("source") == "manual":
            v = {**v, "match": cur["match"], "source": "manual", "text": "" if cur["match"] else MANUAL_NO}
        say = unlocked_text(route["stations"][idx], v) if v.get("match") is True else (v.get("text") or "")
        m = journal.add(route["code"], "judge", device_id, photo=fname, match=v.get("match"), said=say,
                        people=people_in(v), station=idx + 1, **where)
    return {"verdict": v, "say": say, "moment": m["id"]}


def see_later(jpeg: bytes, device_id: str) -> None:
    """For devices that don't wait for the answer (XIAO, Watcher alarm push): the web page shows it."""
    def run():
        try:
            see(jpeg, device_id)
        except Exception as e:
            log(f"看图失败（{device_id}）{type(e).__name__}: {e}")
    threading.Thread(target=run, daemon=True).start()


def people_in(verdict: dict):
    """True / False as the model said; None when it didn't say (treated like people: not kept)."""
    p = verdict.get("people")
    return p if isinstance(p, bool) else None


def keep_reply(kept: dict | None) -> str:
    if not kept:
        return "还没有可以收的，先让我看一眼。"
    if kept.get("people") is True:
        return "收起来了。照片里有人，照片今晚会删掉，只留我说的话。"
    if kept.get("people") is None:
        return "收起来了。我没看清照片里有没有人，照片今晚会删掉，只留我说的话。"
    return "收起来了。"


def unlocked_text(station: dict, verdict: dict) -> str:
    """What the spirit says when a station is solved."""
    story = station.get("story", "").replace("（占位）", "")
    if station.get("mode") == "describe" and verdict.get("text"):
        return verdict["text"] + "。" + story
    return story


def compact_state() -> dict:
    with lock:                                   # one consistent view: route, station, verdict together
        path, idx, v, solved = Path(state["route"]), state["station"], state["verdict"], list(state["solved"])
        devices = {k: dict(d) for k, d in state["devices"].items()}
    route = load_route(path)
    stations = route["stations"]
    idx = min(idx, max(len(stations) - 1, 0))    # in case the route file was edited while playing
    base = {"title": route.get("title", "Loci"), "route": route["code"], "mode": route["mode"],
            "city": route.get("city", ""), "verdict": v, "devices": devices}
    if not stations:                                 # wander: no stations, nothing to solve
        return {**base, "station": 0, "total": 0, "solved": [], "unlocked": None,
                "riddle": route.get("intro", "随便走走。看到有意思的，就让我看看。")}
    st = stations[idx]
    solved_now = idx in solved
    return {
        **base,
        "station": idx + 1,
        "total": len(stations),
        "solved": [i + 1 for i in solved],
        "riddle": st.get("riddle", ""),
        "unlocked": unlocked_text(st, v or {}) if solved_now else None,
    }


# ── speech for the Watcher ───────────────────────────────────────────────

def tts_key(text: str) -> str:
    return hashlib.sha1(f"{TTS_VOICE}|{text}".encode()).hexdigest()[:16]


def voice_url(text: str) -> str | None:
    """Synthesize once and hand clients a URL to play (phones play it; glasses use their own voice)."""
    if not text:
        return None
    try:
        tts_wav(text)
    except Exception as e:
        log(f"合成语音失败 {type(e).__name__}")
        return None
    return f"/api/v1/voice/{tts_key(text)}.wav"


def tts_wav(text: str) -> bytes:
    """macOS say → 16 kHz mono 16-bit WAV with a plain 44-byte header
    (the Watcher reads sample rate from a fixed header; a JUNK chunk would confuse it)."""
    key = tts_key(text)
    cached = TTS_DIR / f"{key}.wav"
    if cached.exists():
        return cached.read_bytes()
    with tempfile.TemporaryDirectory() as td:
        aiff, raw = Path(td) / "t.aiff", Path(td) / "t.wav"
        subprocess.run(["say", "-v", TTS_VOICE, "-o", str(aiff), text], check=True, timeout=60)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(raw)],
                       check=True, timeout=60)
        with wave.open(str(raw), "rb") as w:
            frames, params = w.readframes(w.getnframes()), w.getparams()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(frames)
    data = buf.getvalue()
    cached.write_bytes(data)
    return data


# ── talking to the spirit (Watcher push-to-talk) ─────────────────────────

def voiced_seconds(pcm: bytes) -> float:
    """Seconds of 30 ms frames clearly above the clip's noise floor (16 kHz mono 16-bit PCM)."""
    a = array.array("h", pcm[: len(pcm) // 2 * 2])
    rms = [math.sqrt(sum(x * x for x in a[i:i + 480]) / 480) for i in range(0, len(a) - 479, 480)]
    if not rms:
        return 0.0
    threshold = min(1500, max(600, 2 * sorted(rms)[len(rms) // 10]))   # follows a noisy room, within limits
    return sum(r > threshold for r in rms) * 0.03


def transcribe(pcm: bytes) -> str:
    """Speech → text through an OpenAI-compatible transcription endpoint (ASR_URL)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm[: 55 * 32000])            # keep under the usual 60 s request limit
    b = "loci" + os.urandom(8).hex()
    body = f'--{b}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{ASR_MODEL}\r\n'.encode() if ASR_MODEL else b""
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="voice.wav"\r\n'
             f"Content-Type: audio/wav\r\n\r\n").encode() + buf.getvalue() + f"\r\n--{b}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={b}"}
    if ASR_KEY:
        headers["Authorization"] = f"Bearer {ASR_KEY}"
    no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # usually a local server
    with no_proxy.open(urllib.request.Request(ASR_URL, data=body, headers=headers), timeout=60) as r:
        return json.loads(r.read()).get("text", "").strip()


def ask_text(prompt: str) -> str:
    """A short text answer from the same backend the judge uses ("" when there is none)."""
    if API_KEY:
        body = {"model": MODEL, "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
            headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return "".join(b.get("text", "") for b in json.loads(r.read()).get("content", [])).strip()
    if CLAUDE_BIN:
        env = claude_env()
        r = subprocess.run([CLAUDE_BIN, "-p", "--setting-sources", "", "--model", MODEL, "--output-format", "text", prompt],
                           capture_output=True, text=True, timeout=60, env=env, cwd=str(HERE))
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout)[-200:])
        return r.stdout.strip()
    return ""


def to_pcm16k(audio: bytes) -> bytes:
    """Any recording a phone makes (AAC in MP4, Opus in WebM, WAV) → 16 kHz mono 16-bit PCM."""
    if not FFMPEG:
        raise RuntimeError("ffmpeg not installed")
    r = subprocess.run([FFMPEG, "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
                       input=audio, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[-200:])
    return r.stdout


def hear(audio: bytes) -> str:
    """A phone recording → text, with the same near-silence guard as the Watcher ("" = say it again)."""
    pcm = to_pcm16k(audio)
    if voiced_seconds(pcm) < MIN_VOICED_S or not ASR_URL:
        return ""
    return transcribe(pcm)


def spirit_reply(heard: str, device_id: str) -> tuple[str, str]:
    """The spirit's answer and the route it was about: knows the route and where the player is; on a quest
    it hints, never answers. If the route is switched while the model thinks, the answer is dropped."""
    with lock:
        path, idx, solved = Path(state["route"]), state["station"], state["station"] in state["solved"]
        epoch = state["epoch"]
        history = list(talks.get(device_id, []))
        last = (state.get("verdict") or {}).get("text", "")
    route = load_route(path)
    stations, spirit = route["stations"], route.get("spirit") or SPIRIT   # a route may set its own voice
    idx = min(idx, max(len(stations) - 1, 0))
    if not stations:
        where = f"玩家在{route.get('city') or '城里'}随便走走。" + (f"你刚才看到并说过：{last}" if last else "")
    elif solved:
        where = f"玩家已经解开了第{idx + 1}站。"
    else:
        st = stations[idx]
        where = f"玩家在第{idx + 1}站，谜面是：{st.get('riddle', '')} 要找的是：{st.get('target', '')}。可以给提示，别直接说出答案。"
    past = "".join(f"玩家：{h}\n地灵：{r}\n" for h, r in history)
    reply = ask_text(f"{spirit}\n{where}\n{past}玩家：{heard}\n地灵：")
    reply = " ".join(reply.split()).removeprefix("地灵：").strip("「」\"' ")[:80] or "嗯，我在听。"
    with lock:
        if state["epoch"] != epoch:          # another route now: don't carry this city's talk into it
            return "路线换了，刚才那句不算，再问我一次。", route["code"]
        talks[device_id] = (history + [(heard, reply)])[-3:]
    return reply, route["code"]


def watcher_talk(pcm: bytes, device_id: str) -> bytes:
    """Push-to-talk on the stock firmware (app/app_voice_interaction.c): the request body is raw 16 kHz
    mono 16-bit PCM; the reply is JSON, then ---sensecraftboundary---, a newline, and a WAV to play."""
    t0 = time.time()
    heard, voiced = "", voiced_seconds(pcm)
    try:
        if voiced < MIN_VOICED_S:
            reply = "我没听清，按住滚轮再说一遍？"
        elif not ASR_URL:
            reply = "我还听不懂说话，等我学会了再聊。"
        else:
            heard = transcribe(pcm)
            reply = spirit_reply(heard, device_id)[0] if heard else "我没听清，再说一遍？"
    except Exception as e:
        log(f"语音对话出错 {type(e).__name__}: {e}")
        reply = "我刚才走神了，再说一遍好吗？"
    wav = tts_wav(reply)
    log(f"地灵对话 人声{voiced:.1f}s 听到={heard[:30]!r} 回={reply[:30]!r} · {time.time() - t0:.1f}s")
    head = {"code": 200, "data": {"mode": 0, "duration": max(0, len(wav) - 44) * 1000 // 32000,
                                  "stt_result": heard, "screen_text": reply}}
    return json.dumps(head, ensure_ascii=False).encode() + b"---sensecraftboundary---\n" + wav


def watcher_vision(req: dict, device_id: str) -> dict:
    """SenseCAP Watcher stock firmware image analysis (tf_module_img_analyzer.c):
    request {img: base64 jpeg, prompt, audio_txt, type}; reply {code: 200, data: {state, type, audio}}.
    state=1 makes the Watcher run its alarm action and play `audio` (WAV)."""
    jpeg = base64.b64decode(req.get("img", ""))
    log(f"Watcher 任务 prompt={str(req.get('prompt'))[:40]!r} audio_txt={str(req.get('audio_txt'))[:30]!r} type={req.get('type')}")
    out = see(jpeg, device_id)
    v = out["verdict"]
    speech = ""
    if v.get("match") is True:
        speech = out["say"]
    elif WATCHER_SPEAK_MISS:
        speech = "还没找到哦，再看看。"
    data = {"state": 1 if speech else 0, "type": req.get("type", 0)}
    if speech:
        try:
            data["audio"] = base64.b64encode(tts_wav(speech)).decode()
        except Exception as e:
            log(f"TTS 失败 {type(e).__name__}，只回 state")
    log(f"回 Watcher state={data['state']} audio={len(data.get('audio', '')) * 3 // 4 // 1024}KB")
    return {"code": 200, "data": data}


# ── web page ─────────────────────────────────────────────────────────────

PAGE = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Loci</title>
<style>
body{font-family:-apple-system,"PingFang SC",sans-serif;margin:0;background:#111;color:#eee}
main{max-width:760px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0 0 8px}.k{color:#999;font-size:13px}
.card{background:#1d1d1d;border-radius:12px;padding:14px;margin:12px 0}
img{width:100%;border-radius:8px;background:#000;min-height:120px}
.ok{color:#5ad17a}.no{color:#ff7a6b}.wait{color:#f2c94c}
button{font-size:16px;padding:10px 14px;margin:4px 4px 0 0;border-radius:8px;border:0;background:#333;color:#eee}
button.g{background:#2f6f43}button.r{background:#7a2f2a}
.story{font-size:18px;line-height:1.6;white-space:pre-wrap}
.devs{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px}
.dev{background:#1d1d1d;border-radius:10px;padding:10px;font-size:13px;border-left:4px solid #444}
.dev.live{border-color:#5ad17a}.dev.idle{border-color:#f2c94c}
.dev b{display:block;font-size:14px;margin-bottom:2px}
pre{font-size:12px;color:#888;white-space:pre-wrap}
</style></head><body><main>
<h1 id="title">Loci</h1><div class="k" id="prog"></div>
<div class="devs" id="devs"></div>
<div class="card"><div class="k">这一站</div><div class="story" id="riddle"></div></div>
<div class="card"><img id="photo" alt="还没有照片"><div class="k" id="from"></div><div id="verdict" class="wait">等设备拍照</div></div>
<div class="card" id="storybox" hidden><div class="k">解锁</div><div class="story" id="story"></div></div>
<div>
<button class="g" onclick="post('/judge?ok=1')">判对</button><button class="r" onclick="post('/judge?ok=0')">判错</button>
<button onclick="post('/station?d=-1')">上一站</button><button onclick="post('/station?d=1')">下一站</button>
<button onclick="speakNow()">朗读</button><button onclick="post('/reset')">重来</button>
</div>
<pre id="log"></pre>
</main><script>
const $=id=>document.getElementById(id);
const esc=t=>String(t??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const KINDS={watcher:"SenseCAP Watcher（地灵本体）",xiao:"XIAO ESP32S3 Sense（可穿戴）",glasses:"Rokid 眼镜（玩家视角）",phone:"手机（玩家视角）"};
let last="",spoken="",cur=null;
async function post(u){await fetch(u,{method:"POST"});tick();}
function speak(t,lang){if(!t)return;speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(t);u.lang=lang||"zh-CN";speechSynthesis.speak(u);}
function ago(t){const s=Math.round(Date.now()/1000-t);return s<60?s+" 秒前":s<3600?Math.round(s/60)+" 分钟前":Math.round(s/3600)+" 小时前";}
function renderDevices(devs){
  const byKind={};for(const [id,d] of Object.entries(devs)){(byKind[d.kind]=byKind[d.kind]||[]).push([id,d]);}
  let html="";
  for(const [kind,label] of Object.entries(KINDS)){
    const list=byKind[kind]||[];
    if(!list.length){html+=`<div class="dev"><b>${label}</b>还没连上来</div>`;continue;}
    for(const [id,d] of list){const s=Date.now()/1000-d.last_seen;
      html+=`<div class="dev ${s<60?"live":s<600?"idle":""}"><b>${label}</b>${esc(id)} · ${ago(d.last_seen)}<br>${esc(d.last_event)} · 共 ${d.count} 次</div>`;}
  }
  for(const [kind,list] of Object.entries(byKind)){if(KINDS[kind])continue;
    for(const [id,d] of list)html+=`<div class="dev live"><b>${esc(id)}</b>${ago(d.last_seen)} · ${esc(d.last_event)}</div>`;}
  $("devs").innerHTML=html;
}
async function tick(){
  const s=await (await fetch("/api/v1/state")).json();const full=await (await fetch("/state")).json();
  const st=full.stations[full.station];const v=s.verdict;
  cur={st:st,v:v,unlocked:s.unlocked};
  $("title").textContent=s.title;$("prog").textContent=`第 ${s.station} / ${s.total} 站 · 已解 ${s.solved.length}`;
  renderDevices(s.devices);
  $("riddle").textContent=s.riddle;
  if(full.photo&&full.photo!==last){$("photo").src="/photo/"+full.photo;last=full.photo;}
  $("from").textContent=full.photo_from?("来自 "+full.photo_from):"";
  const el=$("verdict");
  if(!v){el.className="wait";el.textContent=full.photo?"判定中…":"等设备拍照";}
  else{el.className=v.match===true?"ok":v.match===false?"no":"wait";
    el.textContent=(v.match===true?"✓ ":v.match===false?"✗ ":"? ")+v.text+"  ["+v.source+"]";}
  $("storybox").hidden=!s.unlocked;$("story").textContent=s.unlocked||"";
  const key=s.station+":"+(s.unlocked||"");
  if(s.unlocked&&spoken!==key){spoken=key;speak(s.unlocked,st.lang);}
  $("log").textContent=full.log.slice().reverse().join("\n");
}
function speakNow(){if(cur)speak(cur.unlocked||cur.st.riddle,cur.st.lang);}
setInterval(tick,1000);tick();
</script></body></html>"""


# ── HTTP ─────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> bytes:
        """The whole body, plain or chunked. Raises ValueError if it is cut off or over MAX_BODY."""
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks, total, blank = [], 0, 0
            while True:
                raw = self.rfile.readline(1024)
                if not raw:
                    raise ValueError("connection closed mid-body")
                size_line = raw.strip()
                if not size_line:
                    blank += 1
                    if blank > 4:
                        raise ValueError("no chunk size")
                    continue
                size = int(size_line.split(b";")[0], 16)
                if size < 0:
                    raise ValueError("negative chunk size")
                if size == 0:
                    self.rfile.readline(1024)  # trailing CRLF after the last chunk
                    break
                total += size
                if total > MAX_BODY:
                    raise ValueError("body too large")
                chunk = self.rfile.read(size)
                if len(chunk) != size:
                    raise ValueError("connection closed mid-body")
                chunks.append(chunk)
                self.rfile.readline(1024)      # CRLF after each chunk
            return b"".join(chunks)
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n < 0:
            raise ValueError("negative length")
        if n > MAX_BODY:
            raise ValueError("body too large")
        body = self.rfile.read(n) if n else b""
        if len(body) != n:
            raise ValueError("connection closed mid-body")
        return body

    def ip(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def device_id(self, default_kind: str) -> tuple[str, str]:
        dev = self.headers.get("X-Device") or ""
        if not dev:
            q = re.search(r"(?:^|&)device=([^&]*)", self.path.partition("?")[2])
            dev = q.group(1) if q else ""
        if not re.fullmatch(r"[\w.-]{1,48}", dev):    # it ends up on the operator page
            dev = ""
        if not dev:
            dev = f"{default_kind}-{self.ip()}"
        return dev, device_kind(dev, default_kind)

    @staticmethod
    def query_float(query: str, name: str):
        m = re.search(rf"(?:^|&){name}=(-?[\d.]+)", query)
        return float(m.group(1)) if m else None

    def watcher_id(self) -> str:
        eui = re.sub(r"[^\w-]", "", self.headers.get("API-OBITER-DEVICE-EUI", ""))
        if eui:
            return f"watcher-{eui[-6:]}"
        auth = self.headers.get("Authorization", "")
        return "watcher-" + (hashlib.sha1(auth.encode()).hexdigest()[:6] if auth else self.ip())

    def outside(self) -> bool:
        """From the internet (Cloudflare tunnel), from a page on another site open in a browser here, or
        through a domain name that points at this machine (DNS rebinding). Everyone else is this Mac or a
        device on the LAN (Host is localhost or an IP address, and any Origin is the relay itself)."""
        if self.headers.get("Cf-Connecting-Ip"):
            return True
        host = self.headers.get("Host") or ""
        name = re.match(r"(\[[^\]]*\]|[^:]*)", host).group(1).strip("[]")
        if name and name != "localhost":
            try:
                ipaddress.ip_address(name)
            except ValueError:
                return True
        origin = self.headers.get("Origin")
        return bool(origin) and urlsplit(origin).netloc != host

    def from_tunnel_blocked(self) -> bool:
        """A request from outside may only reach the public API, and only with the key when LOCI_KEY is
        set (the phone page itself is the one public thing without a key)."""
        if not self.outside():
            return False
        path, _, query = self.path.partition("?")
        if path not in PUBLIC_PATHS and not path.startswith(PUBLIC_PREFIXES):
            self.send(404, {"error": "not found"})
            return True
        m = re.search(r"(?:^|&)key=([\w-]+)", query)
        given = self.headers.get("X-Loci-Key") or (m.group(1) if m else "")
        if LOCI_KEY and path != "/play" and not hmac.compare_digest(given, LOCI_KEY):
            self.send(401, {"error": "key required"})
            return True
        return False

    def watcher_auth_ok(self) -> bool:
        if WATCHER_TOKEN and self.headers.get("Authorization", "") != WATCHER_TOKEN:
            log("Watcher 请求 Token 不对，拒绝")
            return False
        return True

    def cors_headers(self):
        """Browser-hosted runtimes (AIUI preview/Studio) may send credentials, so echo the origin, not *."""
        origin = self.headers.get("Origin")
        self.send_header("Access-Control-Allow-Origin", origin or "*")
        if origin:
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        asked = self.headers.get("Access-Control-Request-Headers", "")
        log(f"跨域预检 {self.path.partition('?')[0]} 来自 {self.headers.get('Origin')} 头={asked!r}")
        self.send_response(204)
        self.cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", asked or "Content-Type, X-Device")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.from_tunnel_blocked():
            return
        path = self.path.partition("?")[0]
        if path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if path == "/state":
            with lock:
                snap = json.loads(json.dumps(state))
            route = load_route(Path(snap["route"]))
            snap.update(stations=route["stations"], title=route.get("title", "Loci"))
            return self.send(200, snap)
        if path == "/api/v1/state":
            if "device=" in self.path or self.headers.get("X-Device"):
                dev, kind = self.device_id("glasses")
                seen(dev, kind, "查看进度", self.ip())
            return self.send(200, compact_state())
        if path == "/play":                          # the phone client
            return self.send(200, PHONE_PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/v1/routes":
            out = []
            for p in route_files():
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                out.append({"route": p.stem, "title": d.get("title", p.stem), "city": d.get("city", ""),
                            "mode": d.get("mode") or ("quest" if d.get("stations") else "wander")})
            return self.send(200, {"current": load_route()["code"], "routes": out})
        if path == "/api/v1/digest":
            day = (re.search(r"(?:^|&)day=(\d{8})", self.path.partition("?")[2]) or [None, None])[1]
            return self.send(200, journal.digest(day))
        m = re.match(r"^/api/v1/voice/([0-9a-f]{16})\.wav$", path)
        if m and (TTS_DIR / f"{m.group(1)}.wav").exists():
            return self.send(200, (TTS_DIR / f"{m.group(1)}.wav").read_bytes(), "audio/wav")
        m = re.match(r"^/(photo|api/v1/photo-file)/([\w.-]+\.jpg)$", path)
        if m and (CAPTURES / m.group(2)).exists():
            # /photo/ is the operator page on this Mac; anything else gets only photos the model said
            # have no people in them (strangers' faces are for judging on the spot, never sent out)
            if m.group(1) == "photo" or journal.no_people(m.group(2)):
                return self.send(200, (CAPTURES / m.group(2)).read_bytes(), "image/jpeg")
            return self.send(404, {"error": "not found"})
        log(f"陌生 GET {self.path}")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if self.from_tunnel_blocked():     # before reading: an outsider's body is never read
            self.close_connection = True
            return
        try:
            body = self.read_body()
        except ValueError as e:
            log(f"请求体不对 {path}：{e}")
            self.close_connection = True
            return self.send(413 if "large" in str(e) else 400, {"error": str(e)})

        if path == "/api/v1/talk":                  # any client: text (glasses) or a recording (phone) → the spirit's reply
            dev, kind = self.device_id("glasses")
            seen(dev, kind, "说话", self.ip())
            ctype = self.headers.get("Content-Type", "")
            try:
                if ctype.startswith(("audio/", "video/", "application/octet-stream")):
                    text = hear(body)
                else:
                    text = str(json.loads(body or b"{}").get("text", "")).strip()
            except json.JSONDecodeError:
                return self.send(400, {"error": "bad json"})
            except Exception as e:
                log(f"听不了录音 {type(e).__name__}: {e}")
                text = ""
            kept, about = None, None
            try:
                if text and wants_keep(text):
                    kept = journal.keep(dev)
                    reply = keep_reply(kept)
                elif text:
                    reply, about = spirit_reply(text, dev)
                else:
                    reply = "我没听清，再说一遍？"
            except Exception as e:
                log(f"地灵回话失败 {type(e).__name__}: {e}")
                reply = "我刚才走神了，再说一遍好吗？"
            if text:
                journal.add(about or load_route()["code"], "talk", dev, heard=text, said=reply,
                            lat=self.query_float(query, "lat"), lon=self.query_float(query, "lon"))
            log(f"地灵对话（{dev}）听到={text[:30]!r} 回={reply[:30]!r}")
            return self.send(200, {"heard": text, "reply": reply, "say": reply, "voice": voice_url(reply),
                                   "kept": kept["id"] if kept else None, **compact_state()})

        if path == "/api/v1/keep":                  # any client: keep the latest look/judgement (or a given moment)
            dev, kind = self.device_id("glasses")
            try:
                mid = json.loads(body or b"{}").get("moment")
            except Exception:
                mid = None
            kept = journal.keep(dev, mid)
            say = keep_reply(kept)
            log(f"收起来（{dev}）{kept['id'] if kept else '没有可收的'}")
            return self.send(200, {"kept": kept["id"] if kept else None, "say": say, "voice": voice_url(say)})

        if path == "/api/v1/route":                 # any client: switch route (a new city, a new quest)
            try:
                code = str(json.loads(body or b"{}").get("route", ""))
            except Exception:
                return self.send(400, {"error": "bad json"})
            target = next((p for p in route_files() if p.stem == code), None)
            if not target:
                return self.send(404, {"error": "no such route"})
            with lock:
                state.update(route=str(target), station=0, solved=[], verdict=None, photo=None, photo_from=None,
                             epoch=state["epoch"] + 1)
                talks.clear()
            log(f"换路线 → {code}")
            st = compact_state()
            return self.send(200, {**st, "say": st["riddle"], "voice": voice_url(st["riddle"])})

        if path == "/api/v1/log":                   # any client: a short diagnostic line (devices without a console)
            dev, kind = self.device_id("glasses")
            seen(dev, kind, "日志", self.ip())
            try:
                msg = str(json.loads(body or b"{}").get("msg", ""))
            except Exception:
                msg = body.decode(errors="replace")
            log(f"设备日志（{dev}）{' '.join(msg.split())[:300]}")
            return self.send(200, {"ok": True})

        if path == "/api/v1/next":                  # any client: move on, but only from a solved station
            with lock:                              # the route is read under the same lock that moves the station
                stations = load_route(Path(state["route"]))["stations"]
                if state["station"] in state["solved"] and state["station"] < len(stations) - 1:
                    state["station"] += 1
                    # a new station starts without a photo: the last one was evidence for the station before
                    state.update(verdict=None, photo=None, photo_from=None)
                    moved = True
                else:
                    moved = False
            if moved:
                log(f"下一站（{self.device_id('glasses')[0]}）")
            return self.send(200, {"moved": moved, **compact_state()})

        if path == "/upload":                       # XIAO: raw JPEG, async judge
            if not body.startswith(b"\xff\xd8"):
                return self.send(400, {"error": "not a jpeg"})
            dev, kind = self.device_id("xiao")
            seen(dev, kind, "拍照", self.ip())
            see_later(body, dev)
            return self.send(200, {"ok": True})

        if path == "/api/v1/photo":                 # any client: JPEG → verdict (quest) or a look (wander)
            jpeg, lat, lon = body, self.query_float(query, "lat"), self.query_float(query, "lon")
            if body[:1] == b"{":
                try:
                    req = json.loads(body)
                    jpeg = base64.b64decode(req.get("img", ""))
                    lat, lon = req.get("lat", lat), req.get("lon", lon)
                except Exception:
                    return self.send(400, {"error": "bad json"})
            if not jpeg.startswith(b"\xff\xd8"):
                return self.send(400, {"error": "not a jpeg"})
            dev, kind = self.device_id("glasses")
            seen(dev, kind, "拍照", self.ip())
            at = self.query_float(query, "at")      # ms since 1970, set by a phone sending a photo late
            if at:
                prev = journal.find_taken(dev, round(at / 1000, 3))
                if prev:                            # sent again because the answer got lost: same answer
                    log(f"补传重复（{dev}）{prev['id']}，回上次的结果")
                    said = prev.get("said") or ""
                    out = {"verdict": {"match": prev.get("match"), "text": said, "people": prev.get("people"),
                                       "source": "repeat"}, "say": said, "moment": prev["id"]}
                    return self.send(200, {**compact_state(), **out, "voice": voice_url(said)})
            r = re.search(r"(?:^|&)route=([\w.-]+)", query)
            st = self.query_float(query, "station")
            out = see(jpeg, dev, lat, lon, taken=at / 1000 if at else None,
                      taken_route=r.group(1) if r else None, taken_station=int(st) if st else None)
            return self.send(200, {**compact_state(), **out, "voice": voice_url(out["say"])})

        if path == "/audio":                        # XIAO: raw WAV, stored only
            dev, kind = self.device_id("xiao")
            seen(dev, kind, "录音", self.ip())
            fname = new_name(".wav")
            (CAPTURES / fname).write_bytes(body)
            with lock:
                state["audio"] = fname
            log(f"收到录音 {len(body) // 1024}KB 来自 {dev}（还没接语音识别）")
            return self.send(200, {"ok": True, "file": fname})

        if path == "/v1/watcher/vision":            # Watcher stock firmware, Device AI Service
            if not self.watcher_auth_ok():
                return self.send(401, {"code": 401})
            dev = self.watcher_id()
            seen(dev, "watcher", "看图任务", self.ip())
            try:
                return self.send(200, watcher_vision(json.loads(body or b"{}"), dev))
            except Exception as e:
                log(f"Watcher 请求处理失败 {type(e).__name__}: {e}")
                return self.send(200, {"code": 500, "data": {"state": 0}})

        if path == "/v1/notification/event":        # Watcher HTTP push from a task alarm
            dev = self.watcher_id()
            seen(dev, "watcher", "报警推送", self.ip())
            try:
                req = json.loads(body or b"{}")
                events = req.get("events") or {}
                img = events.get("img") if isinstance(events, dict) else None
                if img:
                    see_later(base64.b64decode(img), dev)
                log(f"Watcher 推送 text={str(events.get('text') if isinstance(events, dict) else '')[:40]!r} 带图={bool(img)}")
            except Exception as e:
                log(f"Watcher 推送解析失败 {type(e).__name__}: {e}")
            return self.send(200, {"code": 200})

        if path == "/v2/watcher/talk/audio_stream":  # Watcher push-to-talk: answer out loud
            dev = self.watcher_id()
            seen(dev, "watcher", "语音", self.ip())
            (CAPTURES / f"voice-{new_name('')}.bin").write_bytes(body)   # kept for tuning
            try:
                return self.send(200, watcher_talk(body, dev), "application/octet-stream")
            except Exception as e:
                log(f"语音回复失败 {type(e).__name__}: {e}")
                return self.send(200, {"code": 500, "data": {}})

        if path.startswith("/v2/watcher/talk/"):     # e.g. view_task_detail: not handled
            log(f"Watcher 语音 {path} {len(body)}B（不支持）")
            return self.send(200, {"code": 501, "msg": "not supported by this relay"})

        if path == "/judge":                        # manual verdict (fallback, and for demos)
            ok = "ok=1" in query
            with lock:
                state["verdict"] = {"match": ok, "text": "人工判定", "source": "manual"}
                if ok and state["station"] not in state["solved"]:
                    state["solved"].append(state["station"])
                if not ok and state["station"] in state["solved"]:   # overruling a wrong "found it"
                    state["solved"].remove(state["station"])
                state["epoch"] += 1                                    # and any judgement still running
                photo = state["photo"]
                stations = load_route(Path(state["route"]))["stations"]
                station = stations[state["station"]] if state["station"] < len(stations) else {}
            if photo:   # the record says what the person decided, not the model's reason any more
                journal.judged(photo, ok, unlocked_text(station, {}) if ok else MANUAL_NO)
            log(f"人工判定 {ok}")
            return self.send(200, {"ok": True})
        if path == "/station":
            d = -1 if "d=-1" in query else 1
            with lock:
                stations = load_route(Path(state["route"]))["stations"]
                state["station"] = max(0, min(len(stations) - 1, state["station"] + d))
                state.update(verdict=None, photo=None, photo_from=None)
            return self.send(200, {"ok": True})
        if path == "/reset":
            with lock:
                state.update(station=0, verdict=None, solved=[], photo=None, photo_from=None, epoch=state["epoch"] + 1)
            log("重来")
            return self.send(200, {"ok": True})

        log(f"陌生 POST {self.path} {len(body)}B 头={body[:120]!r}")
        self.send(404, {"code": 404, "error": "not found"})


def serve(host=HOST, port=PORT):
    return ThreadingHTTPServer((host, port), Handler)


def nightly(day: str | None = None) -> dict:
    """Delete the photos nobody kept, then write the day's digest next to the photos (after the prune, so
    the digest is newer than the journal it summarises and reflects what is left)."""
    day = day or time.strftime("%Y%m%d")
    deleted = journal.prune(day)
    digest = journal.digest(day)
    out = CAPTURES / "digests"
    out.mkdir(exist_ok=True)
    (out / f"{day}.json").write_text(json.dumps(digest, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"每晚整理 {day}：拾遗 {len(digest['kept'])} 个，删掉照片 {deleted} 张")
    return {**digest, "deleted": deleted}


def nightly_tick(done: set) -> None:
    """Today: once, at PRUNE_AT. Any earlier day (photos after PRUNE_AT, a night the Mac slept, a run
    that failed): again until nothing is left to delete. A day goes into `done` only once it succeeded."""
    today = time.strftime("%Y%m%d")
    yesterday = time.strftime("%Y%m%d", time.localtime(time.time() - 86400))
    jobs = [d for d in journal.days() if d < today and d not in done]          # a day that is over: final pass
    if time.strftime("%H:%M") >= PRUNE_AT and f"{today}+early" not in done:   # today at PRUNE_AT: first pass
        jobs.append(today)
    for day in jobs:
        try:
            digest = CAPTURES / "digests" / f"{day}.json"
            stale = not digest.exists() or journal.changed_at(day) > digest.stat().st_mtime   # talk/keeps after it
            if day == today or journal.pending(day) or stale:
                nightly(day)
            if day == today:
                done.add(f"{day}+early")
            elif day < yesterday:          # yesterday can still get a late moment (judged after midnight)
                done.add(day)
        except Exception as e:
            log(f"每晚整理 {day} 失败 {type(e).__name__}: {e}，一分钟后再试")


def nightly_loop():
    done = set()
    while True:
        nightly_tick(done)
        time.sleep(60)


if __name__ == "__main__":
    stations, title = load_stations()
    threading.Thread(target=nightly_loop, daemon=True).start()
    print(f"Loci relay · {title} · {len(stations)} 站 · 判定 {judge_backend()} · 模型 {MODEL}"
          f" · 路线 {len(route_files())} 条 · 密钥 {'开' if LOCI_KEY else '关'} · 每晚 {PRUNE_AT} 整理")
    print(f"浏览器打开 http://localhost:{PORT}/ ；" + ("设备填这台电脑的 IP:{PORT}（ipconfig getifaddr en0）".format(PORT=PORT)
          if HOST == "0.0.0.0" else f"只听 {HOST}：局域网连不上，手机走隧道"))
    serve().serve_forever()
