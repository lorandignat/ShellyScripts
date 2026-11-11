import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import app


class FakeRegistry:
    """Small in-memory stand-in used to exercise HTTP routing without bulbs."""
    def entries(self):
        return [{"mac": "9877d50abae0", "ip": "192.168.1.176", "online": True}]

    def live_entries(self):
        return self.entries()

    def category_counts(self):
        return [{"id": "c1", "name": "Bedroom", "count": 1}]

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
    """Verify the public HTTP contract using an ephemeral local server."""
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
        self.assertEqual(body["categories"][0]["name"], "Bedroom")

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
    """Cover address parsing, presence tracking, probing, and persistence."""
    def test_normalize_mac(self):
        for value in ("98:77:d5:0a:ba:e0", "98-77-D5-0A-BA-E0", "9877d50abae0", "9877D50ABAE0"):
            self.assertEqual(app.normalize_mac(value), "9877d50abae0")
        for value in ("", "9877d50aba", "zz77d50abae0"):
            self.assertIsNone(app.normalize_mac(value))

    def test_parse_subnet(self):
        hosts = app.parse_subnet("192.168.1.0/30")
        self.assertEqual(hosts, ["192.168.1.1", "192.168.1.2"])

    def test_one_miss_only_means_no_response(self):
        # UDP response health and LAN presence are deliberately independent:
        # a missed getPilot must not mark a TCP-reachable bulb as offline.
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

    def _registry_with(self, mac="aabbccddeeff", present=True):
        now = time.monotonic()
        reg = app.Registry(hosts=["127.0.0.1"], scan_timeout=0.01, op_timeout=0.01,
                           workers=1, interval=60, ttl=180, probe_timeout=0.01,
                           response_grace=30, presence_interval=5,
                           presence_timeout=0.01)
        with reg._lock:
            reg._by_mac[mac] = {"ip": "127.0.0.1", "present": present,
                                "last_present": now, "last_response": now}
        return reg, mac

    def test_probe_is_rate_limited(self):
        reg, mac = self._registry_with()
        with reg._lock:
            reg._by_mac[mac]["last_probe"] = time.monotonic()

        def boom(*_args, **_kwargs):
            raise AssertionError("probe hit the bulb too soon")

        original = app.wiz_call
        app.wiz_call = boom
        try:
            self.assertTrue(reg.probe(mac))      # gated: no network call
        finally:
            app.wiz_call = original

    def test_state_persists_categories_and_removals(self):
        # Recreate the registry from disk to verify that durable metadata,
        # category assignments, and removals survive a process restart.
        def make(path):
            return app.Registry(hosts=[], scan_timeout=0.01, op_timeout=0.01,
                                workers=1, interval=60, ttl=180,
                                state_file=path)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            reg = make(path)
            with reg._lock:
                reg._by_mac["aabbccddeeff"] = {
                    "ip": "192.168.1.50", "model": "X", "present": False,
                    "first_seen": time.time(),
                }
            cat = reg.add_category("Bedroom")
            with self.assertRaises(ValueError):
                reg.add_category("bedroom")          # case-insensitive dupe
            reg.set_category("aabbccddeeff", cat["id"])

            reloaded = make(path)
            self.assertEqual(reloaded.category_counts()[0]["count"], 1)
            self.assertEqual(reloaded._entry("aabbccddeeff")["ip"],
                             "192.168.1.50")
            self.assertEqual(reloaded._entry("aabbccddeeff")["category"],
                             cat["id"])

            reloaded.remove_category(cat["id"])
            self.assertIsNone(reloaded._entry("aabbccddeeff")["category"])
            reloaded.remove_bulb("aabbccddeeff")
            self.assertIsNone(reloaded._entry("aabbccddeeff"))
            self.assertIsNone(make(path)._entry("aabbccddeeff"))

    def test_order_is_stable_when_ip_changes(self):
        reg, mac = self._registry_with()
        with reg._lock:
            reg._by_mac[mac]["first_seen"] = 10.0
            reg._by_mac["112233445566"] = {"ip": "192.168.1.2", "present": True,
                                           "first_seen": 20.0}
            reg._by_mac[mac]["ip"] = "192.168.1.250"
        self.assertEqual([r["mac"] for r in reg.entries()],
                         [mac, "112233445566"])

    def test_presence_retries_a_single_tcp_timeout(self):
        reg, mac = self._registry_with()
        calls = {"n": 0}

        def flaky(ip, timeout):
            calls["n"] += 1
            return calls["n"] > 1        # first attempt times out

        original = app.Registry._host_up
        app.Registry._host_up = staticmethod(flaky)
        try:
            reg.check_presence()
        finally:
            app.Registry._host_up = original
        self.assertTrue(reg._entry(mac)["present"])
        self.assertEqual(calls["n"], 2)

    def test_presence_marks_absent_after_two_failures(self):
        reg, mac = self._registry_with()
        original = app.Registry._host_up
        app.Registry._host_up = staticmethod(lambda ip, timeout: False)
        try:
            reg.check_presence()
        finally:
            app.Registry._host_up = original
        self.assertFalse(reg._entry(mac)["present"])
        self.assertFalse(next(r for r in reg.entries() if r["mac"] == mac)["online"])


    def test_parse_subnet_rejects_large_range_before_expansion(self):
        with self.assertRaisesRegex(ValueError, "subnet too large"):
            app.parse_subnet("10.0.0.0/8")

    def test_parse_subnet_rejects_ipv6(self):
        with self.assertRaisesRegex(ValueError, "IPv4"):
            app.parse_subnet("2001:db8::/64")

    def test_parse_subnet_accepts_small_point_to_point_networks(self):
        self.assertEqual(app.parse_subnet("192.0.2.0/31"),
                         ["192.0.2.0", "192.0.2.1"])
        self.assertEqual(app.parse_subnet("192.0.2.7/32"), ["192.0.2.7"])

    def test_load_config_rejects_invalid_values(self):
        invalid_configs = [
            {"WIZ_WORKERS": "0"},
            {"WIZ_WORKERS": "257"},
            {"WIZ_PORT": "70000"},
            {"WIZ_OP_TIMEOUT": "nan"},
            {"WIZ_PRESENCE_TIMEOUT": "-1"},
            {"WIZ_HOSTS": "not-an-ip"},
            {"WIZ_HOSTS": "2001:db8::1"},
            {"WIZ_LOG_LEVEL": "VERBOSE"},
        ]
        for config in invalid_configs:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    app.load_config(config)

    def test_load_config_normalizes_and_deduplicates_extra_hosts(self):
        config = app.load_config({
            "WIZ_SUBNET": "192.0.2.0/31",
            "WIZ_HOSTS": "192.0.2.1, 198.51.100.7,198.51.100.7",
            "WIZ_PORT": "8080",
        })
        self.assertEqual(config["hosts"],
                         ["192.0.2.0", "192.0.2.1", "198.51.100.7"])
        self.assertEqual(config["port"], 8080)

    def test_invalid_state_root_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as state_file:
                state_file.write("[]")
            reg = app.Registry(hosts=[], scan_timeout=0.01, op_timeout=0.01,
                               workers=1, interval=60, ttl=180, state_file=path)
            self.assertEqual(reg.entries(), [])

    def test_malformed_state_fields_are_skipped_or_defaulted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as state_file:
                json.dump({
                    "bulbs": {
                        "aabbccddeeff": {
                            "ip": "not-an-ip",
                            "first_seen": "not-a-number",
                        }
                    },
                    "categories": [{"id": "c1", "name": "  "}, {"id": "c2", "name": "Office"}],
                }, state_file)
            reg = app.Registry(hosts=[], scan_timeout=0.01, op_timeout=0.01,
                               workers=1, interval=60, ttl=180, state_file=path)
            entry = reg._entry("aabbccddeeff")
            self.assertIsNone(entry["ip"])
            self.assertGreater(entry["first_seen"], 0)
            self.assertEqual(reg.categories, [{"id": "c2", "name": "Office"}])

    def test_concurrent_category_saves_leave_valid_complete_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            reg = app.Registry(hosts=[], scan_timeout=0.01, op_timeout=0.01,
                               workers=1, interval=60, ttl=180, state_file=path)
            errors = []

            def add(index):
                try:
                    reg.add_category(f"Room {index}")
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=add, args=(i,)) for i in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            with open(path) as state_file:
                saved = json.load(state_file)
            self.assertEqual(len(saved["categories"]), 12)
            self.assertEqual({c["name"] for c in saved["categories"]},
                             {f"Room {i}" for i in range(12)})

    def test_remove_waits_for_active_operation_and_prevents_revival(self):
        reg, mac = self._registry_with()
        operation_started = threading.Event()
        allow_operation_to_finish = threading.Event()
        removal_started = threading.Event()
        removal_finished = threading.Event()
        original_run = reg._run

        def blocked_run(requested_mac, fn):
            operation_started.set()
            self.assertTrue(allow_operation_to_finish.wait(2))
            return "127.0.0.1", {"result": {"state": True}}

        reg._run = blocked_run
        operation_errors = []
        removal_errors = []

        def run_status():
            try:
                reg.status(mac)
            except Exception as exc:
                operation_errors.append(exc)

        def remove():
            removal_started.set()
            try:
                reg.remove_bulb(mac)
            except Exception as exc:
                removal_errors.append(exc)
            finally:
                removal_finished.set()

        operation = threading.Thread(target=run_status)
        operation.start()
        self.assertTrue(operation_started.wait(2))

        remover = threading.Thread(target=remove)
        remover.start()
        self.assertTrue(removal_started.wait(2))
        time.sleep(0.05)  # removal must be waiting for the operation lock
        self.assertFalse(removal_finished.is_set())

        allow_operation_to_finish.set()
        operation.join(2)
        remover.join(2)
        reg._run = original_run

        self.assertFalse(operation.is_alive())
        self.assertFalse(remover.is_alive())
        self.assertEqual(operation_errors, [])
        self.assertEqual(removal_errors, [])
        self.assertIsNone(reg._entry(mac))

        # A stale completion callback must not recreate the removed registry row.
        reg._remember(mac, "127.0.0.1", {"state": True})
        self.assertIsNone(reg._entry(mac))
        # A later explicit request retains the pre-existing discovery path;
        # without a discoverable bulb it reports the normal unreachable result.
        with self.assertRaises(app.Unreachable):
            reg.status(mac)


if __name__ == "__main__":
    unittest.main(verbosity=2)
