# Getting started

Three steps, smallest first. Each one answers a single question: **does it work, yes or no?** Don't move on until it does.

## 0. What you need

- A Mac (these steps use macOS) with Python 3
- A 2.4 GHz Wi-Fi network that the device and the Mac share. Venue Wi-Fi with a login page won't work for devices — use a phone hotspot. On iPhone, turn on **Maximize Compatibility** to get 2.4 GHz.
- For judging photos: either an `ANTHROPIC_API_KEY`, or the `claude` command-line tool logged in. Without either, the relay still runs and you judge by hand on the web page.

## 1. Run the relay

```sh
cd src/relay
python3 relay.py
```

Open <http://localhost:8787/>. You should see the first station of the demo route, and device slots at the top — Watcher, XIAO, glasses, phone. Each slot turns green the moment that device reaches the relay, so connect them one at a time and watch.
Find the Mac's address on the shared network — devices will need it:

```sh
ipconfig getifaddr en0      # e.g. 172.20.10.2
```

## 2. Play on a phone (quickest)

A phone browser only gets camera, microphone and location over HTTPS, so publish the relay through a tunnel:

```sh
LOCI_KEY=<a long random string> RELAY_HOST=127.0.0.1 python3 relay.py
cloudflared tunnel --url http://localhost:8787     # prints https://<random>.trycloudflare.com
```

Open `https://<random>.trycloudflare.com/play#key=<LOCI_KEY>` on the phone — in Safari on iPhone, not inside a chat app's browser, which may not allow the microphone or Add to Home Screen. The key stays in the link, so **Add to Home Screen** gives an icon that keeps working.

- `RELAY_HOST=127.0.0.1` keeps people on the same Wi-Fi out; only the tunnel reaches the relay. Leave it out if Watcher or XIAO devices on your LAN need to connect.
- If the tunnel keeps dropping, try `protocol: http2` in a cloudflared config file; if cloudflared exits because it can't find Cloudflare's edge (some routers don't answer DNS SRV lookups), add `--edge region1.v2.argotunnel.com:7844`.
- A quick tunnel's address changes every time it restarts; use a named tunnel for a fixed one.

## 2a. Connect a SenseCAP Watcher

1. Plug the **bottom** USB-C into a 5 V supply. Hold the wheel for 3 s to power on.
2. Install the **SenseCraft** app on your phone, bind the Watcher over Bluetooth, and join it to the same 2.4 GHz network as the Mac. (When registering, the *Website* field is optional.)
3. Create any simple task in the app and run it, to confirm the device itself works.
4. In the app: **Settings → Device AI Service**. URL: `http://<Mac address>:8787`. Token: leave empty — or set one and start the relay with `WATCHER_TOKEN=<same value> python3 relay.py`.
5. Run the task again and point the Watcher at the first station's target. The web page should show the photo and the verdict; on a correct find, the Watcher speaks the story.

If the relay log shows `陌生 POST /some/path`, the Watcher is calling an endpoint the relay doesn't implement yet — note the path.

## 2b. Flash and smoke-test a XIAO ESP32S3 Sense

"Flashing" means writing the program onto the board. The "smoke test" is the smallest run that proves the board works: camera found, focus OK, Wi-Fi up, a photo reaches the relay.

**One-time setup**

```sh
brew install arduino-cli
arduino-cli config init
arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32@3.3.8
git clone https://github.com/0015/ESP32-OV5640-AF.git ~/Arduino/libraries/ESP32-OV5640-AF
```

**Prepare the board**

1. Fit the camera board onto the XIAO (don't pull it straight up to remove it later), attach the Wi-Fi antenna, peel the film off the lens.
2. Connect it to the Mac with a **data** USB-C cable (some cables only charge).

**Configure**

```sh
cd src/firmware/xiao-senses
cp secrets.example.h secrets.h      # then edit: Wi-Fi name and password, RELAY_BASE = http://<Mac address>:8787
```

**Flash**

```sh
arduino-cli board list                                   # find the port, e.g. /dev/cu.usbmodem1101
arduino-cli compile -u -p /dev/cu.usbmodem1101 -b esp32:esp32:XIAO_ESP32S3:PSRAM=opi .
arduino-cli monitor -p /dev/cu.usbmodem1101 -c baudrate=115200
```

**What a pass looks like** (in the monitor):

```
camera ok: OV5640 (PID 0x5640)
OV5640 AF: focusInit=0 autoFocusMode=0 ready=1
Wi-Fi ok: <network>  IP 172.20.10.5
ready: camera=1 mic=1
```

Then:

- Open `http://<board IP>/capture` in a browser — is the picture sharp? Can you read text a metre away?
- Press **BOOT** once — the relay page should show the photo within a few seconds. Hold BOOT for about a second to record 3 s of audio instead.

**If it fails**

| You see | It means |
|---|---|
| `camera init failed 0x105` | Camera not detected — check the ribbon and that the camera board is seated |
| `没有 PSRAM` / no PSRAM | Compiled without `PSRAM=opi` |
| `focus not settled` | Auto-focus didn't converge — point at something with contrast, or check it's an OV5640 |
| `Wi-Fi not connected` | 5 GHz network, or wrong password; use a 2.4 GHz hotspot |
| Upload fails / no port | Hold BOOT while plugging in the cable, then flash again |
| POST `-1` in the log | The board can't reach the relay — same network? correct Mac address? firewall? |
