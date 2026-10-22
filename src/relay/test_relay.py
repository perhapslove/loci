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
from pathlib import Path

DATA = tempfile.mkdtemp(prefix="loci-test-")
ROUTES = tempfile.mkdtemp(prefix="loci-routes-")
with open(os.path.join(ROUTES, "t-wander.json"), "w") as f:
    json.dump({"title": "测试漫游", "city": "吉隆坡", "mode": "wander", "stations": []}, f)
os.environ["LOCI_DATA"] = DATA
os.environ["ROUTES_DIR"] = ROUTES
os.environ["JUDGE"] = "manual"

import relay  # noqa: E402  (env must be set first)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
FAKE_WAV = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 28


def tone(seconds: float) -> bytes:
    """Loud 16 kHz mono 16-bit PCM, like someone speaking close to the mic."""
    n = int(16000 * seconds)
    return struct.pack(f"<{n}h", *(8000 if (i // 20) % 2 else -8000 for i in range(n)))


def talk_reply(body: bytes) -> tuple[dict, bytes]:
    """Split a push-to-talk reply the way the Watcher does: JSON, boundary, one newline, audio."""
    head, sep, rest = body.partition(b"---sensecraftboundary---")
    assert sep, body[:200]
    return json.loads(head), rest[1:]


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
        self.real = (relay.ask_model, relay.tts_wav, relay.transcribe, relay.ask_text, relay.ASR_URL,
                     relay.hear, relay.LOCI_KEY)
        with relay.lock:
            relay.state["route"] = str(relay.STATIONS_FILE)
        self.req("POST", "/reset")
        with relay.lock:
            relay.state["devices"].clear()
            relay.talks.clear()

    def tearDown(self):
        (relay.ask_model, relay.tts_wav, relay.transcribe, relay.ask_text, relay.ASR_URL,
         relay.hear, relay.LOCI_KEY) = self.real

    def wander(self):
        st, r = self.req("POST", "/api/v1/route", json.dumps({"route": "t-wander"}), {"Content-Type": "application/json"})
        self.assertEqual((st, r["mode"], r["city"]), (200, "wander", "吉隆坡"))

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
        relay.tts_wav = lambda text: FAKE_WAV
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
        head, wav = talk_reply(r.read())
        c.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(head["code"], 200)
        self.assertEqual(wav, FAKE_WAV)
        saved = sorted(p for p in os.listdir(DATA) if p.startswith("voice-") and p.endswith(".bin"))
        with open(os.path.join(DATA, saved[-1]), "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_tunnel_requests_only_reach_the_public_api(self):
        cf = {"Cf-Connecting-Ip": "203.0.113.9"}
        self.assertEqual(self.req("GET", "/", headers=cf)[0], 404)            # no web page
        self.assertEqual(self.req("GET", "/state", headers=cf)[0], 404)       # no full state / log
        self.assertEqual(self.req("GET", "/photo/x.jpg", headers=cf)[0], 404)  # no photos
        self.assertEqual(self.req("POST", "/reset", headers=cf)[0], 404)      # no operator buttons
        self.assertEqual(self.req("GET", "/api/v1/state", headers=cf)[0], 200)
        self.assertEqual(self.req("GET", "/")[0], 200)                        # the local network still can

    def test_talk_endpoint_answers_as_the_spirit(self):
        relay.ask_text = lambda prompt: "去找一个人吧。"
        st, r = self.req("POST", "/api/v1/talk?device=glasses-t", json.dumps({"text": "我该去哪"}),
                         {"Content-Type": "application/json"})
        self.assertEqual(st, 200)
        self.assertEqual((r["heard"], r["reply"]), ("我该去哪", "去找一个人吧。"))
        self.assertEqual(r["devices"]["glasses-t"]["kind"], "glasses")

    def test_routes_are_listed_and_switchable(self):
        _, r = self.req("GET", "/api/v1/routes")
        self.assertIn("t-wander", [x["route"] for x in r["routes"]])
        self.assertEqual(r["current"], "stations")
        self.wander()
        _, s = self.req("GET", "/api/v1/state")
        self.assertEqual((s["route"], s["total"], s["station"]), ("t-wander", 0, 0))
        self.assertEqual(self.req("POST", "/api/v1/route", json.dumps({"route": "nope"}),
                                  {"Content-Type": "application/json"})[0], 404)

    def test_wander_look_becomes_a_moment_and_can_be_kept(self):
        relay.tts_wav = lambda text: FAKE_WAV
        seen = []
        relay.ask_model = lambda jpeg, station, path: seen.append(station) or {"text": "一扇老木门", "people": False, "source": "test"}
        self.wander()
        st, r = self.req("POST", "/api/v1/photo?device=phone-t&lat=3.1478&lon=101.6953", JPEG, {"Content-Type": "image/jpeg"})
        self.assertEqual((st, r["say"]), (200, "一扇老木门"))
        self.assertIn("吉隆坡", seen[0]["instruction"])           # the spirit knows the city
        self.assertTrue(r["voice"].startswith("/api/v1/voice/"))
        moment = relay.journal.day()[-1]
        self.assertEqual((moment["kind"], moment["lat"], moment["kept"]), ("look", 3.1478, False))
        self.assertEqual(relay.state["devices"]["phone-t"]["kind"], "phone")
        _, k = self.req("POST", "/api/v1/keep?device=phone-t")
        self.assertEqual(k["kept"], moment["id"])
        _, d = self.req("GET", "/api/v1/digest")
        self.assertIn(moment["id"], [m["id"] for m in d["kept"]])

    def test_phone_recording_is_heard_and_answered(self):
        relay.tts_wav = lambda text: FAKE_WAV
        relay.hear = lambda audio: "这是什么地方"
        relay.ask_text = lambda prompt: "这是茨厂街。"
        self.wander()
        st, r = self.req("POST", "/api/v1/talk?device=phone-t", b"\x00" * 4000, {"Content-Type": "audio/mp4"})
        self.assertEqual((st, r["heard"], r["say"]), (200, "这是什么地方", "这是茨厂街。"))
        self.assertEqual(relay.journal.day()[-1]["kind"], "talk")

    def test_saying_keep_keeps_the_last_look(self):
        relay.tts_wav = lambda text: FAKE_WAV
        relay.ask_model = lambda jpeg, station, path: {"text": "骑楼", "people": False, "source": "test"}
        relay.hear = lambda audio: "帮我收起来"
        relay.ask_text = lambda prompt: self.fail("keeping is not a question for the model")
        self.wander()
        self.req("POST", "/api/v1/photo?device=phone-k", JPEG, {"Content-Type": "image/jpeg"})
        _, r = self.req("POST", "/api/v1/talk?device=phone-k", b"\x00" * 4000, {"Content-Type": "audio/mp4"})
        self.assertTrue(r["kept"])
        self.assertEqual(r["say"], "收起来了。")

    def test_tunnel_requests_need_the_key_when_set(self):
        relay.LOCI_KEY = "s3cret-key"
        cf = {"Cf-Connecting-Ip": "203.0.113.9"}
        self.assertEqual(self.req("GET", "/api/v1/state", headers=cf)[0], 401)
        self.assertEqual(self.req("GET", "/api/v1/state?key=wrong", headers=cf)[0], 401)
        self.assertEqual(self.req("GET", "/api/v1/state?key=s3cret-key", headers=cf)[0], 200)
        self.assertEqual(self.req("GET", "/api/v1/state", headers={**cf, "X-Loci-Key": "s3cret-key"})[0], 200)
        self.assertEqual(self.req("GET", "/play", headers=cf)[0], 200)           # the page itself is public
        self.assertEqual(self.req("GET", "/api/v1/state")[0], 200)               # the local network needs no key

    def test_nightly_keeps_kept_and_solved_photos_only(self):
        relay.tts_wav = lambda text: FAKE_WAV
        looks = iter([{"text": "门", "people": False}, {"text": "人群", "people": True}, {"text": "墙", "people": False}])
        relay.ask_model = lambda jpeg, station, path: {**next(looks), "source": "test"}
        self.wander()
        ids = []
        for _ in range(3):
            self.req("POST", "/api/v1/photo?device=phone-n", JPEG, {"Content-Type": "image/jpeg"})
            ids.append(relay.journal.day()[-1])
        self.req("POST", "/api/v1/keep?device=phone-n", json.dumps({"moment": ids[0]["id"]}), {"Content-Type": "application/json"})
        out = relay.nightly()
        self.assertGreaterEqual(out["deleted"], 2)
        self.assertTrue(os.path.exists(os.path.join(DATA, ids[0]["photo"])))
        self.assertFalse(os.path.exists(os.path.join(DATA, ids[1]["photo"])))   # people, not kept
        self.assertFalse(os.path.exists(os.path.join(DATA, ids[2]["photo"])))   # nobody kept it
        self.assertTrue(os.path.exists(os.path.join(DATA, "digests")))

    def test_nightly_drops_solved_photos_nobody_checked_for_people(self):
        day = time.strftime("%Y%m%d")
        for name, people in (("judge-unchecked.jpg", None), ("judge-clear.jpg", False)):
            with open(os.path.join(DATA, name), "wb") as f:
                f.write(JPEG)
            extra = {} if people is None else {"people": people}
            relay.journal.add("t-quest", "judge", "phone-j", photo=name, match=True, **extra)
        relay.journal.prune(day)
        self.assertFalse(os.path.exists(os.path.join(DATA, "judge-unchecked.jpg")))  # could be a stranger's face
        self.assertTrue(os.path.exists(os.path.join(DATA, "judge-clear.jpg")))

    def test_kept_photo_with_people_is_still_deleted_at_night(self):
        relay.tts_wav = lambda text: FAKE_WAV
        relay.ask_model = lambda jpeg, station, path: {"text": "路口的人群", "people": True, "source": "test"}
        self.wander()
        self.req("POST", "/api/v1/photo?device=phone-p", JPEG, {"Content-Type": "image/jpeg"})
        moment = relay.journal.day()[-1]
        _, k = self.req("POST", "/api/v1/keep?device=phone-p")
        self.assertEqual(k["kept"], moment["id"])
        self.assertIn("照片里有人", k["say"])                         # the player is told up front
        relay.journal.prune(time.strftime("%Y%m%d"))
        self.assertFalse(os.path.exists(os.path.join(DATA, moment["photo"])))   # a stranger's face is never kept

    def test_judgement_is_paired_with_its_own_photo(self):
        def judge_while_another_photo_arrives(jpeg, station, path):
            with relay.lock:
                relay.state["photo"] = "someone-else.jpg"
            return {"match": False, "text": "不是这个", "people": False, "source": "test"}
        relay.ask_model = judge_while_another_photo_arrives
        relay.tts_wav = lambda text: FAKE_WAV
        self.req("POST", "/api/v1/photo?device=phone-q", JPEG, {"Content-Type": "image/jpeg"})
        moment = relay.journal.day()[-1]
        self.assertNotEqual(moment["photo"], "someone-else.jpg")
        self.assertTrue(os.path.exists(os.path.join(DATA, moment["photo"])))

    def test_photo_names_are_unique_within_a_millisecond(self):
        self.assertEqual(len({relay.new_name(".jpg") for _ in range(3000)}), 3000)

    def test_photo_reply_and_moment_belong_to_this_request(self):
        def judge_while_others_move_on(jpeg, station, path):
            with relay.lock:
                relay.state["photo"] = "someone-else.jpg"     # a newer photo arrived
                relay.state["station"] = 1                    # and someone went on to station 2
            return {"match": True, "text": "找到了", "people": False, "source": "test"}
        relay.ask_model = judge_while_others_move_on
        relay.tts_wav = lambda text: FAKE_WAV
        _, r = self.req("POST", "/api/v1/photo?device=phone-s", JPEG, {"Content-Type": "image/jpeg"})
        self.assertIs(r["verdict"]["match"], True)            # not the newer photo's (empty) verdict
        self.assertEqual(relay.journal.day()[-1]["station"], 1)  # recorded for the station it judged
        self.assertIn(0, relay.state["solved"])

    def test_every_photo_entry_is_journaled(self):
        self.req("POST", "/upload", JPEG, {"X-Device": "xiao-j", "Content-Type": "image/jpeg"})
        self.wait_verdict()
        for _ in range(50):
            if relay.journal.day() and relay.journal.day()[-1]["device"] == "xiao-j":
                break
            time.sleep(0.05)
        self.assertEqual(relay.journal.day()[-1]["device"], "xiao-j")   # so the nightly tidy-up can delete it

    def test_watcher_photo_on_a_wander_does_not_crash(self):
        relay.ask_model = lambda jpeg, station, path: {"text": "一棵树", "people": False, "source": "test"}
        self.wander()
        body = json.dumps({"img": base64.b64encode(JPEG).decode(), "type": 1})
        st, r = self.req("POST", "/v1/watcher/vision", body, {"Content-Type": "application/json"})
        self.assertEqual((st, r["code"]), (200, 200))
        self.assertEqual(relay.journal.day()[-1]["kind"], "look")

    def test_pages_on_other_sites_and_rebound_names_count_as_outside(self):
        relay.LOCI_KEY = "k-123"
        evil = {"Origin": "https://evil.example"}
        self.assertEqual(self.req("GET", "/state", headers=evil)[0], 404)            # operator data: never
        self.assertEqual(self.req("POST", "/reset", headers=evil)[0], 404)
        self.assertEqual(self.req("GET", "/api/v1/digest", headers=evil)[0], 401)    # player API: key only
        self.assertEqual(self.req("GET", "/api/v1/digest?key=k-123", headers=evil)[0], 200)
        rebound = {"Host": f"evil.example:{self.port}"}                              # DNS rebinding
        self.assertEqual(self.req("GET", "/state", headers=rebound)[0], 404)
        own = {"Origin": f"http://127.0.0.1:{self.port}"}                            # the operator page itself
        self.assertEqual(self.req("POST", "/reset", headers=own)[0], 200)
        self.assertEqual(self.req("GET", "/state", headers={"Host": f"localhost:{self.port}"})[0], 200)

    def test_photos_with_people_are_never_sent_out(self):
        relay.tts_wav = lambda text: FAKE_WAV
        looks = iter([{"text": "人群", "people": True}, {"text": "门", "people": False}])
        relay.ask_model = lambda jpeg, station, path: {**next(looks), "source": "test"}
        self.wander()
        names = []
        for _ in range(2):
            self.req("POST", "/api/v1/photo?device=phone-v", JPEG, {"Content-Type": "image/jpeg"})
            names.append(relay.journal.day()[-1]["photo"])
            self.req("POST", "/api/v1/keep?device=phone-v")
        self.assertEqual(self.req("GET", f"/api/v1/photo-file/{names[0]}")[0], 404)
        self.assertEqual(self.req("GET", f"/api/v1/photo-file/{names[1]}")[0], 200)
        _, d = self.req("GET", "/api/v1/digest")
        shown = {m["said"]: m["photo"] for m in d["kept"] if m["device"] == "phone-v"}
        self.assertEqual(shown, {"人群": None, "门": names[1]})                     # the words stay

    def test_a_judgement_cannot_solve_a_route_switched_to_meanwhile(self):
        def judge_while_switching(jpeg, station, path):
            self.req("POST", "/api/v1/route", json.dumps({"route": "t-wander"}), {"Content-Type": "application/json"})
            self.req("POST", "/api/v1/route", json.dumps({"route": "stations"}), {"Content-Type": "application/json"})
            return {"match": True, "text": "找到了", "people": False, "source": "test"}
        relay.ask_model = judge_while_switching
        relay.tts_wav = lambda text: FAKE_WAV
        self.req("POST", "/api/v1/photo?device=phone-w", JPEG, {"Content-Type": "image/jpeg"})
        self.assertEqual(relay.state["solved"], [])                                 # the new play-through starts clean

    def test_a_journal_write_cut_short_leaves_the_old_journal(self):
        from moments import Journal
        j = Journal(Path(tempfile.mkdtemp(prefix="loci-j-")))
        first = j.add("t-crash", "look", "phone-c", photo="a.jpg", people=False)
        real_write = Path.write_text
        def disk_full(self, data, *a, **k):
            real_write(self, data[: len(data) // 2], *a, **k)
            raise OSError(28, "No space left on device")
        Path.write_text = disk_full
        try:
            with self.assertRaises(OSError):
                j.keep("phone-c")
        finally:
            Path.write_text = real_write
        self.assertEqual([m["id"] for m in j.day()], [first["id"]])        # still readable, nothing lost

    def test_moment_ids_are_unique_within_a_millisecond(self):
        from moments import Journal
        j = Journal(Path(tempfile.mkdtemp(prefix="loci-j-")))
        self.assertEqual(len({j.add("t-ids", "talk", "phone-i")["id"] for _ in range(2000)}), 2000)

    def test_a_manual_verdict_reaches_the_journal(self):
        relay.ask_model = lambda jpeg, station, path: {"match": False, "text": "不像", "people": False, "source": "test"}
        relay.tts_wav = lambda text: FAKE_WAV
        self.req("POST", "/api/v1/photo?device=phone-m", JPEG, {"Content-Type": "image/jpeg"})
        moment = relay.journal.day()[-1]
        self.req("POST", "/judge?ok=1")
        self.assertIs([m for m in relay.journal.day() if m["id"] == moment["id"]][0]["match"], True)
        _, d = self.req("GET", "/api/v1/digest")
        self.assertIn(moment["id"], [m["id"] for m in d["kept"]])           # a solved station, photo kept tonight

    def test_photos_no_moment_points_at_are_swept(self):
        day = "20200103"
        os.makedirs(os.path.join(DATA, "runs", f"{day}-t-orphan"), exist_ok=True)
        old, fresh = os.path.join(DATA, f"{day}-000000-000-0001.jpg"), os.path.join(DATA, f"{day}-000000-000-0002.jpg")
        for f in (old, fresh):
            with open(f, "wb") as fh:
                fh.write(JPEG)
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        relay.journal.prune(day)
        self.assertFalse(os.path.exists(old))          # the relay stopped before recording it
        self.assertTrue(os.path.exists(fresh))         # may still be with the model

    def raw(self, data: bytes, half_close=False) -> bytes:
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        s.sendall(data)
        if half_close:
            s.shutdown(socket.SHUT_WR)
        try:
            return s.recv(200)
        finally:
            s.close()

    def test_a_chunked_body_cut_off_does_not_hang_the_relay(self):
        head = b"POST /api/v1/log HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nab"
        self.assertTrue(self.raw(head, half_close=True).startswith(b"HTTP/1.1 400"))

    def test_an_oversized_body_is_refused_without_reading_it(self):
        head = b"POST /api/v1/photo HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 300000000\r\n\r\n"
        self.assertTrue(self.raw(head).startswith(b"HTTP/1.1 413"))

    def test_judging_wrong_by_hand_undoes_a_solve(self):
        self.req("POST", "/judge?ok=1")
        self.assertEqual(relay.state["solved"], [0])
        self.req("POST", "/judge?ok=0")
        _, s = self.req("GET", "/api/v1/state")
        self.assertEqual((relay.state["solved"], s["unlocked"]), ([], None))

    def test_claude_goes_direct_when_the_proxy_is_not_running(self):
        real = relay.CLAUDE_PROXY
        try:
            relay.CLAUDE_PROXY = "http://127.0.0.1:1"          # nothing listens on port 1
            self.assertNotIn("HTTPS_PROXY", relay.claude_env())
        finally:
            relay.CLAUDE_PROXY = real

    def test_a_half_written_last_line_does_not_block_the_day(self):
        from moments import Journal
        j = Journal(Path(tempfile.mkdtemp(prefix="loci-j-")))
        first = j.add("t-cut", "look", "phone-h", people=False)
        with open(j._file("t-cut"), "a", encoding="utf-8") as f:
            f.write('{"id": "2", "photo": "x.jp')                       # the relay stopped mid-append
        self.assertEqual([m["id"] for m in j.day()], [first["id"]])
        j.prune(time.strftime("%Y%m%d"))                                 # and the tidy-up still runs

    def test_a_verdict_by_hand_beats_the_model_still_thinking(self):
        def model_says_yes_after_a_person_said_no(jpeg, station, path):
            self.req("POST", "/judge?ok=0")
            return {"match": True, "text": "找到了", "people": False, "source": "test"}
        relay.ask_model = model_says_yes_after_a_person_said_no
        relay.tts_wav = lambda text: FAKE_WAV
        self.req("POST", "/api/v1/photo?device=phone-h", JPEG, {"Content-Type": "image/jpeg"})
        self.assertEqual(relay.state["solved"], [])
        self.assertIs(relay.journal.day()[-1]["match"], False)

    def test_a_moment_is_filed_under_the_day_its_photo_was_taken(self):
        photo = "20200104-235959-999-0001.jpg"
        relay.journal.add("t-midnight", "look", "phone-n", photo=photo, people=False)
        self.assertTrue(os.path.exists(os.path.join(DATA, "runs", "20200104-t-midnight", "moments.jsonl")))
        self.assertTrue(relay.journal.no_people(photo))

    def test_a_photo_sent_late_keeps_when_and_where_it_was_taken(self):
        relay.ask_model = lambda jpeg, station, path: {"text": "老街口", "people": False, "source": "test"}
        relay.tts_wav = lambda text: FAKE_WAV
        self.wander()
        _, r = self.req("POST", "/api/v1/photo?device=phone-l&at=1792600000123&lat=1.2834&lon=103.8607", JPEG,
                        {"Content-Type": "image/jpeg"})
        m = relay.journal.day()[-1]
        self.assertEqual((m["id"], m["taken_at"], m["lat"]), (r["moment"], 1792600000.123, 1.2834))

    def test_a_late_photo_from_another_route_is_only_described_there(self):
        seen = []
        relay.ask_model = lambda jpeg, station, path: seen.append(station) or {"text": "旧街的门", "people": False, "source": "test"}
        relay.tts_wav = lambda text: FAKE_WAV
        # taken on the KL wander, sent after the player switched back to the quest
        _, r = self.req("POST", "/api/v1/photo?device=phone-x&at=1792600001000&route=t-wander", JPEG, {"Content-Type": "image/jpeg"})
        m = relay.journal.day()[-1]
        self.assertEqual((m["kind"], m["route"]), ("look", "t-wander"))
        self.assertIn("吉隆坡", seen[0]["instruction"])                   # that route's city, not today's
        self.assertEqual(relay.state["solved"], [])

    def test_a_late_photo_for_an_old_station_does_not_solve_the_new_one(self):
        relay.ask_model = lambda jpeg, station, path: {"text": "一块牌子", "match": True, "people": False, "source": "test"}
        relay.tts_wav = lambda text: FAKE_WAV
        with relay.lock:
            relay.state["station"] = 1                                    # the player is on station 2 now
        self.req("POST", "/api/v1/photo?device=phone-y&at=1792600002000&route=stations&station=1", JPEG,
                 {"Content-Type": "image/jpeg"})
        self.assertEqual((relay.state["solved"], relay.journal.day()[-1]["kind"]), ([], "look"))

    def test_a_late_photo_sent_twice_is_judged_once(self):
        calls = []
        relay.ask_model = lambda jpeg, station, path: calls.append(1) or {"text": "桥", "people": False, "source": "test"}
        relay.tts_wav = lambda text: FAKE_WAV
        self.wander()
        q = "/api/v1/photo?device=phone-z&at=1792600003000&route=t-wander"
        _, first = self.req("POST", q, JPEG, {"Content-Type": "image/jpeg"})
        _, again = self.req("POST", q, JPEG, {"Content-Type": "image/jpeg"})   # the first answer got lost
        self.assertEqual((first["moment"], len(calls)), (again["moment"], 1))
        self.assertEqual(sum(m["device"] == "phone-z" for m in relay.journal.day()), 1)

    def test_a_new_station_does_not_inherit_the_last_photo(self):
        relay.ask_model = lambda jpeg, station, path: {"match": True, "text": "找到了", "people": False, "source": "test"}
        relay.tts_wav = lambda text: FAKE_WAV
        self.req("POST", "/api/v1/photo?device=phone-s2", JPEG, {"Content-Type": "image/jpeg"})
        first = relay.journal.day()[-1]
        self.req("POST", "/api/v1/next")
        self.assertIsNone(relay.state["photo"])
        self.req("POST", "/judge?ok=1")                                  # by hand, before any new photo
        again = [m for m in relay.journal.day() if m["id"] == first["id"]][0]
        self.assertNotEqual(again.get("by"), "manual")                    # station 1's record left alone

    def test_a_negative_length_is_refused(self):
        head = b"POST /api/v1/log HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: -1\r\n\r\n" + b"x" * 64
        self.assertTrue(self.raw(head).startswith(b"HTTP/1.1 400"))

    def test_a_route_switch_while_a_photo_is_on_its_way_solves_nothing(self):
        relay.ask_model = lambda jpeg, station, path: {"match": True, "text": "找到了", "people": False, "source": "test"}
        relay.tts_wav = lambda text: FAKE_WAV
        real, switched = relay.load_route, []
        def load_then_someone_switches(path=None):
            route = real(path)
            if not switched:
                switched.append(1)
                with relay.lock:
                    relay.state.update(route=os.path.join(ROUTES, "t-wander.json"), station=0, solved=[],
                                       epoch=relay.state["epoch"] + 1)
            return route
        relay.load_route = load_then_someone_switches
        try:
            self.req("POST", "/api/v1/photo?device=phone-r2", JPEG, {"Content-Type": "image/jpeg"})
        finally:
            relay.load_route = real
        self.assertEqual(relay.state["solved"], [])                     # the old question can't solve the new route

    def test_a_negative_chunk_size_is_refused(self):
        head = (b"POST /api/v1/log HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"-1\r\n" + b"x" * 64)
        self.assertTrue(self.raw(head).startswith(b"HTTP/1.1 400"))

    def test_progress_never_mixes_two_routes(self):
        one = os.path.join(ROUTES, "t-one.json")
        with open(one, "w") as f:
            json.dump({"title": "一站", "stations": [{"riddle": "唯一一站", "target": "门"}]}, f)
        with relay.lock:
            relay.state.update(route=one, station=0, solved=[])
        real, switched = relay.load_route, []
        def load_then_someone_moves(path=None):
            route = real(path)
            if not switched:                         # another request: a 3-station route, on station 3
                switched.append(1)
                with relay.lock:
                    relay.state.update(route=str(relay.STATIONS_FILE), station=2, solved=[])
            return route
        relay.load_route = load_then_someone_moves
        try:
            st, s = self.req("GET", "/api/v1/state")
        finally:
            relay.load_route = real
            os.remove(one)
        self.assertEqual((st, s["route"], s["riddle"]), (200, "t-one", "唯一一站"))

    def test_talk_from_the_old_route_does_not_follow_into_the_new_one(self):
        relay.tts_wav = lambda text: FAKE_WAV
        def answers_while_someone_switches(prompt):
            self.req("POST", "/api/v1/route", json.dumps({"route": "t-wander"}), {"Content-Type": "application/json"})
            return "这一站的牌子在桥边"
        relay.ask_text = answers_while_someone_switches
        _, r = self.req("POST", "/api/v1/talk?device=phone-t2", json.dumps({"text": "提示一下"}),
                        {"Content-Type": "application/json"})
        self.assertIn("路线换了", r["reply"])                              # the late answer is dropped
        self.assertNotIn("phone-t2", relay.talks)                           # no old-city memory in the new route
        self.assertEqual(relay.journal.day()[-1]["route"], "stations")      # filed under what it was about

    def test_a_digest_is_rebuilt_when_its_day_changed_after_it(self):
        real_at = relay.PRUNE_AT
        relay.PRUNE_AT = "99:99"
        try:
            day = time.strftime("%Y%m%d", time.localtime(time.time() - 86400))   # yesterday: still watched
            self.past_day(day, "late-1.jpg")
            done = set()
            relay.nightly_tick(done)                                      # first pass writes the digest
            digest = os.path.join(DATA, "digests", f"{day}.json")
            first = os.path.getmtime(digest)
            relay.nightly_tick(done)                                      # nothing changed: left alone
            self.assertEqual(os.path.getmtime(digest), first)
            run = os.path.join(DATA, "runs", f"{day}-t-old", "moments.jsonl")
            with open(run, "a") as f:                                     # a talk after the first pass
                f.write(json.dumps({"id": "2", "t": 2.0, "route": "t-old", "kind": "talk", "device": "phone-o",
                                    "kept": False, "heard": "晚安", "said": "明天见"}) + "\n")
            os.utime(run, (first + 5, first + 5))
            relay.nightly_tick(done)
            with open(digest) as f:
                self.assertEqual(json.load(f)["counts"]["talk"], 1)
        finally:
            relay.PRUNE_AT = real_at

    def test_keep_reply_tells_people_from_unknown(self):
        self.assertIn("照片里有人", relay.keep_reply({"people": True}))
        self.assertIn("没看清", relay.keep_reply({"people": None}))
        self.assertEqual(relay.keep_reply({"people": False}), "收起来了。")

    def test_keep_takes_the_latest_moment_across_routes(self):
        older = relay.journal.add("z-route", "look", "phone-r", people=False)
        time.sleep(0.01)
        newer = relay.journal.add("a-route", "look", "phone-r", people=False)
        self.assertEqual(relay.journal.keep("phone-r")["id"], newer["id"])
        self.assertNotEqual(older["id"], newer["id"])

    def test_device_names_cannot_carry_markup(self):
        self.req("GET", "/api/v1/state", headers={"X-Device": '<img src=x onerror="alert(1)">'})
        self.assertEqual(list(relay.state["devices"]), ["glasses-127.0.0.1"])

    def past_day(self, day: str, photo: str) -> None:
        run = os.path.join(DATA, "runs", f"{day}-t-old")
        os.makedirs(run, exist_ok=True)
        with open(os.path.join(DATA, photo), "wb") as f:
            f.write(JPEG)
        with open(os.path.join(run, "moments.jsonl"), "w") as f:
            f.write(json.dumps({"id": "1", "t": 1.0, "route": "t-old", "kind": "look", "device": "phone-o",
                                "kept": False, "photo": photo, "people": False}) + "\n")

    def test_nightly_catches_up_on_days_it_missed_and_retries_failures(self):
        real_nightly, real_at = relay.nightly, relay.PRUNE_AT
        relay.PRUNE_AT = "99:99"                                      # leave today alone in this test
        try:
            self.past_day("20200101", "old-1.jpg")                     # the Mac slept through that night
            done = set()
            relay.nightly_tick(done)
            self.assertFalse(os.path.exists(os.path.join(DATA, "old-1.jpg")))
            self.assertTrue(os.path.exists(os.path.join(DATA, "digests", "20200101.json")))
            self.assertIn("20200101", done)

            self.past_day("20200102", "old-2.jpg")                     # taken after that day's 23:30 pass
            done = {"20200101", "20200102+early"}
            def fail_once(day):
                relay.nightly = real_nightly
                raise OSError("disk busy")
            relay.nightly = fail_once
            relay.nightly_tick(done)
            self.assertNotIn("20200102", done)                         # a failed pass is not counted
            relay.nightly_tick(done)
            self.assertIn("20200102", done)
            self.assertFalse(os.path.exists(os.path.join(DATA, "old-2.jpg")))
        finally:
            relay.nightly, relay.PRUNE_AT = real_nightly, real_at

    def test_voice_files_are_served(self):
        key = relay.tts_key("你好")
        with open(os.path.join(DATA, "tts", f"{key}.wav"), "wb") as f:
            f.write(FAKE_WAV)
        st, body = self.req("GET", f"/api/v1/voice/{key}.wav")
        self.assertEqual((st, body), (200, FAKE_WAV))

    def test_device_log_lands_in_the_relay_log(self):
        cf = {"Cf-Connecting-Ip": "203.0.113.9", "Content-Type": "application/json"}
        st, r = self.req("POST", "/api/v1/log?device=glasses-t", json.dumps({"msg": "takePhoto failed:\nInvalid"}), cf)
        self.assertEqual((st, r), (200, {"ok": True}))
        _, full = self.req("GET", "/state")
        self.assertTrue(any("设备日志（glasses-t）takePhoto failed: Invalid" in line for line in full["log"]))

    def test_next_only_from_a_solved_station(self):
        _, r = self.req("POST", "/api/v1/next")
        self.assertEqual((r["moved"], r["station"]), (False, 1))
        self.req("POST", "/judge?ok=1")
        _, r = self.req("POST", "/api/v1/next")
        self.assertEqual((r["moved"], r["station"]), (True, 2))

    def test_voiced_seconds_tells_silence_from_speech(self):
        self.assertEqual(relay.voiced_seconds(b"\x00\x00" * 16000), 0.0)
        quiet_then_loud = struct.pack("<16000h", *([50] * 16000)) + tone(1.0)
        self.assertGreater(relay.voiced_seconds(quiet_then_loud), 0.9)

    def test_watcher_talk_silence_is_not_transcribed(self):
        relay.tts_wav = lambda text: FAKE_WAV
        relay.ASR_URL = "http://asr.invalid"
        relay.transcribe = lambda pcm: self.fail("silence must not reach the ASR")
        st, body = self.req("POST", "/v2/watcher/talk/audio_stream", b"\x00\x00" * 32000)
        head, wav = talk_reply(body)
        self.assertEqual(head["data"]["stt_result"], "")
        self.assertIn("没听清", head["data"]["screen_text"])

    def test_watcher_talk_answers_what_it_heard(self):
        relay.tts_wav = lambda text: FAKE_WAV
        relay.ASR_URL = "http://asr.invalid"
        relay.transcribe = lambda pcm: "地灵你好"
        prompts = []
        relay.ask_text = lambda prompt: prompts.append(prompt) or "地灵：你好呀，去找山海坞吧。"
        st, body = self.req("POST", "/v2/watcher/talk/audio_stream", tone(1.0))
        head, wav = talk_reply(body)
        self.assertEqual(st, 200)
        self.assertEqual(head["data"]["mode"], 0)
        self.assertEqual(head["data"]["stt_result"], "地灵你好")
        self.assertEqual(head["data"]["screen_text"], "你好呀，去找山海坞吧。")
        self.assertEqual(wav, FAKE_WAV)
        self.assertIn("第1站", prompts[0])          # the spirit knows where the player is
        self.req("POST", "/v2/watcher/talk/audio_stream", tone(1.0))
        self.assertIn("你好呀", prompts[1])          # and remembers the last exchange

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
