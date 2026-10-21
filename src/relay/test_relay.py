"""Relay tests — no hardware, no model calls.  Run:  python3 -m unittest -v test_relay"""

import base64
import http.client
import json
import os
import shutil
import struct
import tempfile
import threading
import time
import unittest

DATA = tempfile.mkdtemp(prefix="loci-test-")
os.environ["LOCI_DATA"] = DATA
os.environ["JUDGE"] = "manual"

import relay  # noqa: E402  (env must be set first)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
FAKE_WAV = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 28


class RelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = relay.serve("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(DATA, ignore_errors=True)

    def setUp(self):
        self.real_ask, self.real_tts = relay.ask_model, relay.tts_wav
        self.req("POST", "/reset")
        with relay.lock:
            relay.state["devices"].clear()

    def tearDown(self):
        relay.ask_model, relay.tts_wav = self.real_ask, self.real_tts

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        ctype = r.getheader("Content-Type", "")
        return r.status, (json.loads(data) if "json" in ctype else data)

    def wait_verdict(self):
        for _ in range(50):
            _, s = self.req("GET", "/api/v1/state")
            if s["verdict"] is not None:
                return s
            time.sleep(0.05)
        self.fail("no verdict")

    def test_page_and_compact_state(self):
        st, page = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"Loci", page)
        st, s = self.req("GET", "/api/v1/state")
        self.assertEqual(st, 200)
        for key in ("title", "station", "total", "riddle", "verdict", "unlocked", "devices"):
            self.assertIn(key, s)
        self.assertEqual(s["station"], 1)

    def test_xiao_upload_registers_device_and_judges(self):
        st, r = self.req("POST", "/upload", JPEG, {"X-Device": "xiao-senses-1", "Content-Type": "image/jpeg"})
        self.assertEqual((st, r), (200, {"ok": True}))
        s = self.wait_verdict()
        self.assertIsNone(s["verdict"]["match"])          # manual mode: a human decides
        self.assertEqual(s["devices"]["xiao-senses-1"]["kind"], "xiao")

    def test_upload_rejects_non_jpeg(self):
        st, r = self.req("POST", "/upload", b"hello")
        self.assertEqual(st, 400)

    def test_watcher_vision_silent_when_not_matched(self):
        body = json.dumps({"img": base64.b64encode(JPEG).decode(), "prompt": "p", "audio_txt": "a", "type": 1})
        st, r = self.req("POST", "/v1/watcher/vision", body, {"Content-Type": "application/json", "Authorization": "tok"})
        self.assertEqual(st, 200)
        self.assertEqual(r["code"], 200)
        self.assertEqual(r["data"]["state"], 0)
        self.assertNotIn("audio", r["data"])
        _, s = self.req("GET", "/api/v1/state")
        self.assertTrue(any(d["kind"] == "watcher" for d in s["devices"].values()))

    def test_watcher_vision_speaks_story_when_matched(self):
        relay.ask_model = lambda jpeg, station, path: {"match": True, "text": "找到了", "source": "test"}
        relay.tts_wav = lambda text: FAKE_WAV
        body = json.dumps({"img": base64.b64encode(JPEG).decode(), "type": 1})
        st, r = self.req("POST", "/v1/watcher/vision", body, {"Content-Type": "application/json"})
        self.assertEqual(r["data"]["state"], 1)
        self.assertEqual(r["data"]["type"], 1)
        self.assertEqual(base64.b64decode(r["data"]["audio"]), FAKE_WAV)
        _, s = self.req("GET", "/api/v1/state")
        self.assertEqual(s["solved"], [1])
        self.assertTrue(s["unlocked"])

    def test_watcher_vision_quiet_after_station_solved(self):
        calls = []
        relay.ask_model = lambda jpeg, station, path: calls.append(1) or {"match": True, "text": "找到了", "source": "test"}
        relay.tts_wav = lambda text: FAKE_WAV
        body = json.dumps({"img": base64.b64encode(JPEG).decode(), "type": 1})
        _, first = self.req("POST", "/v1/watcher/vision", body, {"Content-Type": "application/json"})
        _, second = self.req("POST", "/v1/watcher/vision", body, {"Content-Type": "application/json"})
        self.assertEqual(first["data"]["state"], 1)
        self.assertEqual(second["data"]["state"], 0)     # no retelling every capture
        self.assertEqual(len(calls), 1)                   # and no model call for a solved station

    def test_glasses_photo_returns_verdict_synchronously(self):
        relay.ask_model = lambda jpeg, station, path: {"match": False, "text": "不是这个", "source": "test"}
        body = json.dumps({"img": base64.b64encode(JPEG).decode()})
        st, r = self.req("POST", "/api/v1/photo", body, {"X-Device": "glasses-1", "Content-Type": "application/json"})
        self.assertEqual(st, 200)
        self.assertEqual(r["verdict"]["match"], False)
        self.assertEqual(r["devices"]["glasses-1"]["kind"], "glasses")

    def test_watcher_notification_event_with_image(self):
        body = json.dumps({"events": {"img": base64.b64encode(JPEG).decode(), "text": "alarm"}})
        st, r = self.req("POST", "/v1/notification/event", body, {"Content-Type": "application/json"})
        self.assertEqual((st, r["code"]), (200, 200))
        _, full = self.req("GET", "/state")
        self.assertTrue(full["photo"])

    def test_watcher_voice_chunked_body_is_saved(self):
        payload = os.urandom(5000)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/v2/watcher/talk/audio_stream")
        c.putheader("Transfer-Encoding", "chunked")
        c.putheader("Content-Type", "application/octet-stream")
        c.putheader("session-id", "s1")
        c.endheaders()
        for i in range(0, len(payload), 1024):
            part = payload[i:i + 1024]
            c.send(b"%X\r\n" % len(part) + part + b"\r\n")
        c.send(b"0\r\n\r\n")
        r = c.getresponse()
        resp = json.loads(r.read())
        c.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(resp["code"], 501)
        saved = sorted(p for p in os.listdir(DATA) if p.startswith("voice-") and p.endswith(".bin"))
        with open(os.path.join(DATA, saved[-1]), "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_keepalive_two_posts_on_one_connection(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        for _ in range(2):
            c.request("POST", "/upload", JPEG, {"X-Device": "xiao-1"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            r.read()
        c.close()

    def test_manual_judge_and_station_flow(self):
        self.req("POST", "/judge?ok=1")
        _, s = self.req("GET", "/api/v1/state")
        self.assertEqual(s["solved"], [1])
        self.req("POST", "/station?d=1")
        _, s = self.req("GET", "/api/v1/state")
        self.assertEqual(s["station"], 2)
        self.assertIsNone(s["verdict"])
        self.req("POST", "/station?d=-1")
        self.req("POST", "/station?d=-1")               # can't go below station 1
        _, s = self.req("GET", "/api/v1/state")
        self.assertEqual(s["station"], 1)

    def test_unknown_post_is_logged_not_crashing(self):
        st, r = self.req("POST", "/some/new/path", b"x")
        self.assertEqual(st, 404)
        _, full = self.req("GET", "/state")
        self.assertTrue(any("陌生 POST /some/new/path" in line for line in full["log"]))

    @unittest.skipUnless(shutil.which("say") and shutil.which("afconvert"), "macOS TTS only")
    def test_tts_wav_has_plain_44_byte_header(self):
        w = relay.tts_wav("你好")
        self.assertEqual((w[0:4], w[8:12], w[12:16], w[36:40]), (b"RIFF", b"WAVE", b"fmt ", b"data"))
        self.assertEqual(struct.unpack("<I", w[24:28])[0], 16000)
        self.assertEqual(struct.unpack("<H", w[22:24])[0], 1)


if __name__ == "__main__":
    unittest.main()
