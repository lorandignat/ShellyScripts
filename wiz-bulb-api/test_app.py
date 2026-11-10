import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import app


class FakeRegistry:
    def entries(self):
        return [{"mac": "9877d50abae0", "ip": "192.168.1.176", "online": True}]

    def live_entries(self):
        return self.entries()

    def status(self, mac):
        if mac != "9877d50abae0":
            raise app.Unreachable(mac)
        return {"mac": mac, "ip": "192.168.1.176", "online": True,
                "state": True, "dimming": 80, "r": 255, "g": 0, "b": 0,
                "temp": None, "c": None, "w": None, "sceneId": None}

    def toggle(self, mac):
        return {"mac": mac, "state": True}

    def set_pilot(self, mac, payload):
        return {"mac": mac, "applied": {k: v for k, v in payload.items() if k in app.SETTABLE}}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ui = b"<!doctype html><title>bulbs</title>"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         app.make_handler(FakeRegistry(), "", ui))
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                return resp.status, json.loads(raw) if "json" in ctype else raw
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_ui(self):
        code, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn(b"<title>bulbs</title>", body)

    def test_health(self):
        self.assertEqual(self.request("/healthz"), (200, {"ok": True}))

    def test_status_has_color(self):
        code, body = self.request("/bulb/98:77:d5:0a:ba:e0")
        self.assertEqual(code, 200)
        self.assertEqual((body["r"], body["g"], body["b"]), (255, 0, 0))
        self.assertIn("temp", body)

    def test_bulbs_live(self):
        code, body = self.request("/bulbs?live=1")
        self.assertEqual(code, 200)
        self.assertEqual(body["bulbs"][0]["mac"], "9877d50abae0")

    def test_toggle(self):
        code, body = self.request("/bulb/9877D50ABAE0/toggle", "POST")
        self.assertEqual(code, 200)
        self.assertTrue(body["state"])

    def test_unreachable(self):
        code, body = self.request("/bulb/000000000000")
        self.assertEqual(code, 503)
        self.assertFalse(body["online"])

    def test_bad_mac(self):
        self.assertEqual(self.request("/bulb/nope")[0], 400)

    def test_set_ignores_unknown_keys(self):
        code, body = self.request("/bulb/9877d50abae0/set", "POST", {"state": True, "evil": 1})
        self.assertEqual(code, 200)
        self.assertEqual(body["applied"], {"state": True})


class UnitTest(unittest.TestCase):
    def test_normalize_mac(self):
        for value in ("98:77:d5:0a:ba:e0", "98-77-D5-0A-BA-E0", "9877d50abae0", "9877D50ABAE0"):
            self.assertEqual(app.normalize_mac(value), "9877d50abae0")
        for value in ("", "9877d50aba", "zz77d50abae0"):
            self.assertIsNone(app.normalize_mac(value))

    def test_parse_subnet(self):
        hosts = app.parse_subnet("192.168.1.0/30")
        self.assertEqual(hosts, ["192.168.1.1", "192.168.1.2"])

    def test_one_miss_only_means_no_response(self):
        mac = "aabbccddeeff"
        reg = app.Registry(hosts=["127.0.0.1"], scan_timeout=0.01, op_timeout=0.01,
                           workers=1, interval=60, ttl=180, probe_timeout=0.01,
                           response_grace=30, presence_interval=5,
                           presence_timeout=0.01)
        now = time.monotonic()
        with reg._lock:
            reg._by_mac[mac] = {"ip": "127.0.0.1", "present": True,
                                "last_present": now, "last_response": now}

        def dead(*_args, **_kwargs):
            raise OSError("timeout")

        original = app.wiz_call
        app.wiz_call = dead
        try:
            self.assertFalse(reg.probe(mac))
            self.assertTrue(reg._entry(mac)["present"])   # still on the LAN
            row = next(r for r in reg.entries() if r["mac"] == mac)
            self.assertTrue(row["responsive"])            # answered recently

            with reg._lock:
                reg._by_mac[mac]["last_response"] = time.monotonic() - 31
            row = next(r for r in reg.entries() if r["mac"] == mac)
            self.assertTrue(row["present"])
            self.assertFalse(row["responsive"])           # 30 s without reply
        finally:
            app.wiz_call = original

    def test_host_up_uses_tcp_refusal(self):
        self.assertTrue(app.Registry._host_up("127.0.0.1", 0.2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
