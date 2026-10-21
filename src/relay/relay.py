#!/usr/bin/env python3
"""Loci relay — the brain. Devices send photos here; the relay judges "did they find it?",
speaks the story back, and serves a web page for the route.

Standard library only. Run:
    python3 relay.py                      # 0.0.0.0:8787, route from ./stations.json

Judging backend, first that applies:
    1. ANTHROPIC_API_KEY set   → Anthropic API (fastest)
    2. `claude` CLI installed  → `claude -p` reads the photo (~6–8 s each, uses your Claude login)
    3. otherwise, or JUDGE=manual → judge by hand with the buttons on the web page

Environment: RELAY_PORT, MODEL (default claude-sonnet-5-5), STATIONS (route file), JUDGE (manual),
CLAUDE_PROXY (default http://127.0.0.1:7890), TTS_VOICE (default Tingting), WATCHER_TOKEN,
WATCHER_SPEAK_MISS=1, LOCI_DATA (where photos/audio are stored; default ./captures),
ASR_URL / ASR_KEY / ASR_MODEL (an OpenAI-compatible /v1/audio/transcriptions endpoint, e.g. a local
Whisper or Qwen-ASR server; without it the Watcher's push-to-talk gets a polite "I can't listen yet").

Device endpoints (see docs/api.md):
    POST /upload                         XIAO ESP32S3 Sense: raw JPEG body, header X-Device
    POST /audio                          XIAO: raw WAV body
    POST /v1/watcher/vision              SenseCAP Watcher stock firmware, "Device AI Service"
    POST /v1/notification/event          SenseCAP Watcher HTTP push (task alarm with image)
    POST /v2/watcher/talk/audio_stream   SenseCAP Watcher push-to-talk → the spirit answers out loud
    GET  /api/v1/state                   any client (glasses, phone): compact progress
    POST /api/v1/photo                   any client: JPEG (raw or JSON {img}) → verdict in the response
    POST /api/v1/talk                    any client: {"text": "..."} → the spirit's reply
    POST /api/v1/next                    any client: go to the next station, only once this one is solved

Requests that arrive through a Cloudflare tunnel (they carry Cf-Connecting-Ip) may only use the
/api/v1/* endpoints above; the web page, photos and operator buttons stay on the local network.
"""

import array
import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAPTURES = Path(os.environ.get("LOCI_DATA", HERE / "captures"))
CAPTURES.mkdir(parents=True, exist_ok=True)
TTS_DIR = CAPTURES / "tts"
TTS_DIR.mkdir(exist_ok=True)
PORT = int(os.environ.get("RELAY_PORT", "8787"))
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
PUBLIC_PATHS = {"/api/v1/state", "/api/v1/photo", "/api/v1/talk", "/api/v1/next"}  # all a tunnel may reach
SPIRIT = ("你是「地灵」，住在这座城市里的地方之灵，正被一位玩家带着在城里解谜。"
          "用中文口语回答，像老朋友聊天，最多两句、40 个字以内。你的话会被念出来，所以不要表情符号、括号和列表。")

# The three devices this project expects; the web page shows a slot for each.
DEVICE_KINDS = {
    "watcher": "SenseCAP Watcher（地灵本体）",
    "xiao": "XIAO ESP32S3 Sense（可穿戴）",
    "glasses": "Rokid 眼镜（玩家视角）",
}

lock = threading.Lock()
state = {
    "station": 0,
    "photo": None,          # latest photo filename
    "photo_at": None,
    "photo_from": None,     # device id
    "audio": None,
    "verdict": None,        # {"match": True/False/None, "text": "...", "source": "..."}
    "solved": [],
    "devices": {},          # id → {kind, first_seen, last_seen, last_event, ip, count}
    "log": [],
}
talks = {}                  # device id → last few (heard, reply) pairs, so the spirit remembers the chat


def load_stations():
    data = json.loads(STATIONS_FILE.read_text(encoding="utf-8"))
    return data["stations"], data.get("title", "Loci")


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
    for k in ("watcher", "glasses", "rokid", "xiao"):
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
    if station.get("mode") == "describe":
        return station["prompt"] + "\n只输出 JSON：{\"match\": true, \"text\": \"要念给玩家听的话\"}"
    return (
        "你是一个城市解谜游戏的裁判。玩家用胸前的小相机拍下他认为的答案。\n"
        f"这一站要找的东西：{station['target']}\n"
        f"判定标准：{station.get('criteria', '照片里清楚地拍到了上面这个东西')}\n"
        "拍得模糊、只拍到一部分但能认出来，算对；拍的是别的东西，算错。\n"
        "只输出 JSON：{\"match\": true 或 false, \"text\": \"一句话理由，对玩家说，20 字以内\"}"
    )


def parse_reply(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        return json.loads(m.group(0)) if m else {"match": None, "text": raw.strip()[:80]}
    except json.JSONDecodeError:
        return {"match": None, "text": raw.strip()[:80]}


def ask_claude_cli(path: Path, instruction: str) -> dict:
    prompt = f"用 Read 工具看这张照片：{path} 。\n{instruction}\n只输出一行 JSON，不要别的话。"
    env = dict(os.environ, HTTP_PROXY=CLAUDE_PROXY, HTTPS_PROXY=CLAUDE_PROXY)
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


def new_name(suffix: str) -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}{suffix}"


def ingest_photo(jpeg: bytes, device_id: str, sync: bool) -> dict | None:
    """Store a photo, make it current, judge it. sync=True waits and returns the verdict."""
    fname = new_name(".jpg")
    (CAPTURES / fname).write_bytes(jpeg)
    with lock:
        state.update(photo=fname, photo_at=time.time(), photo_from=device_id, verdict=None)
    log(f"收到照片 {len(jpeg) // 1024}KB 来自 {device_id}")
    if sync:
        return judge(fname, jpeg)
    threading.Thread(target=judge, args=(fname, jpeg), daemon=True).start()
    return None


def judge(fname: str, jpeg: bytes) -> dict:
    stations, _ = load_stations()
    with lock:
        idx = state["station"]
        already_solved = idx in state["solved"]
    if already_solved:  # don't re-judge (or retell the story) until someone moves on
        verdict = {"match": None, "text": "这一站已经解开了，点「下一站」继续。", "source": "skip"}
    else:
        verdict = ask_model(jpeg, stations[idx], CAPTURES / fname)
    with lock:
        if state["photo"] == fname:  # only if no newer photo arrived meanwhile
            state["verdict"] = verdict
            if verdict.get("match") is True and idx not in state["solved"]:
                state["solved"].append(idx)
    log(f"站{idx + 1} 判定 {verdict.get('match')} · {verdict.get('text')} · {verdict.get('source')}")
    return verdict


def unlocked_text(station: dict, verdict: dict) -> str:
    """What the spirit says when a station is solved."""
    story = station.get("story", "").replace("（占位）", "")
    if station.get("mode") == "describe" and verdict.get("text"):
        return verdict["text"] + "。" + story
    return story


def compact_state() -> dict:
    stations, title = load_stations()
    with lock:
        idx, v, solved = state["station"], state["verdict"], list(state["solved"])
        devices = {k: dict(d) for k, d in state["devices"].items()}
    st = stations[idx]
    solved_now = idx in solved
    return {
        "title": title,
        "station": idx + 1,
        "total": len(stations),
        "solved": [i + 1 for i in solved],
        "riddle": st.get("riddle", ""),
        "verdict": v,
        "unlocked": unlocked_text(st, v or {}) if solved_now else None,
        "devices": devices,
    }


# ── speech for the Watcher ───────────────────────────────────────────────

def tts_wav(text: str) -> bytes:
    """macOS say → 16 kHz mono 16-bit WAV with a plain 44-byte header
    (the Watcher reads sample rate from a fixed header; a JUNK chunk would confuse it)."""
    key = hashlib.sha1(f"{TTS_VOICE}|{text}".encode()).hexdigest()[:16]
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
        env = dict(os.environ, HTTP_PROXY=CLAUDE_PROXY, HTTPS_PROXY=CLAUDE_PROXY)
        r = subprocess.run([CLAUDE_BIN, "-p", "--setting-sources", "", "--model", MODEL, "--output-format", "text", prompt],
                           capture_output=True, text=True, timeout=60, env=env, cwd=str(HERE))
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout)[-200:])
        return r.stdout.strip()
    return ""


def spirit_reply(heard: str, device_id: str) -> str:
    """The spirit's answer: knows the current station, gives hints, never the answer."""
    stations, _ = load_stations()
    spirit = json.loads(STATIONS_FILE.read_text(encoding="utf-8")).get("spirit") or SPIRIT  # a route may set its own voice
    with lock:
        idx, solved = state["station"], state["station"] in state["solved"]
        history = list(talks.get(device_id, []))
    st = stations[idx]
    where = (f"玩家已经解开了第{idx + 1}站。" if solved else
             f"玩家在第{idx + 1}站，谜面是：{st.get('riddle', '')} 要找的是：{st.get('target', '')}。可以给提示，别直接说出答案。")
    past = "".join(f"玩家：{h}\n地灵：{r}\n" for h, r in history)
    reply = ask_text(f"{spirit}\n{where}\n{past}玩家：{heard}\n地灵：")
    reply = " ".join(reply.split()).removeprefix("地灵：").strip("「」\"' ")[:80] or "嗯，我在听。"
    with lock:
        talks[device_id] = (history + [(heard, reply)])[-3:]
    return reply


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
            reply = spirit_reply(heard, device_id) if heard else "我没听清，再说一遍？"
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
    v = ingest_photo(jpeg, device_id, sync=True) or {}
    stations, _ = load_stations()
    with lock:
        idx = state["station"]
    speech = ""
    if v.get("match") is True:
        speech = unlocked_text(stations[idx], v)
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
const KINDS={watcher:"SenseCAP Watcher（地灵本体）",xiao:"XIAO ESP32S3 Sense（可穿戴）",glasses:"Rokid 眼镜（玩家视角）"};
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
      html+=`<div class="dev ${s<60?"live":s<600?"idle":""}"><b>${label}</b>${id} · ${ago(d.last_seen)}<br>${d.last_event} · 共 ${d.count} 次</div>`;}
  }
  for(const [kind,list] of Object.entries(byKind)){if(KINDS[kind])continue;
    for(const [id,d] of list)html+=`<div class="dev live"><b>${id}</b>${ago(d.last_seen)} · ${d.last_event}</div>`;}
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
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks = []
            while True:
                size_line = self.rfile.readline().strip()
                if not size_line:
                    continue
                size = int(size_line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()  # trailing CRLF after the last chunk
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()      # CRLF after each chunk
            return b"".join(chunks)
        n = int(self.headers.get("Content-Length", "0") or 0)
        return self.rfile.read(n) if n else b""

    def ip(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def device_id(self, default_kind: str) -> tuple[str, str]:
        dev = self.headers.get("X-Device") or ""
        if not dev:
            q = re.search(r"(?:^|&)device=([\w.-]+)", self.path.partition("?")[2])
            dev = q.group(1) if q else ""
        if not dev:
            dev = f"{default_kind}-{self.ip()}"
        return dev, device_kind(dev, default_kind)

    def watcher_id(self) -> str:
        eui = self.headers.get("API-OBITER-DEVICE-EUI")
        if eui:
            return f"watcher-{eui[-6:]}"
        auth = self.headers.get("Authorization", "")
        return "watcher-" + (hashlib.sha1(auth.encode()).hexdigest()[:6] if auth else self.ip())

    def from_tunnel_blocked(self) -> bool:
        """A request relayed by Cloudflare from the internet may only reach PUBLIC_PATHS."""
        if self.headers.get("Cf-Connecting-Ip") and self.path.partition("?")[0] not in PUBLIC_PATHS:
            self.send(404, {"error": "not found"})
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
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.from_tunnel_blocked():
            return
        path = self.path.partition("?")[0]
        if path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if path == "/state":
            stations, title = load_stations()
            with lock:
                snap = json.loads(json.dumps(state))
            snap.update(stations=stations, title=title)
            return self.send(200, snap)
        if path == "/api/v1/state":
            if "device=" in self.path or self.headers.get("X-Device"):
                dev, kind = self.device_id("glasses")
                seen(dev, kind, "查看进度", self.ip())
            return self.send(200, compact_state())
        m = re.match(r"^/photo/([\w.-]+\.jpg)$", path)
        if m and (CAPTURES / m.group(1)).exists():
            return self.send(200, (CAPTURES / m.group(1)).read_bytes(), "image/jpeg")
        log(f"陌生 GET {self.path}")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        path, _, query = self.path.partition("?")
        body = self.read_body()
        if self.from_tunnel_blocked():
            return

        if path == "/api/v1/talk":                  # any client: text in, the spirit's reply out
            try:
                text = str(json.loads(body or b"{}").get("text", "")).strip()
            except Exception:
                return self.send(400, {"error": "bad json"})
            dev, kind = self.device_id("glasses")
            seen(dev, kind, "说话", self.ip())
            try:
                reply = spirit_reply(text, dev) if text else "我没听清，再说一遍？"
            except Exception as e:
                log(f"地灵回话失败 {type(e).__name__}: {e}")
                reply = "我刚才走神了，再说一遍好吗？"
            log(f"地灵对话（{dev}）听到={text[:30]!r} 回={reply[:30]!r}")
            return self.send(200, {"heard": text, "reply": reply, **compact_state()})

        if path == "/api/v1/next":                  # any client: move on, but only from a solved station
            stations, _ = load_stations()
            with lock:
                if state["station"] in state["solved"] and state["station"] < len(stations) - 1:
                    state["station"] += 1
                    state["verdict"] = None
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
            ingest_photo(body, dev, sync=False)
            return self.send(200, {"ok": True})

        if path == "/api/v1/photo":                 # any client: JPEG → verdict in the response
            jpeg = body
            if body[:1] == b"{":
                try:
                    jpeg = base64.b64decode(json.loads(body).get("img", ""))
                except Exception:
                    return self.send(400, {"error": "bad json"})
            if not jpeg.startswith(b"\xff\xd8"):
                return self.send(400, {"error": "not a jpeg"})
            dev, kind = self.device_id("glasses")
            seen(dev, kind, "拍照", self.ip())
            v = ingest_photo(jpeg, dev, sync=True)
            return self.send(200, {"verdict": v, **compact_state()})

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
                    ingest_photo(base64.b64decode(img), dev, sync=False)
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
            log(f"人工判定 {ok}")
            return self.send(200, {"ok": True})
        if path == "/station":
            stations, _ = load_stations()
            d = -1 if "d=-1" in query else 1
            with lock:
                state["station"] = max(0, min(len(stations) - 1, state["station"] + d))
                state["verdict"] = None
            return self.send(200, {"ok": True})
        if path == "/reset":
            with lock:
                state.update(station=0, verdict=None, solved=[], photo=None, photo_from=None)
            log("重来")
            return self.send(200, {"ok": True})

        log(f"陌生 POST {self.path} {len(body)}B 头={body[:120]!r}")
        self.send(404, {"code": 404, "error": "not found"})


def serve(host="0.0.0.0", port=PORT):
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    stations, title = load_stations()
    print(f"Loci relay · {title} · {len(stations)} 站 · 判定 {judge_backend()} · 模型 {MODEL}")
    print(f"浏览器打开 http://localhost:{PORT}/ ；设备填这台电脑的 IP:{PORT}（ipconfig getifaddr en0）")
    serve().serve_forever()
