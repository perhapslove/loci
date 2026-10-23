# Loci · 地灵

**A pocket spirit of place that sees where you are and tells you the city's stories.**
Gamified cultural exploration of cities, built from cheap AI hardware and open-source software.

> *Genius loci* — the protective spirit of a place. In Chinese, 人杰地灵: great people come from places with spirit.
> Every city has one. Loci is how you meet it.

**Website:** <https://loci.goshimmer.ai> · **Status:** early, playable in a phone browser; started at the Seeed × ShanHaiWoo AI Hardware Hackathon (Shenzhen, Oct 21–23, 2026). Expect rough edges.

---

## How it works

```
  player points a device at a clue (a carved stone, a sign, a gate)
        │
        ▼
  [ vessel ]  anything that can see and speak   a phone browser · Rokid glasses · SenseCAP Watcher · XIAO ESP32S3 Sense
        │  photo (+ time and place)
        ▼
  [ relay ]   judges "did you find it?"          src/relay  (Python, stdlib only)
        │     with a vision model, then
        │     writes the story as speech
        ▼
  the spirit speaks the story and the next clue; every look and exchange becomes a moment
```

The answer only exists on site. An AI can't search it — the spirit has to *see* that you are really there.

Two ways to play: **quest** (fixed stations: riddle → find → story → next) and **wander** (no stations: show the spirit anything and it tells you what it notices; keep the moments you like, and the day is summed up at night).

## What's in this repo

| Path | What |
|---|---|
| `src/relay/` | The brain. Receives photos, asks a vision model whether the player found the target (or what is worth noticing), returns words and speech. Includes the endpoint the SenseCAP Watcher's stock firmware calls (`/v1/watcher/vision`) and an operator page for the route. |
| `src/relay/moments.py` | Every look, judgement and exchange as a moment; the nightly tidy-up (digest, delete photos nobody kept). |
| `src/relay/stations.json` | A demo route: stations, riddles, targets, stories. Edit this, not the code. |
| `src/phone/` | The phone client, served by the relay at `/play`: take a photo, hold to talk or type, keep, today's digest. Works offline for a day and sends photos when the relay is back. |
| `src/glasses/` | A Rokid Glasses agent (AIUI). |
| `src/relay/test_relay.py` | Tests for the relay — no hardware, no model calls: `python3 -m unittest -v test_relay` |
| `src/firmware/xiao-senses/` | Firmware for XIAO ESP32S3 Sense: press BOOT to snap and upload; hold to record audio. |
| `docs/hardware.md` | What each supported device can and can't do, with sources. |
| `docs/getting-started.md` | Step-by-step: run the relay, play on a phone, connect a Watcher, flash a XIAO. |
| `docs/api.md` | Every endpoint the relay serves, for devices and other clients. |

## Quick start

```sh
cd src/relay
python3 relay.py            # open http://localhost:8787/
```

Then follow [docs/getting-started.md](docs/getting-started.md) to point a device at it — the quickest is your phone, through a tunnel, at `/play`.

## Supported hardware

- **Any phone** — open `/play` in the browser (Safari on iPhone). Camera, microphone, location; no app to install.
- **Rokid AI glasses** — an AIUI agent in `src/glasses/`; runs on the glasses through the global Hi Rokid app. Taking photos on real glasses is not verified yet.
- **Seeed SenseCAP Watcher** — round screen with eyes, camera, mic, speaker, battery. Works with stock firmware via its "Device AI Service" setting.
- **Seeed XIAO ESP32S3 Sense** — eyes and ears only (no screen, no speaker); good for wearables.

## Privacy

- A photo is taken only when the player presses something; nothing records continuously.
- Strangers' faces are for judging on the spot only. The model says whether a photo shows people; such photos are never served to any client and are deleted every night, even if kept — only the words stay.
- Photos nobody kept are deleted every night. Photos waiting on a phone (relay unreachable) are dropped at the end of that day.
- Requests from outside (a tunnel, another website) reach only the player API and need the key set in `LOCI_KEY`.

---

## 中文简介

**Loci（地灵）**：一只会看、会说的口袋「地灵」，陪你在城市里解谜，讲这个地方的故事。

玩家用手机（或眼镜、Watcher）对准线索（石刻、牌匾、城门）拍下来，交给 AI 判断「你是不是真的找到了」。找对了，地灵就讲一段故事，再给出下一站的线索。答案只在现场，AI 搜不到，它得看见你真的在那里。

也可以不设站随便走（漫游）：拍到什么，地灵就说说它看见了什么；喜欢的收起来，晚上整理成当天的拾遗。拍到别人的脸只用于当场判断，当晚就删。

起于 2026 年 10 月深圳 Seeed × 山海坞 AI 硬件创客松，现在能用手机浏览器玩，还很早期。

## License

[MIT](LICENSE)
