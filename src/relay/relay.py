#!/usr/bin/env python3
"""Loci 中继：板子把照片/录音 POST 过来，这里判定、出故事，网页上显示和朗读。

只用标准库。运行：
    python3 relay.py                      # 默认 0.0.0.0:8787，读同目录 stations.json
判定按顺序选：
    1. 设了 ANTHROPIC_API_KEY → 直接调 API（最快）
    2. 本机有 claude 命令 → 调 `claude -p` 看图（走你的订阅；10-20 实测约 8 秒一张）
    3. 都没有，或 JUDGE=manual → 网页上「判对/判错」人工判
可选环境变量：RELAY_PORT、MODEL（默认 claude-sonnet-5-5）、STATIONS（站点文件路径）、JUDGE（manual 强制人工）、
CLAUDE_PROXY（claude -p 用的代理，默认 http://127.0.0.1:7890）。
claude -p 报 401 时，先在同一个终端里跑一次 `claude` 确认登录态（或把 CLAUDE_CONFIG_DIR 指向能用的那份）。
"""

import base64
import hashlib
import io
import tempfile
import wave
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAPTURES = HERE / "captures"
CAPTURES.mkdir(exist_ok=True)
PORT = int(os.environ.get("RELAY_PORT", "8787"))
MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
STATIONS_FILE = Path(os.environ.get("STATIONS", HERE / "stations.json"))
JUDGE = os.environ.get("JUDGE", "auto")
CLAUDE_BIN = shutil.which("claude")
CLAUDE_PROXY = os.environ.get("CLAUDE_PROXY", "http://127.0.0.1:7890")
TTS_VOICE = os.environ.get("TTS_VOICE", "Tingting")
WATCHER_TOKEN = os.environ.get("WATCHER_TOKEN", "")   # App「Device AI Service」里填的 Token；空 = 不校验
WATCHER_SPEAK_MISS = os.environ.get("WATCHER_SPEAK_MISS", "0") == "1"  # 判错也让 Watcher 出声（会触发它的报警动作）
TTS_DIR = CAPTURES / "tts"
TTS_DIR.mkdir(exist_ok=True)

lock = threading.Lock()
state = {
    "station": 0,
    "photo": None,          # 最新照片文件名
    "photo_at": None,
    "audio": None,
    "verdict": None,        # {"match": True/False/None, "text": "...", "source": "model|manual|none"}
    "solved": [],
    "log": [],
}


def load_stations():
    data = json.loads(STATIONS_FILE.read_text(encoding="utf-8"))
    return data["stations"], data.get("title", "senses")


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with lock:
        state["log"] = (state["log"] + [line])[-30:]


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
        instruction = station["prompt"] + "\n只输出 JSON：{\"match\": true, \"text\": \"要念给玩家听的话\"}"
    else:
        instruction = (
            "你是一个城市解谜游戏的裁判。玩家用胸前的小相机拍下他认为的答案。\n"
            f"这一站要找的东西：{station['target']}\n"
            f"判定标准：{station.get('criteria', '照片里清楚地拍到了上面这个东西')}\n"
            "拍得模糊、只拍到一部分但能认出来，算对；拍的是别的东西，算错。\n"
            "只输出 JSON：{\"match\": true 或 false, \"text\": \"一句话理由，对玩家说，20 字以内\"}"
        )
    return instruction


def parse_reply(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else {"match": None, "text": raw.strip()[:80]}


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
    """返回 {"match": bool|None, "text": str, "source": ...}；判不了时 match=None，交给人工。"""
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
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(jpeg).decode()}},
                {"type": "text", "text": instruction},
            ],
        }],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        raw = "".join(b.get("text", "") for b in resp.get("content", []))
        out = parse_reply(raw)
        out["source"] = f"model {time.time() - t0:.1f}s"
        return out
    except Exception as e:  # 网络、代理、额度——都退回人工
        return {"match": None, "text": f"模型调用失败（{type(e).__name__}），请人工判。", "source": "error"}


def tts_wav(text: str) -> bytes:
    """macOS say → 16kHz 单声道 16bit WAV，标准 44 字节头（Watcher 按固定头读采样率，带 JUNK 段会读错）。"""
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


def watcher_vision(req: dict) -> dict:
    """SenseCAP Watcher 官方固件的图像分析接口（固件源码 tf_module_img_analyzer.c）：
    请求 {img: base64 jpeg, prompt, audio_txt, type}；回 {code:200, data:{state, type, audio: base64 wav}}。
    state=1 时 Watcher 触发报警动作并播放 audio。"""
    jpeg = base64.b64decode(req.get("img", ""))
    fname = time.strftime("%Y%m%d-%H%M%S") + f"-w{int(time.time() * 1000) % 1000:03d}.jpg"
    (CAPTURES / fname).write_bytes(jpeg)
    with lock:
        state.update(photo=fname, photo_at=time.time(), verdict=None)
    log(f"Watcher 图 {len(jpeg) // 1024}KB · prompt={str(req.get('prompt'))[:40]!r} audio_txt={str(req.get('audio_txt'))[:30]!r} type={req.get('type')}")
    judge_async(fname, jpeg)  # 同步判：Watcher 自建服务超时 2 分钟，够用
    stations, _ = load_stations()
    with lock:
        v, idx = state["verdict"] or {}, state["station"]
    st = stations[idx]
    match = v.get("match") is True
    speech = ""
    if match:
        speech = (v.get("text", "") + "。" + st["story"]) if st.get("mode") == "describe" else st["story"]
    elif WATCHER_SPEAK_MISS:
        speech = "还没找到哦，再看看。"
    data = {"state": 1 if speech else 0, "type": req.get("type", 0)}
    if speech:
        try:
            data["audio"] = base64.b64encode(tts_wav(speech.replace("（占位）", ""))).decode()
        except Exception as e:
            log(f"TTS 失败 {type(e).__name__}，只回 state")
    log(f"回 Watcher state={data['state']} audio={len(data.get('audio', '')) * 3 // 4 // 1024}KB")
    return {"code": 200, "data": data}


def judge_async(fname: str, jpeg: bytes):
    stations, _ = load_stations()
    with lock:
        idx = state["station"]
    station = stations[idx]
    verdict = ask_model(jpeg, station, CAPTURES / fname)
    with lock:
        if state["photo"] == fname:  # 期间没有更新的照片才写回
            state["verdict"] = verdict
            if verdict.get("match") and idx not in state["solved"]:
                state["solved"].append(idx)
    log(f"站{idx + 1} 判定 {verdict.get('match')} · {verdict.get('text')} · {verdict.get('source')}")


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Loci</title>
<style>
body{font-family:-apple-system,"PingFang SC",sans-serif;margin:0;background:#111;color:#eee}
main{max-width:720px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0 0 8px}.k{color:#999;font-size:13px}
.card{background:#1d1d1d;border-radius:12px;padding:14px;margin:12px 0}
img{width:100%;border-radius:8px;background:#000;min-height:120px}
.ok{color:#5ad17a}.no{color:#ff7a6b}.wait{color:#f2c94c}
button{font-size:16px;padding:10px 14px;margin:4px 4px 0 0;border-radius:8px;border:0;background:#333;color:#eee}
button.g{background:#2f6f43}button.r{background:#7a2f2a}
.story{font-size:18px;line-height:1.6;white-space:pre-wrap}
pre{font-size:12px;color:#888;white-space:pre-wrap}
</style></head><body><main>
<h1 id="title">Loci</h1><div class="k" id="prog"></div>
<div class="card"><div class="k">这一站</div><div class="story" id="riddle"></div></div>
<div class="card"><img id="photo" alt="还没有照片"><div id="verdict" class="wait">按一下板子上的 BOOT 键拍照</div></div>
<div class="card" id="storybox" hidden><div class="k">解锁</div><div class="story" id="story"></div></div>
<div>
<button class="g" onclick="post('/judge?ok=1')">判对</button><button class="r" onclick="post('/judge?ok=0')">判错</button>
<button onclick="post('/station?d=-1')">上一站</button><button onclick="post('/station?d=1')">下一站</button>
<button onclick="speakNow()">朗读</button><button onclick="post('/reset')">重来</button>
</div>
<pre id="log"></pre>
</main><script>
const $=id=>document.getElementById(id);
let last="",spoken="",cur=null;
async function post(u){await fetch(u,{method:"POST"});tick();}
function speak(t,lang){if(!t)return;speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(t);u.lang=lang||"zh-CN";speechSynthesis.speak(u);}
function storyText(st,v){return st.mode==="describe"&&v&&v.text?v.text+"\\n\\n"+st.story:st.story;}
function speakNow(){if(cur)speak(cur.solvedNow?storyText(cur.st,cur.v):cur.st.riddle,cur.st.lang);}
async function tick(){const s=await (await fetch("/state")).json();const st=s.stations[s.station];const v=s.verdict;
cur={st:st,v:v,solvedNow:s.solved.includes(s.station)};
$("title").textContent=s.title;$("prog").textContent=`第 ${s.station+1} / ${s.stations.length} 站 · 已解 ${s.solved.length}`;
$("riddle").textContent=st.riddle;
if(s.photo&&s.photo!==last){$("photo").src="/photo/"+s.photo;last=s.photo;}
const el=$("verdict");
if(!v){el.className="wait";el.textContent=s.photo?"判定中…":"按一下板子上的 BOOT 键拍照";}
else{el.className=v.match===true?"ok":v.match===false?"no":"wait";
el.textContent=(v.match===true?"✓ ":v.match===false?"✗ ":"? ")+v.text+"  ["+v.source+"]";}
$("storybox").hidden=!cur.solvedNow;$("story").textContent=cur.solvedNow?storyText(st,v):"";
const key=s.station+":"+(cur.solvedNow?"s":"r")+":"+(v?v.text:"");
if(cur.solvedNow&&spoken!==key){spoken=key;speak(storyText(st,v),st.lang);}
$("log").textContent=s.log.slice().reverse().join("\\n");}
setInterval(tick,1000);tick();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
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
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        if self.path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/state":
            stations, title = load_stations()
            with lock:
                snap = dict(state)
            snap.update(stations=stations, title=title)
            return self.send(200, snap)
        m = re.match(r"^/photo/([\w.-]+\.jpg)$", self.path)
        if m and (CAPTURES / m.group(1)).exists():
            return self.send(200, (CAPTURES / m.group(1)).read_bytes(), "image/jpeg")
        log(f"陌生 GET {self.path}")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path == "/upload":
            jpeg = self.read_body()
            if not jpeg.startswith(b"\xff\xd8"):
                return self.send(400, {"error": "not a jpeg"})
            fname = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}.jpg"
            (CAPTURES / fname).write_bytes(jpeg)
            with lock:
                state.update(photo=fname, photo_at=time.time(), verdict=None)
            log(f"收到照片 {len(jpeg) // 1024}KB 来自 {self.headers.get('X-Device', '?')}")
            threading.Thread(target=judge_async, args=(fname, jpeg), daemon=True).start()
            return self.send(200, {"ok": True, "file": fname})
        if path == "/v1/watcher/vision":
            if WATCHER_TOKEN and self.headers.get("Authorization", "") != WATCHER_TOKEN:
                log("Watcher 请求 Token 不对，拒绝")
                return self.send(401, {"code": 401})
            try:
                req = json.loads(self.read_body() or b"{}")
                return self.send(200, watcher_vision(req))
            except Exception as e:
                log(f"Watcher 请求处理失败 {type(e).__name__}: {e}")
                return self.send(200, {"code": 500, "data": {"state": 0}})
        if path == "/audio":
            wav = self.read_body()
            fname = time.strftime("%Y%m%d-%H%M%S") + ".wav"
            (CAPTURES / fname).write_bytes(wav)
            with lock:
                state["audio"] = fname
            log(f"收到录音 {len(wav) // 1024}KB（还没接语音识别）")
            return self.send(200, {"ok": True, "file": fname})
        if path == "/judge":  # 人工判（模型不通或演示兜底）
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
                state.update(station=0, verdict=None, solved=[], photo=None)
            log("重来")
            return self.send(200, {"ok": True})
        body = self.read_body()
        log(f"陌生 POST {self.path} {len(body)}B 头={body[:120]!r}")  # 侦察：看 Watcher 还会调哪些接口
        self.send(404, {"code": 404, "error": "not found"})


if __name__ == "__main__":
    stations, title = load_stations()
    print(f"Loci relay · {title} · {len(stations)} 站 · 判定 {judge_backend()} · 模型 {MODEL}")
    print(f"浏览器打开 http://localhost:{PORT}/ ；板子 secrets.h 里 RELAY_BASE 填这台电脑的 IP:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
