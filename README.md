# Loci · 地灵

**A pocket spirit of place that sees where you are and tells you the city's stories.**
Gamified cultural exploration of cities, built from cheap AI hardware and open-source software.

> *Genius loci* — the protective spirit of a place. In Chinese, 人杰地灵: great people come from places with spirit.
> Every city has one. Loci is how you meet it.

**Status:** prototype, started at the Seeed × ShanHaiWoo AI Hardware Hackathon (Shenzhen, Oct 21–23, 2026). Expect rough edges.

---

## How it works

```
  player points the creature at a clue (a carved stone, a sign, a gate)
        │
        ▼
  [ device ]  camera + mic + speaker          SenseCAP Watcher  (or XIAO ESP32S3 Sense)
        │  photo
        ▼
  [ relay ]   judges "did you find it?"        src/relay  (Python, stdlib only)
        │     with a vision model, then
        │     writes the story as speech
        ▼
  device speaks the story and the next clue;  a web page shows progress
```

The answer only exists on site. An AI can't search it — the creature has to *see* that you are really there.

## What's in this repo

| Path | What |
|---|---|
| `src/relay/` | The brain. Receives photos, asks a vision model whether the player found the target, returns speech. Includes the endpoint the SenseCAP Watcher's stock firmware calls (`/v1/watcher/vision`) and a web page for the route. |
| `src/relay/stations.json` | A route: stations, riddles, targets, stories. Edit this, not the code. |
| `src/firmware/xiao-senses/` | Firmware for XIAO ESP32S3 Sense: press BOOT to snap and upload; hold to record audio. |
| `docs/hardware.md` | What each supported device can and can't do, with sources. |
| `docs/getting-started.md` | Step-by-step: run the relay, connect a Watcher, flash a XIAO. |

## Quick start

```sh
cd src/relay
python3 relay.py            # open http://localhost:8787/
```

Then follow [docs/getting-started.md](docs/getting-started.md) to point a device at it.

## Supported hardware

- **Seeed SenseCAP Watcher** — the creature's body: round screen with eyes, camera, mic, speaker, battery. Works with stock firmware via its "Device AI Service" setting.
- **Seeed XIAO ESP32S3 Sense** — eyes and ears only (no screen, no speaker); good for wearables.
- **Rokid AI glasses** — planned: clue cards on the lens, first-person view.

---

## 中文简介

**Loci（地灵）**：一只会看、会说的口袋「地灵」，陪你在城市里解谜，讲这个地方的故事。

玩家把神兽对准线索（石刻、牌匾、城门），它拍下来交给 AI 判断「你是不是真的找到了」。找对了，它就讲一段故事，再给出下一站的线索。答案只在现场，AI 搜不到，它得看见你真的在那里。

起于 2026 年 10 月深圳 Seeed × 山海坞 AI 硬件创客松，目前是原型。

## License

[MIT](LICENSE)
