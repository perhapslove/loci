"""Read or change the task running on a SenseCAP Watcher over its USB console (stock firmware).

The SenseCraft app won't let an LLM-mode task capture more often than every 30 s. The firmware has no
such floor: the interval is `silent_period.silence_duration` of the "ai camera" module in the task flow,
and the built-in Local Human Detection task uses 5 s. This tool exports the running task flow, edits it
and imports it back through the console commands `taskflow -e` / `taskflow -i -j`
(SenseCAP-Watcher-Firmware/examples/factory_firmware/main/app/app_cmd.c).

    pip install pyserial
    python3 watcher_taskflow.py show              # print the running task flow
    python3 watcher_taskflow.py every 5           # capture every 5 s and make sure the speaker is on
    python3 watcher_taskflow.py load saved.json   # put a saved task flow back

Plug the Watcher's bottom USB-C port into the computer first, and start a task in the app (Run) so
there is something to edit. Every change saves the previous task flow to taskflow-<time>.json.
"""
import glob
import json
import re
import sys
import time

import serial

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def open_watcher(port: str | None = None) -> serial.Serial:
    """The bottom USB-C port shows two serial ports (ESP32-S3 and the Himax AI chip). Opening the
    ESP32-S3 one restarts the Watcher, so wait for it to boot (it reloads its saved task) before talking."""
    ports = [port] if port else sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
    for p in ports:
        ser = serial.Serial()
        ser.port, ser.baudrate, ser.timeout = p, 115200, 0.2
        ser.dtr = ser.rts = False
        ser.open()
        seen, end = b"", time.time() + 3
        while time.time() < end:
            seen += ser.read(4096)
        if b"ESP-ROM" not in seen and b"I (" not in seen and b"SenseCAP" not in seen:
            ser.close()                     # not the ESP32-S3 console
            continue
        print(f"Watcher console on {p}, waiting for it to boot …")
        for _ in range(12):                 # boot takes ~10-20 s
            if "taskflow" in console(ser, "help", wait=3):
                return ser
        ser.close()
    sys.exit(f"no Watcher console answered on {ports or 'any USB serial port'}; use the bottom USB-C port")


def console(ser: serial.Serial, line: str, wait: float = 2.0, until: str | None = None) -> str:
    ser.reset_input_buffer()
    ser.write(line.encode() + b"\r\n")
    out, end = "", time.time() + wait
    while time.time() < end:
        out += ser.read(4096).decode(errors="replace")
        if until and until in out:
            break
    return ANSI.sub("", out)


def export(ser: serial.Serial, quiet: bool = False) -> dict | None:
    out = console(ser, "taskflow -e", wait=3)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{") and '"task_flow"' in line:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    if not quiet:
        print(out.strip()[-400:])
    return None


def load(ser: serial.Serial, flow: dict) -> None:
    out = console(ser, "taskflow -i -j", wait=3, until="Please input taskflow json")
    if "Please input" not in out:
        sys.exit(f"the Watcher did not ask for the JSON:\n{out[-400:]}")
    data = json.dumps(flow, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    for i in range(0, len(data), 256):     # small chunks so the USB buffer keeps up
        ser.write(data[i:i + 256])
        time.sleep(0.02)
    print(ANSI.sub("", ser.read(4096).decode(errors="replace")).strip()[-300:])


def set_interval(flow: dict, seconds: int) -> list[str]:
    """Capture every `seconds` and switch the local alarm's speaker on. Returns what changed."""
    changed = []
    for m in flow.get("task_flow", []):
        p = m.setdefault("params", {})
        if m.get("type") == "ai camera":
            old = p.setdefault("silent_period", {}).get("silence_duration")
            p["silent_period"]["silence_duration"] = seconds
            changed.append(f"ai camera silence_duration {old} -> {seconds}")
        elif m.get("type") == "local alarm" and p.get("sound") != 1:
            p["sound"] = 1
            changed.append("local alarm sound -> 1")
    return changed


def backup(flow: dict) -> str:
    name = time.strftime("taskflow-%Y%m%d-%H%M%S.json")
    with open(name, "w") as f:
        json.dump(flow, f, ensure_ascii=False, indent=1)
    return name


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in ("show", "every", "load"):
        sys.exit(__doc__)
    ser = open_watcher()
    flow = export(ser)
    if argv[0] == "show":
        print(json.dumps(flow, ensure_ascii=False, indent=1) if flow else "no task running")
        return
    if flow is not None:
        print(f"saved the current task flow to {backup(flow)}")
    if argv[0] == "every":
        if flow is None:
            sys.exit("no task running: start one in the app (Run), then try again")
        for c in set_interval(flow, int(argv[1])):
            print(c)
    else:
        flow = json.load(open(argv[1]))
    load(ser, flow)
    time.sleep(3)
    now = export(ser)
    cams = [m["params"].get("silent_period", {}) for m in (now or {}).get("task_flow", []) if m.get("type") == "ai camera"]
    print(f"running now: {now.get('tn') if now else None} · ai camera {cams}")


if __name__ == "__main__":
    main(sys.argv[1:])
