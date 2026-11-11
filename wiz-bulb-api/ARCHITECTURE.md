# WiZ Bulb Service — Architecture

## Purpose

This service discovers WiZ bulbs on a local IPv4 network, keeps a MAC-address-to-IP-address registry, and exposes bulb control and status over HTTP.

The HTTP API is intended for clients such as a browser UI or a Shelly mJS script. Those clients do not need to implement the WiZ UDP protocol themselves.

The application uses only the Python standard library.

## High-level structure

```text
Browser UI / Shelly mJS
          |
          | HTTP
          v
   ThreadingHTTPServer
          |
          v
      API Handler
          |
          v
       Registry
       /   |   \
      /    |    \
 Scan   Presence  Status
 pool     pool     pool
  |        |        |
 UDP      TCP      UDP
  |        |        |
  +--------+--------+
           |
        WiZ bulbs
```

`app.py` contains the network protocol helpers, the `Registry`, the HTTP handler, configuration parsing, and process startup. `ui.html` is the browser interface. `test_app.py` contains API and unit tests.

## Main components

### HTTP server and handler

`main()` creates a `ThreadingHTTPServer` bound to `0.0.0.0` and builds its handler with:

- The shared `Registry` instance.
- The configured optional API token.
- The UI HTML loaded from `ui.html`.

The handler translates HTTP routes into registry operations and returns JSON responses. It also maps expected errors to HTTP status codes:

- Invalid request data: `400`.
- Missing registry item: `404`.
- Unreachable bulb: `503`.
- Unexpected server error: `500`.

The UI routes (`/`, `/ui`, and `/favicon.ico`) are served without the optional API token. When `WIZ_API_TOKEN` is non-empty, other routes require `Authorization: Bearer <token>`. When it is empty, API authentication is disabled. This is the existing deployment behavior; the service assumes it is being used on a trusted LAN.

### Registry

`Registry` is the central in-memory model. It maintains:

- `_by_mac`: bulb metadata and the most recently known IP address, keyed by normalized MAC address.
- `categories`: user-defined groups such as rooms.
- Per-bulb operation locks, preventing simultaneous control/status operations for the same bulb.
- Shared locks for registry data, scans, presence sweeps, and state-file writes.
- Long-lived thread pools for scanning, presence checks, and status probes.

The registry is the boundary between HTTP requests and WiZ network communication.

### WiZ protocol helper

`wiz_call()` sends one JSON UDP datagram to port `38899`, waits for a reply, parses the JSON response, and raises an error if the bulb reports a protocol error.

Discovery uses `getDevInfo`. Status reads use `getPilot`. Control operations use `setPilot`.

## Device discovery and lifecycle

### Startup

1. `main()` reads and validates environment configuration.
2. `parse_subnet()` parses the configured IPv4 subnet and enumerates its usable host addresses only after checking the host count.
3. Additional IPv4 addresses from `WIZ_HOSTS` are validated, normalized, and combined with the subnet addresses.
4. `Registry` loads persisted bulb and category metadata, if a state file is available.
5. The HTTP server is created.
6. Three daemon background loops start: scanning, LAN presence checks, and status probes.
7. The HTTP server begins accepting requests.

### Discovery scan

A scan sends `getDevInfo` UDP requests to the configured host list using the scan thread pool.

A successful response supplies a MAC address and may supply a model name. The MAC address is normalized and used as the stable registry key. The responding IP becomes the current address for that MAC.

A scan does not mark a bulb absent merely because it did not answer UDP. UDP packet loss or a slow response is not sufficient evidence that a device has left the LAN.

Scans are serialized and rate-limited. Forced scans are also subject to a minimum gap, preventing a burst of failed operations from triggering repeated full-subnet scans.

### Presence detection

The presence loop checks known IP addresses using TCP connections to port `38899`.

WiZ bulbs may refuse a TCP connection on that port. A connection refusal is therefore treated as evidence that the host is reachable; a successful connection also counts as reachable. A timeout or other connection failure is treated as a missed presence check.

Each failed check is retried once immediately. If a bulb that was previously present is still not reachable, the registry requests a rate-limited discovery scan in case DHCP has changed its IP address.

Presence is a LAN-level signal. It does not prove that a bulb will answer an application-level UDP request.

### Status and responsiveness

The status loop periodically sends `getPilot` requests to bulbs currently marked present. It uses the probe thread pool and updates cached pilot fields when a bulb replies.

The registry exposes separate concepts:

- `present` / `online`: the last LAN presence sweep considers the device reachable.
- `responsive`: the device is present and has responded to a `getPilot` or discovery request within `WIZ_RESPONSE_GRACE`.

A bulb can therefore be present but not recently responsive. A single missed UDP probe does not mark it offline.

The browser's live-list request can also request cached bulbs to be probed. Per-bulb probe rate limiting prevents repeated browser polling from generating an unbounded stream of UDP requests.

### Toggle behavior

Toggle uses the state already held in the registry. If no state has ever been cached, it obtains the state from the bulb first. Otherwise, it calculates the opposite of the cached state and sends `setPilot`.

The regular status polling loop refreshes that cached state. Toggle deliberately does not perform an unconditional `getPilot` before every action, avoiding an additional network round trip and the associated UI delay.

## Concurrency model

The application uses several layers of synchronization:

- The registry lock protects `_by_mac` and shared category data.
- The scan lock ensures only one full discovery scan runs at a time.
- The presence lock prevents overlapping LAN presence sweeps.
- A per-MAC lock serializes operations for an individual bulb while allowing different bulbs to be operated independently.
- Dedicated, long-lived `ThreadPoolExecutor` instances bound the number of concurrent scan, presence, and probe tasks.
- A state-save lock serializes state snapshots and writes, preventing concurrent saves from competing for the same temporary file.

Bulb removal takes the corresponding per-MAC operation lock before deleting the registry entry. This lets an operation already in progress finish before removal. The lock remains associated with the MAC so that queued and later operations cannot split across different lock objects. A request that began while the bulb was registered also checks that it has not been removed while waiting for the lock.

A stale `_remember()` call does not create a new registry entry. New discovery is still allowed to add a device, and requests for previously unknown MAC addresses retain the normal discovery path.

## Cached state and persistence

The optional state file stores durable metadata:

- MAC address.
- Last known IP address.
- Model name.
- Category assignment.
- First-seen timestamp.

Transient fields such as current LAN presence, recent UDP response time, and last probe time are rebuilt while the service runs.

On startup, malformed or structurally invalid state data is ignored or skipped rather than preventing the service from starting. Invalid stored IP addresses are discarded.

State saves take a snapshot while holding the registry lock. Mutable category records are copied into the snapshot. The save lock covers both snapshot creation and the file write. The snapshot is written to a temporary file and then atomically replaces the previous state file.

If the configured state directory is not writable, the application attempts its existing `/tmp/wiz-bulb-state.json` fallback. A temporary-directory fallback does not survive container recreation or host reboot.

## HTTP API

The principal routes are:

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/` or `/ui` | Serve the browser UI |
| `GET` | `/api` | List API routes |
| `GET` | `/healthz` | Service health check |
| `GET` | `/bulbs` | Return known bulbs and categories |
| `GET` | `/bulbs?live=1` | Probe eligible known bulbs, then return the list |
| `GET` | `/bulb/<mac>` | Read bulb status |
| `GET` | `/bulb/<mac>/state` | Read bulb status and pilot fields |
| `POST` | `/bulb/<mac>/toggle` | Toggle cached on/off state |
| `POST` | `/bulb/<mac>/on` | Turn on |
| `POST` | `/bulb/<mac>/off` | Turn off |
| `POST` | `/bulb/<mac>/set` | Apply supported `setPilot` fields |
| `POST` | `/bulb/<mac>/category` | Assign or clear a category |
| `DELETE` | `/bulb/<mac>` | Forget a bulb from the registry |
| `GET` | `/categories` | List categories and counts |
| `POST` | `/categories` | Create a category |
| `DELETE` | `/categories/<id>` | Remove a category |

The `/set` endpoint forwards only the keys in the `SETTABLE` allowlist. Unknown keys are ignored.

## Configuration

Configuration is read from environment variables and validated before thread pools or background threads are started.

| Variable | Default | Validation / purpose |
|---|---:|---|
| `WIZ_SUBNET` | `192.168.1.0/24` | IPv4 subnet; no more than 4096 usable hosts |
| `WIZ_HOSTS` | empty | Comma-separated additional IPv4 addresses |
| `WIZ_PORT` | `8077` | HTTP port, 1–65535 |
| `WIZ_WORKERS` | `64` | Scan worker setting, 1–256; scan pool uses at most 32 |
| `WIZ_SCAN_TIMEOUT` | `1.5` | Positive seconds |
| `WIZ_OP_TIMEOUT` | `2.5` | Positive seconds |
| `WIZ_SCAN_INTERVAL` | `120` | Positive seconds between background scans |
| `WIZ_CACHE_TTL` | `300` | Positive numeric setting retained by the registry; it is not currently used for automatic eviction |
| `WIZ_PROBE_TIMEOUT` | `2.0` | Positive seconds for UDP status probes |
| `WIZ_RESPONSE_GRACE` | `60` | Zero or greater; recent-response window |
| `WIZ_PRESENCE_INTERVAL` | `60` | Positive seconds between TCP presence sweeps |
| `WIZ_PRESENCE_TIMEOUT` | `1.5` | Positive seconds for each TCP presence check |
| `WIZ_STATUS_INTERVAL` | `30` | Positive seconds between status refresh cycles |
| `WIZ_PROBE_MIN_GAP` | `20` | Zero or greater; minimum per-bulb probe spacing for request-driven probes |
| `WIZ_STATE_FILE` | `/data/state.json` | State-file path; empty disables persistence |
| `WIZ_API_TOKEN` | empty | Optional bearer token for API routes |
| `WIZ_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

The service currently uses IPv4 sockets (`AF_INET`). IPv6 subnets and IPv6 extra hosts are rejected during configuration validation.

## Tests

Run the test suite from the project directory:

```bash
python -m unittest -v test_app
```

The tests cover the HTTP contract, MAC and subnet parsing, configuration validation, UDP responsiveness versus LAN presence, probe rate limiting, persistence and category operations, concurrent state saves, and the bulb-removal/operation race.

The tests use a fake registry for HTTP routing and local or mocked behavior for registry tests; they do not replace an end-to-end test against physical WiZ bulbs on the target LAN.
