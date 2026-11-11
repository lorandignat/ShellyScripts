#!/usr/bin/env python3
"""WiZ bulb reachability/control API + web UI.

A tiny LAN service that finds WiZ bulbs by MAC (UDP 38899 getDevInfo/getPilot)
and exposes them over HTTP, so a Shelly mJS script can use them. Shelly mJS
cannot send raw UDP, and WiZ bulbs cannot be given static IPs, so this service
keeps the MAC -> IP mapping itself.

Stdlib only. No dependencies.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
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
    """Return IPv4 hosts in a subnet, rejecting oversized ranges up front."""
    try:
        net = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise ValueError(f"invalid WIZ_SUBNET: {exc}") from None
    if net.version != 4:
        raise ValueError("WIZ_SUBNET must be an IPv4 network")

    # /31 and /32 networks expose all addresses as hosts; other IPv4 networks
    # reserve the network and broadcast addresses. Check before materializing.
    host_count = net.num_addresses if net.prefixlen >= 31 else net.num_addresses - 2
    if host_count > 4096:
        raise ValueError("subnet too large, use a /20 or smaller")
    return [str(host) for host in net.hosts()]


def load_config(environ: dict[str, str] | None = None) -> dict:
    """Read and validate environment configuration before starting workers."""
    env = os.environ if environ is None else environ

    def positive_float(name: str, default: str, *, allow_zero: bool = False) -> float:
        raw = env.get(name, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        valid = value >= 0 if allow_zero else value > 0
        if not valid or not value < float("inf"):
            qualifier = "zero or greater" if allow_zero else "greater than zero"
            raise ValueError(f"{name} must be {qualifier}")
        return value

    def positive_int(name: str, default: str, minimum: int = 1,
                     maximum: int | None = None) -> int:
        raw = env.get(name, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer") from None
        if value < minimum or (maximum is not None and value > maximum):
            bounds = f"between {minimum} and {maximum}" if maximum else f"at least {minimum}"
            raise ValueError(f"{name} must be {bounds}")
        return value

    log_level = env.get("WIZ_LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("WIZ_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

    subnet = env.get("WIZ_SUBNET", "192.168.1.0/24")
    hosts = parse_subnet(subnet)
    extra_hosts = []
    for raw_host in env.get("WIZ_HOSTS", "").split(","):
        raw_host = raw_host.strip()
        if not raw_host:
            continue
        try:
            address = ipaddress.ip_address(raw_host)
        except ValueError:
            raise ValueError(f"invalid address in WIZ_HOSTS: {raw_host}") from None
        if address.version != 4:
            raise ValueError(f"WIZ_HOSTS only accepts IPv4 addresses: {raw_host}")
        extra_hosts.append(str(address))

    hosts = sorted(set(hosts + extra_hosts), key=ipaddress.ip_address)
    return {
        "subnet": subnet,
        "hosts": hosts,
        "log_level": log_level,
        "scan_timeout": positive_float("WIZ_SCAN_TIMEOUT", "1.5"),
        "op_timeout": positive_float("WIZ_OP_TIMEOUT", "2.5"),
        "workers": positive_int("WIZ_WORKERS", "64", minimum=1, maximum=256),
        "interval": positive_float("WIZ_SCAN_INTERVAL", "120"),
        "ttl": positive_float("WIZ_CACHE_TTL", "300"),
        "probe_timeout": positive_float("WIZ_PROBE_TIMEOUT", "2.0"),
        "response_grace": positive_float("WIZ_RESPONSE_GRACE", "60", allow_zero=True),
        "presence_interval": positive_float("WIZ_PRESENCE_INTERVAL", "60"),
        "presence_timeout": positive_float("WIZ_PRESENCE_TIMEOUT", "1.5"),
        "status_interval": positive_float("WIZ_STATUS_INTERVAL", "30"),
        "probe_min_gap": positive_float("WIZ_PROBE_MIN_GAP", "20", allow_zero=True),
        "state_file": env.get("WIZ_STATE_FILE", "/data/state.json"),
        "port": positive_int("WIZ_PORT", "8077", minimum=1, maximum=65535),
        "token": env.get("WIZ_API_TOKEN", ""),
    }


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
    """Raised when a bulb cannot be reached or does not accept an operation."""


class Registry:
    """MAC -> IP map plus the last known pilot fields, refreshed by a scanner."""

    def __init__(
        self,
        hosts,
        scan_timeout,
        op_timeout,
        workers,
        interval,
        ttl,
        probe_timeout=2.0,
        response_grace=60.0,
        presence_interval=60.0,
        presence_timeout=1.5,
        status_interval=30.0,
        probe_min_gap=20.0,
        state_file: str | None = None,
    ):
        """Initialize caches, synchronization primitives, and worker pools."""
        self.hosts = hosts
        self.scan_timeout = scan_timeout
        self.op_timeout = op_timeout
        self.workers = workers
        self.interval = interval
        self.ttl = ttl
        self.probe_timeout = probe_timeout
        self.response_grace = response_grace
        self.presence_interval = presence_interval
        self.presence_timeout = presence_timeout
        self.status_interval = status_interval
        self.probe_min_gap = probe_min_gap
        self.categories: list[dict] = []
        self._state_save_failed = False
        self.state_file = self._choose_state_file(state_file)
        self.min_scan_gap = 3.0     # collapse bursts of failures into one scan

        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._presence_lock = threading.Lock()
        # Serialize snapshots and writes so concurrent saves cannot race on the temp file.
        self._state_save_lock = threading.Lock()
        # Keep pools alive across requests. Creating a fresh set of threads
        # for each browser poll previously exhausted the container PID limit.
        self._probe_pool = ThreadPoolExecutor(
            max_workers=16,
            thread_name_prefix="probe",
        )
        self._presence_pool = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="presence",
        )
        self._scan_pool = ThreadPoolExecutor(
            max_workers=min(32, workers),
            thread_name_prefix="scan",
        )
        self._locks: dict[str, threading.Lock] = {}
        self._by_mac: dict[str, dict] = {}
        self._last_scan = 0.0
        self._load_state()

    # ---- persistent state -------------------------------------------------

    @staticmethod
    def _choose_state_file(path: str | None) -> Path | None:
        if not path:
            return None
        wanted = Path(path)
        try:
            wanted.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if os.access(wanted.parent, os.W_OK):
            return wanted
        fallback = Path("/tmp/wiz-bulb-state.json")
        if os.access(fallback.parent, os.W_OK):
            LOG.warning("cannot write to %s; using %s instead", path, fallback)
            return fallback
        LOG.warning("state directory %s is not writable; remembered bulbs and "
                    "categories will not survive a restart. Mount a volume at "
                    "/data to keep them.", wanted.parent)
        return wanted

    def _load_state(self) -> None:
        path = self.state_file
        if not path or not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            LOG.warning("ignoring unreadable state file %s", path)
            return
        # Valid JSON can still have the wrong structure at the root.
        if not isinstance(data, dict):
            LOG.warning("ignoring state file with invalid root structure: %s", path)
            return

        now = time.time()
        bulbs = data.get("bulbs")
        if isinstance(bulbs, dict):
            for saved_mac, saved in bulbs.items():
                mac = normalize_mac(saved_mac)
                if not mac or not isinstance(saved, dict):
                    continue
                try:
                    first_seen = float(saved.get("first_seen") or now)
                except (TypeError, ValueError):
                    first_seen = now
                ip = saved.get("ip")
                if ip is not None:
                    try:
                        parsed_ip = ipaddress.ip_address(ip)
                    except (TypeError, ValueError):
                        ip = None
                    else:
                        ip = str(parsed_ip) if parsed_ip.version == 4 else None
                self._by_mac[mac] = {
                    "ip": ip,
                    "model": saved.get("model"),
                    "category": saved.get("category"),
                    "first_seen": first_seen,
                    "present": False,
                    "online": False,
                    "last_probe": 0.0,
                }
        cats = data.get("categories")
        if isinstance(cats, list):
            for cat in cats:
                if (isinstance(cat, dict) and cat.get("id") and
                        isinstance(cat.get("name"), str) and cat["name"].strip()):
                    self.categories.append(
                        {"id": str(cat["id"]), "name": cat["name"]})
        LOG.info("loaded %d bulbs and %d categories from %s",
                 len(self._by_mac), len(self.categories), path)

    def save_state(self) -> None:
        path = self.state_file
        if not path:
            return

        # The save lock covers snapshot creation and the atomic file swap.
        # Copy mutable structures under the registry lock before serialization.
        with self._state_save_lock:
            with self._lock:
                bulbs = {}
                for mac, entry in self._by_mac.items():
                    bulbs[mac] = {
                        "ip": entry.get("ip"),
                        "model": entry.get("model"),
                        "category": entry.get("category"),
                        "first_seen": entry.get("first_seen"),
                    }
                categories = [dict(category) for category in self.categories]
                payload = {"version": 1, "categories": categories, "bulbs": bulbs}

            tmp = path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(payload, indent=1))
                tmp.replace(path)
                self._state_save_failed = False
            except (OSError, TypeError, ValueError) as exc:
                if not self._state_save_failed:
                    LOG.warning("could not save state to %s: %s", path, exc)
                    self._state_save_failed = True

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
            found = [r for r in self._scan_pool.map(self._probe, self.hosts) if r]
            self._last_scan = time.monotonic()

            now = self._last_scan
            changed = False
            with self._lock:
                for mac, ip, model in found:
                    entry = self._by_mac.get(mac, {})
                    if not entry:
                        changed = True
                        entry["first_seen"] = time.time()
                    if entry.get("ip") != ip or (model and entry.get("model") != model):
                        changed = True
                    entry.update(ip=ip, model=model or entry.get("model"),
                                 last_seen=now, present=True, last_present=now,
                                 last_response=now, online=True)
                    self._by_mac[mac] = entry
                # Absence is decided by the TCP presence sweep, not by one
                # UDP sweep: a lost datagram must not make a bulb disappear.
            if changed:
                self.save_state()

            LOG.info("scan: %d/%d bulbs online in %.1fs",
                     len(found), len(self.hosts), time.monotonic() - started)

    def refresh_loop(self) -> None:
        while True:
            try:
                self.scan(force=True)
            except Exception:
                LOG.exception("background scan failed")
            time.sleep(self.interval)

    # ---- presence (LAN, not application level) ----------------------------

    @staticmethod
    def _host_up(ip: str, timeout: float) -> bool:
        """True when something at `ip` answers TCP with a refusal or a connect.

        WiZ bulbs refuse TCP 38899, which is enough to prove the device is on
        the LAN even when it does not answer a UDP getPilot.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            try:
                rc = sock.connect_ex((ip, WIZ_PORT))
            except OSError:
                return False
        return rc in (0, errno.ECONNREFUSED)

    def check_presence(self, blocking: bool = True) -> None:
        if not self._presence_lock.acquire(blocking=blocking):
            return
        try:
            with self._lock:
                targets = {
                    mac: entry["ip"]
                    for mac, entry in self._by_mac.items()
                    if entry.get("ip")
                }
            if not targets:
                return
            macs = list(targets)
            timeout = self.presence_timeout
            # Wi-Fi bulbs are slow: 19 parallel TCP connects and a 0.4 s
            # timeout made healthy bulbs look offline now and then. Keep the
            # sweep gentle and give a missed check one immediate retry.
            results = list(self._presence_pool.map(
                lambda m: (m, self._host_up(targets[m], timeout)), macs))
            retry = [m for m, up in results if not up]
            if retry:
                again = dict(self._presence_pool.map(
                    lambda m: (m, self._host_up(targets[m], timeout)), retry))
                results = [(m, again.get(m, up)) for m, up in results]

            now = time.monotonic()
            lost: list[str] = []
            with self._lock:
                for mac, up in results:
                    entry = self._by_mac.get(mac)
                    if not entry:
                        continue
                    if up:
                        entry["present"] = True
                        entry["last_present"] = now
                    else:
                        if entry.get("present"):
                            lost.append(mac)
                        entry["present"] = False
            if lost:
                # A failed presence check may mean DHCP changed the address. Refresh
        # the MAC-to-IP mapping once before declaring the bulb absent.
                self.scan(force=True)
        finally:
            self._presence_lock.release()

    def presence_loop(self) -> None:
        while True:
            try:
                self.check_presence()
            except Exception:
                LOG.exception("presence check failed")
            time.sleep(self.presence_interval)

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
                present = bool(entry.get("present"))
                last_ok = entry.get("last_response") or 0
                row = {k: v for k, v in entry.items()
                       if k not in ("last_seen", "last_present", "last_response",
                                     "last_probe")}
                row["mac"] = mac
                row["category"] = entry.get("category")
                row["online"] = present
                row["present"] = present
                row["responsive"] = present and (now - last_ok) <= self.response_grace
                row["last_response_s"] = (round(now - entry["last_response"])
                                          if entry.get("last_response") else None)
                row["age_s"] = round(now - entry.get("last_present", now))
                rows.append(row)
        # Keep cards in a stable order across disconnects and DHCP changes.
        return sorted(
            rows,
            key=lambda entry: (
                entry.get("first_seen") or 0,
                entry.get("mac") or "",
            ),
        )

    def resolve(self, mac: str, force: bool = False) -> str | None:
        entry = self._entry(mac)
        if force or not entry or not entry.get("ip"):
            self.scan(force=force)
            entry = self._entry(mac)
            return entry["ip"] if entry and entry.get("present") else None
        if entry.get("present"):
            return entry.get("ip")
        # Not seen by the last presence sweep: it may have just powered on, so
        # one cheap TCP check decides. Bulbs refuse the port instantly; a dead
        # address costs only `presence_timeout`.
        if self._host_up(entry["ip"], self.presence_timeout):
            now = time.monotonic()
            with self._lock:
                live = self._by_mac.get(mac)
                if live:
                    live["present"] = True
                    live["last_present"] = now
            return entry["ip"]
        return None

    def _lock_for(self, mac: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(mac, threading.Lock())

    def _remember(self, mac: str, ip: str, pilot: dict) -> None:
        now = time.monotonic()
        with self._lock:
            entry = self._by_mac.get(mac)
            if entry is None:
                return
            entry.update(ip=ip, online=True, present=True,
                         last_seen=now, last_present=now, last_response=now)
            # A pilot describes exactly one colour mode. Keeping fields from
            # the previous mode made /state report RGB and temperature at once.
            if any(k in pilot for k in ("r", "g", "b")):
                for key in ("temp", "c", "w"):
                    entry.pop(key, None)
            elif "temp" in pilot:
                for key in ("r", "g", "b", "c", "w"):
                    entry.pop(key, None)
            elif any(k in pilot for k in ("c", "w")):
                for key in ("r", "g", "b", "temp"):
                    entry.pop(key, None)
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

    # ---- categories / removal --------------------------------------------

    def category_counts(self) -> list[dict]:
        counts = {c["id"]: 0 for c in self.categories}
        with self._lock:
            for entry in self._by_mac.values():
                cid = entry.get("category")
                if cid in counts:
                    counts[cid] += 1
        return [{"id": c["id"], "name": c["name"], "count": counts[c["id"]]}
                for c in self.categories]

    def add_category(self, name) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        if len(name) > 40:
            raise ValueError("name is too long")
        with self._lock:
            if any(c["name"].lower() == name.lower() for c in self.categories):
                raise ValueError("category already exists")
            cat = {"id": uuid.uuid4().hex[:8], "name": name}
            self.categories.append(cat)
        self.save_state()
        return cat

    def remove_category(self, cid: str) -> dict:
        with self._lock:
            cat = next((c for c in self.categories if c["id"] == cid), None)
            if cat is None:
                raise KeyError(cid)
            self.categories.remove(cat)
            for entry in self._by_mac.values():
                if entry.get("category") == cid:
                    entry["category"] = None
        self.save_state()
        return cat

    def set_category(self, mac: str, cid: str | None) -> dict:
        cid = cid or None
        with self._lock:
            entry = self._by_mac.get(mac)
            if entry is None:
                raise KeyError(mac)
            if cid and not any(c["id"] == cid for c in self.categories):
                raise ValueError("unknown category")
            entry["category"] = cid
            present = bool(entry.get("present"))
        self.save_state()
        return self._view(mac, present)

    def remove_bulb(self, mac: str) -> str:
        # Wait for active work to finish. Keep the lock mapped so queued and
        # future operations for this MAC cannot end up using different locks.
        operation_lock = self._lock_for(mac)
        with operation_lock:
            with self._lock:
                if mac not in self._by_mac:
                    raise KeyError(mac)
                del self._by_mac[mac]
        self.save_state()
        return mac

    def _require_registered(self, mac: str) -> None:
        """Reject work queued behind a removal instead of reviving the entry."""
        with self._lock:
            if mac not in self._by_mac:
                raise KeyError(mac)

    # ---- bulb operations --------------------------------------------------

    def _run(self, mac: str, fn):
        """Run an operation, retrying once if a rescan finds a new IP."""
        for attempt in (0, 1):
            ip = self.resolve(mac)
            if not ip:
                break
            try:
                return ip, fn(ip)
            except (OSError, ValueError) as exc:
                LOG.debug("%s at %s failed: %s", mac, ip, exc)
                if attempt:
                    break
                before = self._last_scan
                self.scan(force=True)
                if self._last_scan == before:
                    break              # rate-limited; nothing new to try
                if not (self._entry(mac) or {}).get("present"):
                    break
        raise Unreachable(mac)

    def status(self, mac: str) -> dict:
        # Preserve discovery for previously unknown MACs. If this request
        # started with a known entry, however, do not revive it if removal
        # wins the race while the request waits for the operation lock.
        was_registered = self._entry(mac) is not None
        with self._lock_for(mac):
            if was_registered:
                self._require_registered(mac)
            ip, reply = self._run(mac, lambda ip: wiz_call(
                ip, {"method": "getPilot"}, self.op_timeout))
            self._remember(mac, ip, reply.get("result", {}))
            return self._view(mac, True)

    def set_pilot(self, mac: str, payload: dict) -> dict:
        body = {k: v for k, v in payload.items() if k in SETTABLE}
        if not body:
            raise ValueError("nothing to set")
        was_registered = self._entry(mac) is not None
        with self._lock_for(mac):
            if was_registered:
                self._require_registered(mac)
            ip, reply = self._run(mac, lambda ip: wiz_call(
                ip, {"method": "setPilot", "params": body}, self.op_timeout))
            if not reply.get("result", {}).get("success", True):
                raise Unreachable(mac)
            self._remember(mac, ip, body)
            return {"mac": mac, "ip": ip, "online": True, "applied": body}

    def toggle(self, mac: str) -> dict:
        was_registered = self._entry(mac) is not None
        with self._lock_for(mac):
            if was_registered:
                self._require_registered(mac)
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

    def probe(self, mac: str, force: bool = False) -> bool:
        """One cheap getPilot at the cached IP.

        Never scans and never touches LAN presence: presence is tracked by the
        TCP sweep, so a missed UDP reply only means "no response", not offline.

        At most one probe per bulb every `probe_min_gap` seconds, so a polling
        browser cannot turn into a stream of requests. The background status
        loop is the one that refreshes everyone, every `status_interval`.
        """
        entry = self._entry(mac)
        if not entry or not entry.get("ip"):
            return False

        now = time.monotonic()
        if not force and now - entry.get("last_probe", 0.0) < self.probe_min_gap:
            return bool(entry.get("present"))

        lock = self._lock_for(mac)
        if not lock.acquire(blocking=False):
            return bool(entry.get("present"))   # a toggle is using the bulb
        try:
            entry = self._entry(mac)
            if not entry or not entry.get("ip"):
                return False
            self._mark_probe(mac)
            reply = wiz_call(entry["ip"], {"method": "getPilot"}, self.probe_timeout)
        except (OSError, ValueError):
            return False
        else:
            self._remember(mac, entry["ip"], reply.get("result", {}))
            return True
        finally:
            lock.release()

    def _mark_probe(self, mac: str) -> None:
        with self._lock:
            if mac in self._by_mac:
                self._by_mac[mac]["last_probe"] = time.monotonic()

    def status_loop(self) -> None:
        """Refresh every reachable bulb on a slow, predictable cadence."""
        while True:
            try:
                with self._lock:
                    macs = [
                        mac
                        for mac, entry in self._by_mac.items()
                        if entry.get("ip") and entry.get("present")
                    ]
                if macs:
                    list(self._probe_pool.map(
                        lambda m: self.probe(m, force=True), macs))
            except Exception:
                LOG.exception("status check failed")
            time.sleep(self.status_interval)

    def live_entries(self) -> list[dict]:
        """Probe the bulbs that are currently on the LAN, then return the cache.

        Presence is left to the background TCP sweep: doing it inline would
        make the request wait for a TCP timeout and was the last source of
        sluggish page loads.
        """
        with self._lock:
            macs = [m for m, e in self._by_mac.items()
                    if e.get("ip") and e.get("present")]
        if macs:
            list(self._probe_pool.map(self.probe, macs))
        return self.entries()


def make_handler(registry: Registry, token: str, ui: bytes):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "wiz-bulb-api/1.12"

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

        def do_DELETE(self):
            self._route("DELETE")

        def _route(self, method: str) -> None:
            path = urlparse(self.path).path
            # Serve the UI without authentication; protect its API requests.
            if token and path not in ("/", "/ui", "/favicon.ico"):
                if self.headers.get("Authorization", "") != f"Bearer {token}":
                    return self._fail(401, "unauthorized")
            try:
                self._dispatch(method, path)
            except BrokenPipeError:
                pass
            except Unreachable:
                self._json(503, {"mac": getattr(self, "_mac", None), "online": False})
            except KeyError:
                self._fail(404, "not found")
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
                bulbs = registry.live_entries() if live else registry.entries()
                # Return bulbs and room counts together to avoid a second UI request.
                return self._json(200, {"bulbs": bulbs,
                                        "categories": registry.category_counts()})
            if parts == ["api"]:
                return self._json(200, {
                    "service": "wiz-bulb-api",
                    "endpoints": [
                        "GET    /                       web UI",
                        "GET    /bulbs[?live=1]         known bulbs",
                        "GET    /bulb/<mac>             200 up / 503 down",
                        "GET    /bulb/<mac>/state       state + colour",
                        "POST   /bulb/<mac>/toggle",
                        "POST   /bulb/<mac>/on",
                        "POST   /bulb/<mac>/off",
                        "POST   /bulb/<mac>/set         setPilot body",
                        "POST   /bulb/<mac>/category    {\"category\": <id|null>}",
                        "DELETE /bulb/<mac>             forget a bulb",
                        "GET    /categories             list with counts",
                        "POST   /categories             {\"name\": ...}",
                        "DELETE /categories/<id>",
                        "GET    /healthz",
                    ],
                })
            if parts == ["categories"]:
                if method == "GET":
                    return self._json(200, {"categories": registry.category_counts()})
                if method == "POST":
                    cat = registry.add_category(self._body().get("name"))
                    return self._json(201, cat)
                return self._fail(405, "use GET or POST")
            if parts[0] == "categories" and len(parts) >= 2:
                if method != "DELETE":
                    return self._fail(405, "use DELETE")
                return self._json(200, {"removed": registry.remove_category(parts[1])})
            if parts[0] != "bulb" or len(parts) < 2:
                return self._fail(404, "not found")

            mac = normalize_mac(parts[1])
            if not mac:
                return self._fail(400, "invalid mac")
            self._mac = mac
            action = parts[2] if len(parts) > 2 else "status"

            if method == "DELETE" and action == "status":
                return self._json(200, {"removed": registry.remove_bulb(mac)})
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
            if action == "category":
                body = self._body()
                return self._json(200, registry.set_category(
                    mac, body.get("category")))
            return self._fail(404, "unknown action")

        def _body(self) -> dict:
            """Read and validate a JSON object from the request body."""
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
    """Validate configuration, start background workers, and serve HTTP requests."""
    # Fail fast before creating pools or starting background threads.
    config = load_config()
    logging.basicConfig(
        level=config["log_level"],
        format="%(asctime)s %(levelname)s %(message)s",
    )
    registry = Registry(
        hosts=config["hosts"],
        scan_timeout=config["scan_timeout"],
        op_timeout=config["op_timeout"],
        workers=config["workers"],
        interval=config["interval"],
        ttl=config["ttl"],
        probe_timeout=config["probe_timeout"],
        response_grace=config["response_grace"],
        presence_interval=config["presence_interval"],
        presence_timeout=config["presence_timeout"],
        status_interval=config["status_interval"],
        probe_min_gap=config["probe_min_gap"],
        state_file=config["state_file"],
    )

    server = ThreadingHTTPServer(
        ("0.0.0.0", config["port"]),
        make_handler(registry, config["token"], load_ui()),
    )
    server.daemon_threads = True

    threading.Thread(target=registry.refresh_loop, daemon=True).start()
    threading.Thread(target=registry.presence_loop, daemon=True).start()
    threading.Thread(target=registry.status_loop, daemon=True).start()
    LOG.info("listening on :%d, scanning %d hosts (%s)",
             config["port"], len(config["hosts"]), config["subnet"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
