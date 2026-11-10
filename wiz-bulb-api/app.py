#!/usr/bin/env python3
"""WiZ bulb reachability/control API + web UI.

A tiny LAN service that finds WiZ bulbs by MAC (UDP 38899 getDevInfo/getPilot)
and exposes them over HTTP, so a Shelly mJS script can use them. Shelly mJS
cannot send raw UDP, and WiZ bulbs cannot be given static IPs, so this service
keeps the MAC -> IP mapping itself.

Stdlib only. No dependencies.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

WIZ_PORT = 38899
LOG = logging.getLogger("wiz")
HERE = Path(__file__).resolve().parent

# Forwarded to setPilot when used through /set.
SETTABLE = {
    "state", "dimming", "temp", "r", "g", "b", "c", "w",
    "sceneId", "speed", "ratio",
}

# Reported back by /state. "Color" here means everything the pilot knows about
# the current light output: RGB channels, cold/warm white, colour temperature
# and scene.
REPORTED = (
    "state", "dimming", "temp", "r", "g", "b", "c", "w", "sceneId", "speed",
)


def normalize_mac(value: str) -> str | None:
    """Accept 98:77:d5:0a:ba:e0, 98-77-D5-0A-BA-E0 or 9877d50abae0."""
    cleaned = re.sub(r"[^0-9a-fA-F]", "", value or "")
    return cleaned.lower() if len(cleaned) == 12 else None


def parse_subnet(value: str) -> list[str]:
    net = ipaddress.ip_network(value, strict=False)
    hosts = [str(h) for h in net.hosts()]
    if len(hosts) > 4096:
        raise ValueError("subnet too large, use a /20 or smaller")
    return hosts


def wiz_call(ip: str, payload: dict, timeout: float) -> dict:
    """Send one JSON datagram to a bulb and return the parsed reply."""
    msg = dict(payload)
    msg.setdefault("id", int(time.time() * 1000) % 1_000_000)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(json.dumps(msg).encode(), (ip, WIZ_PORT))
        data, _ = sock.recvfrom(8192)
    reply = json.loads(data)
    if "error" in reply:
        raise ValueError(reply["error"].get("message", "bulb error"))
    return reply


class Unreachable(Exception):
    pass


class Registry:
    """MAC -> IP map plus the last known pilot fields, refreshed by a scanner."""

    def __init__(self, hosts, scan_timeout, op_timeout, workers, interval, ttl):
        self.hosts = hosts
        self.scan_timeout = scan_timeout
        self.op_timeout = op_timeout
        self.workers = workers
        self.interval = interval
        self.ttl = ttl
        self.min_scan_gap = 3.0     # collapse bursts of failures into one scan

        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._by_mac: dict[str, dict] = {}
        self._last_scan = 0.0

    # ---- scanning ---------------------------------------------------------

    def _probe(self, ip: str) -> tuple[str, str, str | None] | None:
        try:
            res = wiz_call(ip, {"method": "getDevInfo"}, self.scan_timeout).get("result", {})
            mac = normalize_mac(res.get("mac", ""))
            if mac:
                return mac, ip, res.get("moduleName")
        except (OSError, ValueError):
            pass
        return None

    def scan(self, force: bool = False) -> None:
        """Refresh the MAC map. Forced scans are rate-limited too, so a burst
        of failed requests cannot turn into a scan storm."""
        gap = self.min_scan_gap if force else self.interval
        if time.monotonic() - self._last_scan < gap:
            return
        with self._scan_lock:
            if time.monotonic() - self._last_scan < gap:
                return
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                found = [r for r in pool.map(self._probe, self.hosts) if r]
            self._last_scan = time.monotonic()

            online = {mac for mac, _, _ in found}
            with self._lock:
                for mac, ip, model in found:
                    entry = self._by_mac.get(mac, {})
                    entry.update(ip=ip, model=model or entry.get("model"),
                                 last_seen=self._last_scan, online=True)
                    self._by_mac[mac] = entry
                for mac, entry in self._by_mac.items():
                    if mac not in online:
                        entry["online"] = False

            LOG.info("scan: %d/%d bulbs online in %.1fs",
                     len(found), len(self.hosts), time.monotonic() - started)

    def refresh_loop(self) -> None:
        while True:
            try:
                self.scan(force=True)
            except Exception:
                LOG.exception("background scan failed")
            time.sleep(self.interval)

    # ---- lookups ----------------------------------------------------------

    def _entry(self, mac: str) -> dict | None:
        with self._lock:
            entry = self._by_mac.get(mac)
            return dict(entry) if entry else None

    def entries(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            rows = []
            for mac, entry in self._by_mac.items():
                row = {k: v for k, v in entry.items() if k != "last_seen"}
                row["mac"] = mac
                row["age_s"] = round(now - entry.get("last_seen", now))
                rows.append(row)
        return sorted(rows, key=lambda e: ipaddress.ip_address(e["ip"]))

    def resolve(self, mac: str, force: bool = False) -> str | None:
        entry = self._entry(mac)
        if force or not entry or not entry.get("ip"):
            self.scan(force=force)
            entry = self._entry(mac)
            return entry["ip"] if entry and entry.get("online") else None

        fresh = entry.get("online") and \
            time.monotonic() - entry.get("last_seen", 0) <= self.ttl
        if not fresh:
            self.scan(force=False)          # rate-limited, often a no-op
            entry = self._entry(mac) or entry
        # An offline entry keeps its last IP: worth one cheap direct try.
        return entry.get("ip")

    def mark_offline(self, mac: str) -> None:
        with self._lock:
            if mac in self._by_mac:
                self._by_mac[mac]["online"] = False

    def _lock_for(self, mac: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(mac, threading.Lock())

    def _remember(self, mac: str, ip: str, pilot: dict) -> None:
        with self._lock:
            entry = self._by_mac.setdefault(mac, {})
            entry.update(ip=ip, online=True, last_seen=time.monotonic())
            for key in REPORTED:
                if key in pilot:
                    entry[key] = pilot[key]

    def _view(self, mac: str, online: bool) -> dict:
        entry = self._entry(mac) or {}
        view = {"mac": mac, "ip": entry.get("ip"), "online": online,
                "model": entry.get("model")}
        for key in REPORTED:
            view[key] = entry.get(key)
        return view

    # ---- bulb operations --------------------------------------------------

    def _run(self, mac: str, fn):
        """Resolve, call, and retry once after a rescan if the IP went stale."""
        for attempt in (0, 1):
            ip = self.resolve(mac)
            if not ip:
                break
            try:
                return ip, fn(ip)
            except (OSError, ValueError) as exc:
                LOG.debug("%s at %s failed: %s", mac, ip, exc)
                self.mark_offline(mac)
                if attempt:
                    break
                before = self._last_scan
                self.scan(force=True)
                if self._last_scan == before:
                    break              # rate-limited; nothing new to try
                if not (self._entry(mac) or {}).get("online"):
                    break              # scan says the bulb is really gone
        raise Unreachable(mac)

    def status(self, mac: str) -> dict:
        with self._lock_for(mac):
            ip, reply = self._run(mac, lambda ip: wiz_call(
                ip, {"method": "getPilot"}, self.op_timeout))
            self._remember(mac, ip, reply.get("result", {}))
            return self._view(mac, True)

    def set_pilot(self, mac: str, payload: dict) -> dict:
        body = {k: v for k, v in payload.items() if k in SETTABLE}
        if not body:
            raise ValueError("nothing to set")
        with self._lock_for(mac):
            ip, reply = self._run(mac, lambda ip: wiz_call(
                ip, {"method": "setPilot", "params": body}, self.op_timeout))
            if not reply.get("result", {}).get("success", True):
                raise Unreachable(mac)
            self._remember(mac, ip, body)
            return {"mac": mac, "ip": ip, "online": True, "applied": body}

    def toggle(self, mac: str) -> dict:
        with self._lock_for(mac):
            entry = self._entry(mac) or {}
            state = entry.get("state")
            if state is None:
                ip, reply = self._run(mac, lambda ip: wiz_call(
                    ip, {"method": "getPilot"}, self.op_timeout))
                state = bool(reply.get("result", {}).get("state", False))
                self._remember(mac, ip, {"state": state})
            new = not bool(state)
            ip, _ = self._run(mac, lambda ip: wiz_call(
                ip, {"method": "setPilot", "params": {"state": new}}, self.op_timeout))
            self._remember(mac, ip, {"state": new})
            return {"mac": mac, "ip": ip, "online": True, "state": new}

    def live_entries(self) -> list[dict]:
        """Live getPilot for every known bulb, in parallel, then the cache."""
        with self._lock:
            macs = [m for m, e in self._by_mac.items() if e.get("ip")]
        if not macs:
            return []
        with ThreadPoolExecutor(max_workers=min(16, len(macs))) as pool:
            list(pool.map(self._safe_status, macs))
        return self.entries()

    def _safe_status(self, mac: str) -> None:
        try:
            self.status(mac)
        except Exception:
            pass


def make_handler(registry: Registry, token: str, ui: bytes):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "wiz-bulb-api/1.1"

        # -- helpers --------------------------------------------------------

        def _json(self, code: int, body) -> None:
            self._raw(code, json.dumps(body).encode(), "application/json")

        def _raw(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _fail(self, code: int, message: str) -> None:
            self._json(code, {"error": message})

        def log_message(self, fmt, *args):
            LOG.info("%s %s", self.address_string(), fmt % args)

        # -- routing --------------------------------------------------------

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method: str) -> None:
            path = urlparse(self.path).path
            # The UI must load without the token; its fetches will prompt for it.
            if token and path not in ("/", "/ui", "/favicon.ico"):
                if self.headers.get("Authorization", "") != f"Bearer {token}":
                    return self._fail(401, "unauthorized")
            try:
                self._dispatch(method, path)
            except BrokenPipeError:
                pass
            except Unreachable:
                self._json(503, {"mac": getattr(self, "_mac", None), "online": False})
            except ValueError as exc:
                self._fail(400, str(exc))
            except Exception:
                LOG.exception("request failed")
                self._fail(500, "internal error")

        def _dispatch(self, method: str, path: str) -> None:
            parts = [unquote(p) for p in path.strip("/").split("/") if p]

            if parts in ([], ["ui"]):
                return self._raw(200, ui, "text/html; charset=utf-8")
            if parts == ["favicon.ico"]:
                return self._raw(204, b"", "image/x-icon")
            if parts == ["healthz"]:
                return self._json(200, {"ok": True})
            if parts == ["bulbs"] and method == "GET":
                live = parse_qs(urlparse(self.path).query).get("live", ["0"])[0] not in ("0", "")
                return self._json(200, {"bulbs": registry.live_entries() if live else registry.entries()})
            if parts == ["api"]:
                return self._json(200, {
                    "service": "wiz-bulb-api",
                    "endpoints": [
                        "GET  /                 web UI",
                        "GET  /bulbs[?live=1]   known bulbs",
                        "GET  /bulb/<mac>       200 up / 503 down",
                        "GET  /bulb/<mac>/state state + colour",
                        "POST /bulb/<mac>/toggle",
                        "POST /bulb/<mac>/on",
                        "POST /bulb/<mac>/off",
                        "GET  /healthz",
                    ],
                })
            if parts[0] != "bulb" or len(parts) < 2:
                return self._fail(404, "not found")

            mac = normalize_mac(parts[1])
            if not mac:
                return self._fail(400, "invalid mac")
            self._mac = mac
            action = parts[2] if len(parts) > 2 else "status"

            if action in ("status", "state"):
                return self._json(200, registry.status(mac))
            if method != "POST":
                return self._fail(405, "use POST")
            if action == "toggle":
                return self._json(200, registry.toggle(mac))
            if action == "on":
                return self._json(200, registry.set_pilot(mac, {"state": True}))
            if action == "off":
                return self._json(200, registry.set_pilot(mac, {"state": False}))
            if action == "set":
                return self._json(200, registry.set_pilot(mac, self._body()))
            return self._fail(404, "unknown action")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                raise ValueError("body is not valid JSON") from None
            if not isinstance(result, dict):
                raise ValueError("body must be a JSON object")
            return result

    return Handler


def load_ui() -> bytes:
    path = HERE / "ui.html"
    if path.exists():
        return path.read_bytes()
    return b"<!doctype html><h1>wiz-bulb-api</h1><p>ui.html is missing.</p>"


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("WIZ_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    subnet = os.environ.get("WIZ_SUBNET", "192.168.1.0/24")
    hosts = parse_subnet(subnet)
    extra = [h.strip() for h in os.environ.get("WIZ_HOSTS", "").split(",") if h.strip()]
    hosts = sorted(set(hosts + extra), key=ipaddress.ip_address)

    registry = Registry(
        hosts=hosts,
        scan_timeout=float(os.environ.get("WIZ_SCAN_TIMEOUT", "0.3")),
        op_timeout=float(os.environ.get("WIZ_OP_TIMEOUT", "1.5")),
        workers=int(os.environ.get("WIZ_WORKERS", "64")),
        interval=float(os.environ.get("WIZ_SCAN_INTERVAL", "60")),
        ttl=float(os.environ.get("WIZ_CACHE_TTL", "180")),
    )

    port = int(os.environ.get("WIZ_PORT", "8077"))
    token = os.environ.get("WIZ_API_TOKEN", "")
    server = ThreadingHTTPServer(("0.0.0.0", port),
                                 make_handler(registry, token, load_ui()))
    server.daemon_threads = True

    threading.Thread(target=registry.refresh_loop, daemon=True).start()
    LOG.info("listening on :%d, scanning %d hosts (%s)", port, len(hosts), subnet)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
