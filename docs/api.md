# Relay API

The relay (`src/relay/relay.py`) is the only server. Every device talks to it over plain HTTP on the local network (default port `8787`). Responses are JSON unless noted. On a network you don't trust (hotel, café), start it with `RELAY_HOST=127.0.0.1`: then only this computer and the tunnel can reach it, and LAN devices (Watcher, XIAO) can't.

Phones away from home and Rokid Glasses can't reach the local network (AIUI has no LAN access and requires HTTPS), so for them the relay is published through a tunnel, e.g. `cloudflared tunnel --url http://localhost:8787`. Requests from outside may only use the player endpoints below (`/play` and `/api/v1/*`); the operator page, controls and device-specific endpoints answer 404. If `LOCI_KEY` is set, every one of them except `/play` also needs that key, as `?key=` or an `X-Loci-Key` header (wrong or missing → 401). *Outside* means: through Cloudflare (the request carries `Cf-Connecting-Ip`), from a page on another site open in a browser on the same computer (its `Origin` isn't the relay), or addressed to a domain name rather than `localhost` or an IP address (so a page can't reach the relay through DNS rebinding).

A device identifies itself with an `X-Device` header (or `?device=` in the query): letters, digits, `.`, `_`, `-`, at most 48 characters; anything else is ignored. Names starting with `watcher`, `xiao`, `glasses` / `rokid`, `phone` are shown in the matching slot on the web page.

## Routes and modes

A route is one JSON file. The relay plays one route at a time (`STATIONS`, default `stations.json`) and lets players switch between it and the files in `ROUTES_DIR` (default `src/relay/routes/`, if it exists). A route's code is its file name without `.json`.

- **quest**: a list of `stations`, each with a riddle, a target the model judges and a story unlocked on a correct find.
- **wander**: no stations. Every photo gets a short look at what is in it ("the spirit notices something"), in the route's `city`. Set `"mode": "wander"` or leave `stations` empty.

```json
{"title": "吉隆坡漫游", "city": "吉隆坡", "mode": "wander", "intro": "随便走，看到什么让我看一眼。"}
```

## Moments and the nightly tidy-up

Every look, judgement and exchange is a *moment*, appended to `LOCI_DATA/runs/<YYYYMMDD>-<route>/moments.jsonl` with time, device, place (`lat`/`lon` when the client sends them) and what the spirit said. With every look or judgement the model also says whether there are recognisable people in the photo (`people`).

At `PRUNE_AT` (default `23:30`) the relay writes that day's digest to `LOCI_DATA/digests/<YYYYMMDD>.json`, then deletes every photo that isn't worth keeping. A photo survives only if it was kept or solved **and** the model said there are no people in it; a photo with people — or one the model said nothing about — is deleted even if kept, because strangers' faces are only for judging on the spot. The moment and what the spirit said stay. Days the relay missed (the Mac was asleep, photos after `PRUNE_AT`, a pass that failed) are caught up within a minute of the relay running again. A day that only has photos (the relay stopped before recording any) is tidied too, as long as it is not older than the first day of the journal; earlier photos are left alone.

## For any client (phone, glasses, scripts)

### `GET /api/v1/state`

Current progress, compact. Polling with `?device=<name>` also registers the client on the web page.

```json
{
  "title": "Loci 地灵 · 演示路线（占位）",
  "route": "stations",
  "mode": "quest",
  "city": "",
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

Send a photo, get the answer back in the same response (takes as long as the model does — seconds).
Body: a raw JPEG, or JSON `{"img": "<base64 JPEG>", "lat": …, "lon": …}`; `?lat=&lon=` in the query works too. A photo sent late (the phone kept it while the relay was unreachable) adds `?at=<ms since 1970>` (recorded as `taken_at`) and the `route` and `station` it was taken on. If the player has moved on since, it is only described, in that route's city, and never solves a station. The phone sends `at` with every photo, not only late ones, so the same device sending the same `at` again (the first answer got lost) gets the first answer back, not a second judgement. Bodies over 25 MB get 413.
Reply: `{"verdict": {...}, "say": "…", "voice": "/api/v1/voice/<id>.wav", "moment": "<id>", ...same fields as /api/v1/state}`. In a quest `say` is the story or a nudge; in wander it is what the spirit noticed.
Once a station is solved, further photos are not judged again (`verdict.source` is `skip`) until someone moves on.

### `POST /api/v1/talk`

Body: `{"text": "地灵，我该去哪？"}`, or a recording (`Content-Type: audio/*`, e.g. what a phone's `MediaRecorder` makes; the relay converts it with `ffmpeg` and transcribes it with `ASR_URL`).
Reply: `{"heard": "...", "reply": "...", "say": "...", "voice": "...", "kept": null, ...same fields as /api/v1/state}`. The spirit knows the route, the city and the current station, gives hints but not the answer, and remembers the last three exchanges per device. Saying 收起来 / 记下来 / 留着 / keep keeps the last photo instead (`kept` is its moment id). A recording with almost no voice gets "say it again".

### `POST /api/v1/keep`

Keep this device's latest look or judgement today (across routes), or a given one with `{"moment": "<id>"}`. Reply: `{"kept": "<id>" | null, "say": "…", "voice": "…"}`; if the photo has people, `say` tells the player only the words will be kept.

### `POST /api/v1/next`

Go to the next station, only if the current one is solved. Reply: `{"moved": true|false, ...same fields as /api/v1/state}`.

### `GET /api/v1/routes` · `POST /api/v1/route`

List routes: `{"current": "<code>", "routes": [{"route", "title", "city", "mode"}]}`. Switch with `{"route": "<code>"}`: progress starts over and the reply carries the new route's first riddle in `say` / `voice`.

### `GET /api/v1/digest?day=YYYYMMDD`

The day in short (default today): `{"day", "routes", "counts": {"look", "judge", "talk"}, "kept": [moments]}` — kept moments plus correct finds.

### `GET /api/v1/voice/<id>.wav` · `GET /api/v1/photo-file/<name>.jpg`

The spirit's voice for a reply (clients that can't speak Chinese themselves play this) and a stored photo (for the digest). Only photos the model said have no people in them are served; the digest leaves the others' `photo` empty.

### `POST /api/v1/log`

`{"msg": "…"}` → one line in the relay log. For clients without a console you can read (glasses).

## Phone (`src/phone/index.html`)

`GET /play` is a single page that turns any phone browser into a vessel: 让地灵看 (take a photo, sent with the phone's location), 按住说话 (hold to record), a text box (type, or use the keyboard's dictation), 收起来, 下一站, 今天的拾遗 (the digest) and a route picker. Open it as `/play#key=<LOCI_KEY>`; the key stays in the link, so "Add to Home Screen" gives an icon that works. Over a tunnel it needs nothing installed.

When the relay can't be reached (no signal, or the computer running it is asleep — the tunnel answers 502/530), a photo is kept on the phone (IndexedDB) with the time, place, route and station it was taken on, and sent when the relay answers again. Those photos appear under 刚补传的, each with its own 收起来. Nothing has checked them for people, so they wait only until the end of the day they were taken (then they are dropped), and 清掉 drops them at once.

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
| `GET /` | The operator page: device slots, current station, latest photo, verdict, unlocked story (read aloud by the browser). |
| `GET /state` | Full internal state (used by the page). |
| `POST /judge?ok=1` / `?ok=0` | Judge by hand. Beats a model judgement still running; `ok=0` also undoes a solve. |
| `POST /station?d=1` / `?d=-1` | Next / previous station. |
| `POST /reset` | Back to station 1. |

Unknown paths are answered with 404 and logged (`陌生 POST …`) so new device behaviour shows up in the log.

## Tests

```sh
cd src/relay && python3 -m unittest -v test_relay
```

No hardware and no model calls: judging runs in manual mode, and the model and speech are stubbed where needed.
