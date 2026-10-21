# Hardware notes

What each device can do for Loci, checked against vendor docs and firmware source (Oct 2026). If something here disagrees with the device in your hand, trust the device.

## Seeed SenseCAP Watcher (W1-A) — the creature's body

| | |
|---|---|
| Main chip | ESP32-S3, 8 MB PSRAM, 32 MB flash; Himax HX6538 AI co-processor |
| Screen | 1.45" round touch screen, 412×412 (ships showing a pair of eyes) |
| Camera | OV5647, 120° wide angle, fixed focus |
| Audio | 1 microphone, 1 W speaker |
| Power | 3.7 V 400 mAh battery; **5 V USB-C only** — higher voltage can damage it. The bottom USB-C port does power + flashing; the back one is power only |
| Radio | 2.4 GHz Wi-Fi only, BLE 5 |
| Controls | Scroll wheel (scroll + press), RGB LED |

Source: <https://wiki.seeedstudio.com/watcher_hardware_overview/>

**How Loci talks to it (no custom firmware needed).** In the SenseCraft app, *Device AI Service* lets you point the Watcher at your own server. The stock firmware then sends its image-analysis requests there:

- `POST {your URL}/v1/watcher/vision` with JSON `{img: base64 JPEG, prompt, audio_txt, type}`; the `Authorization` header carries the token you set in the app; timeout is 2 minutes for a self-hosted service.
- Reply `{"code": 200, "data": {"state": 1, "type": <same>, "audio": "<base64 WAV>"}}`. With `state: 1` the Watcher runs its alarm action and plays `audio`; with `state: 0` it stays quiet.
- Audio must be a WAV with a plain 44-byte header (RIFF / WAVE / fmt / data); the player reads sample rate, channels and bit depth from that header. `src/relay` produces 16 kHz mono 16-bit.

Source: `SenseCAP-Watcher-Firmware/examples/factory_firmware/main/task_flow_module/tf_module_img_analyzer.{c,h}` (path at lines 44/47 of the header), `tf_module_local_alarm.c` (playback), `app/app_audio_player.c` (WAV header check).

**Pitfalls**
- The SenseCraft cloud free tier allows one cloud image analysis per 15 minutes; a SenseCraft Pro trial (20,000 requests) starts when the device is bound; the self-hosted service is listed as unlimited ([pricing page](https://wiki.seeedstudio.com/watcher_price/)). In the app the setting is called *Watcher AI Service → Private Watcher Service*.
- Before flashing any other firmware (e.g. xiaozhi), back up the factory `nvsfactory` partition first, or the device may not reconnect to SenseCraft afterwards.
- How a task is triggered depends on its object ([task guide](https://wiki.seeedstudio.com/getting_started_with_watcher_task/)): objects with an on-device model (person, cat/dog, "paper" hand gesture) trigger locally in ~2 s; objects without one (e.g. "a sign that says …") run in pure-LLM mode and send a photo every *Capture Frequency*. The guide says at least 60 s; in Oct 2026 the app proposed 30 s. Verified: a "sign" task reached `/v1/watcher/vision` on the self-hosted relay.
- Verified on a real device (Oct 2026): the built-in templates (Human / Pet / Gesture) run entirely on the device and never call the self-hosted service. A typed task with a behaviour, e.g. "If you see a person smiling, play a sound", detects the person locally and then calls `/v1/watcher/vision`; a `state: 1` reply with a 16 kHz mono WAV is spoken by the Watcher.
- A task only starts after you tap **Run** on its card in the app; **Detail Config** sets the action (RGB light, speaker, notification) and the capture frequency. Push-to-talk (hold the wheel) is English-only.

## Seeed XIAO ESP32S3 Sense — eyes and ears for wearables

| | |
|---|---|
| Chip | ESP32-S3R8, 8 MB PSRAM, 8 MB flash |
| Camera | Current units ship **OV5640 with auto-focus** (earlier: OV3660, OV2640). Auto-focus is not initialised by the factory firmware — without it pictures come out blurry. `src/firmware/xiao-senses` does it the way Seeed's example does. |
| Mic | Digital PDM mic, data GPIO41, clock GPIO42 |
| Not on board | **No speaker, no screen** |
| Radio | 2.4 GHz Wi-Fi only, BLE 5 |
| Size | 21 × 17.8 mm |

Sources: <https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/>, <https://wiki.seeedstudio.com/xiao_esp32s3_camera_usage/>

**Pitfalls**
- Arduino board `XIAO_ESP32S3` defaults to PSRAM **disabled**; the camera needs `PSRAM=opi`.
- In USB HID mode the serial port disappears; to flash again, hold BOOT while plugging in.
- Peel the protective film off the lens.

## Rokid AI glasses — planned

Most likely the display model (binocular green monochrome display, 12 MP camera). Two official developer routes work with an iPhone:

- **AIUI Studio** — agents written in JS with a mini-program-like markup, tested in a web simulator, then pushed to the glasses through the phone app. <https://github.com/yodaos-project/AIUI>
- **CXR-L SDK for iOS** — `RGCxrClient` on CocoaPods: push custom UI to the lens, take photos, get audio.

China and global versions use different apps, accounts and studio sites; check which one you have first. Only the wearer sees the lens, so demos need screen mirroring.
