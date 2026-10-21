# Relay API

The relay (`src/relay/relay.py`) is the only server. Every device talks to it over plain HTTP on the local network (default port `8787`). Responses are JSON unless noted.

Rokid Glasses can't reach the local network (AIUI has no LAN access and requires HTTPS), so for them the relay is published through a tunnel, e.g. `cloudflared tunnel --url http://localhost:8787`. Requests that arrive through Cloudflare (they carry `Cf-Connecting-Ip`) may only use the four `/api/v1/*` endpoints below; the web page, photos and operator controls answer 404.

A device identifies itself with an `X-Device` header (or `?device=` in the query). Names starting with `watcher`, `xiao`, `glasses` / `rokid` are shown in the matching slot on the web page.

## For any client (phone, glasses, scripts)

### `GET /api/v1/state`

Current progress, compact. Polling with `?device=<name>` also registers the client on the web page.

```json
{
  "title": "Loci 地灵 · 演示路线（占位）",
  "station": 1,
  "total": 3,
  "solved": [],
  "riddle": "第一站：……",
  "verdict": {"match": true, "text": "拍到了牌子", "source": "claude-cli 6.5s"},
  "unlocked": "the story, once this station is solved — otherwise null",
  "devices": {"xiao-senses-1": {"kind": "xiao", "last_seen": 1792600000.1, "last_event": "拍照", "count": 3}}
}
```

`verdict.match` is `true`, `false`, or `null` (no model available — a person judges on the web page).

### `POST /api/v1/photo`

Send a photo, get the verdict back in the same response (takes as long as judging does — seconds).
Body: a raw JPEG, or JSON `{"img": "<base64 JPEG>"}`. Reply: `{"verdict": {...}, ...same fields as /api/v1/state}`.
Once a station is solved, further photos are not judged again (`verdict.source` is `skip`) until someone moves on.

### `POST /api/v1/talk`

Body: `{"text": "地灵，我该去哪？"}`. Reply: `{"heard": "...", "reply": "...", ...same fields as /api/v1/state}`. The spirit knows the current station, gives hints but not the answer, and remembers the last three exchanges per device.

### `POST /api/v1/next`

Go to the next station, only if the current one is solved. Reply: `{"moved": true|false, ...same fields as /api/v1/state}`.

## SenseCAP Watcher (stock firmware)

Set the app's **Device AI Service** URL to `http://<relay address>:8787`. The firmware then calls:

| Endpoint | What the relay does |
|---|---|
| `POST /v1/watcher/vision` | `{img, prompt, audio_txt, type}` → judges the image. Replies `{"code": 200, "data": {"state": 1, "type": …, "audio": "<base64 WAV>"}}` on a correct find (the Watcher speaks the story), `state: 0` otherwise. |
| `POST /v1/notification/event` | Task alarm push. If `events.img` is present, it becomes the current photo and is judged. |
| `POST /v2/watcher/talk/audio_stream` | Push-to-talk (hold the wheel). Body: raw 16 kHz mono 16-bit PCM, chunked. If it holds at least 0.3 s of voice, the relay transcribes it (`ASR_URL`, any OpenAI-compatible `/v1/audio/transcriptions`), lets the spirit answer, and replies JSON `{"code": 200, "data": {"mode": 0, "duration": ms, "stt_result": …, "screen_text": …}}`, then `---sensecraftboundary---`, a newline and a WAV the Watcher plays. Less voice than that gets "say it again" — ASR models invent whole sentences from near-silence. |
| `POST /v2/watcher/talk/view_task_detail` | Not handled (`{"code": 501}`). |

If `WATCHER_TOKEN` is set, `/v1/watcher/vision` requires the same value in the `Authorization` header.

## XIAO ESP32S3 Sense (`src/firmware/xiao-senses`)

| Endpoint | Body |
|---|---|
| `POST /upload` | raw JPEG (`Content-Type: image/jpeg`, `X-Device: <id>`). Judged in the background; watch the web page. |
| `POST /audio` | raw WAV, 16 kHz mono. Stored only. |

## Web page and controls

| Endpoint | |
|---|---|
| `GET /` | The route page: device slots, current station, latest photo, verdict, unlocked story (read aloud by the browser). |
| `GET /state` | Full internal state (used by the page). |
| `POST /judge?ok=1` / `?ok=0` | Judge by hand. |
| `POST /station?d=1` / `?d=-1` | Next / previous station. |
| `POST /reset` | Back to station 1. |

Unknown paths are answered with 404 and logged (`陌生 POST …`) so new device behaviour shows up in the log.

## Tests

```sh
cd src/relay && python3 -m unittest -v test_relay
```

No hardware and no model calls: judging runs in manual mode, and the model and speech are stubbed where needed.
