# wiz-bulb-api

Small LAN service that finds **WiZ bulbs by MAC address**, reports their state
and colour, and lets other devices toggle them over plain HTTP. Ships with a
browser UI for identifying bulbs.

It exists because two things are impossible from a Shelly:

- WiZ bulbs only speak **UDP 38899**, and Shelly mJS cannot send raw UDP.
- WiZ bulbs cannot be given static IPs, so their address is not stable.

This service keeps a `MAC -> IP` map (refreshed by scanning) and translates HTTP
calls into WiZ UDP packets.

Stdlib only — Python 3.12, no dependencies.

## Web UI

Open `http://<host>:8077/` in a browser. Every bulb found on the LAN is shown as
a card with:

- MAC, IP and model
- online / off-network
- the **colour currently set** (RGB, colour temperature or white channel) as a
  swatch
- a single Turn on / Turn off button

Use the button to identify a bulb physically — the lamp changes immediately.
There is no dimming or colour control by design. The page polls live state every
5 s and greys out bulbs that do not answer.

If `WIZ_API_TOKEN` is set, the page asks for the token once and stores it in
`localStorage`.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | web UI |
| `GET` | `/healthz` | liveness |
| `GET` | `/bulbs` | cached list |
| `GET` | `/bulbs?live=1` | live `getPilot` for every known bulb (parallel) |
| `GET` | `/bulb/<mac>` | live check: `200` bulb answered, `503` did not |
| `GET` | `/bulb/<mac>/state` | same, plus state and colour |
| `POST` | `/bulb/<mac>/toggle` | flip state using the cached state |
| `POST` | `/bulb/<mac>/on` | `setPilot {"state": true}` |
| `POST` | `/bulb/<mac>/off` | `setPilot {"state": false}` |
| `POST` | `/bulb/<mac>/set` | body forwarded to `setPilot` (whitelisted) |

MACs are accepted in any form: `9877d50abae0`, `98:77:d5:0a:ba:e0`,
`98-77-D5-0A-BA-E0`.

`state` returns the full light output, not just on/off:

```json
{
  "mac": "cc4085c78ad0",
  "ip": "192.168.1.144",
  "online": true,
  "present": true,
  "responsive": true,
  "last_response_s": 1,
  "model": "ESP25_SHRGB_01",
  "state": true,
  "dimming": 80,
  "r": 255, "g": 96, "b": 0,
  "c": null, "w": null,
  "temp": null,
  "sceneId": 0,
  "speed": null
}
```

White-mode bulbs report `temp` / `c` / `w`; colour-mode bulbs report `r/g/b`.
All fields are always present, unused ones are `null`.

Unreachable bulb: `503 {"mac":"…","online":false}`.

## Docker

From inside this subfolder:

```bash
docker compose up -d --build
```

The included compose file uses `network_mode: host`, which is the least
surprising option on a home server: the scanner sees the LAN directly and the UI
is reachable at `http://<host-ip>:8077`. Bridge networking also works (unicast
scanning is used); see the comment at the bottom of the file.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `WIZ_SUBNET` | `192.168.1.0/24` | where to look for bulbs |
| `WIZ_HOSTS` | – | extra addresses, comma separated |
| `WIZ_PORT` | `8077` | HTTP port |
| `WIZ_SCAN_INTERVAL` | `60` | seconds between background scans |
| `WIZ_SCAN_TIMEOUT` | `0.3` | UDP timeout during a scan |
| `WIZ_OP_TIMEOUT` | `1.5` | UDP timeout for a live request |
| `WIZ_WORKERS` | `64` | parallel hosts during a scan |
| `WIZ_CACHE_TTL` | `180` | how long a cached IP is trusted |
| `WIZ_PROBE_TIMEOUT` | `0.6` | UDP timeout for live UI probes |
| `WIZ_RESPONSE_GRACE` | `30` | seconds without a reply before "no response" |
| `WIZ_PRESENCE_INTERVAL` | `5` | seconds between LAN presence sweeps |
| `WIZ_PRESENCE_TIMEOUT` | `0.4` | TCP timeout for a presence check |
| `WIZ_API_TOKEN` | – | if set, require `Authorization: Bearer <token>` |
| `WIZ_LOG_LEVEL` | `INFO` | log verbosity |

## How discovery works

1. A background thread scans the subnet (`getDevInfo`) and records `MAC -> IP`.
2. A request resolves the MAC from that cache and talks to that IP.
3. If the cached IP stops answering, the service rescans once and retries.
4. If the bulb is still silent, the request returns `503`.

Bulbs are matched by MAC, so DHCP changing their address is handled
automatically. No reservations needed. Forced rescans are rate-limited, so a
burst of missing bulbs cannot turn into a scan storm.

### Presence vs. response

Two independent signals are tracked:

- **LAN presence** — every 5 s the service opens a TCP connection to port 38899
  on each known IP. A WiZ bulb refuses it, which proves the device is on the
  network even when it is too busy to answer UDP. Not found = **offline
  immediately**.
- **Response** — `getPilot` over UDP. If a bulb is on the LAN but has not
  answered for `WIZ_RESPONSE_GRACE` seconds (default 30), the UI shows
  **no response** instead of flapping to offline.

So a lost UDP packet only changes "Connected" to "No response"; only the TCP
presence check can make a bulb disappear.

## Shelly integration

One bulb per switch:

```js
let CFG = {
  probe:  "http://192.168.1.5:8077/bulb/9877d50abae0",
  toggle: "http://192.168.1.5:8077/bulb/9877d50abae0/toggle",
  setOn:  "http://192.168.1.5:8077/bulb/9877d50abae0/on",
  id: 0,
  scanMs: 5000,
  lostHits: 3
};
```

`200` from `probe` means "bulb present → detached/smart"; `503` means "bulb
gone → dumb/relay". See `smart-bulb-mode.js` in the parent repo.

## Installing into ShellyScripts

This folder is self-contained and can live at `wiz-bulb-api/` inside
[lorandignat/ShellyScripts](https://github.com/lorandignat/ShellyScripts):

```bash
git clone https://github.com/lorandignat/ShellyScripts.git
cd ShellyScripts
cp -r /path/to/wiz-bulb-api .
git add wiz-bulb-api
git commit -m "add wiz-bulb-api"
git push
```

After that, anyone (including you) fetches it with a normal `git pull`:

```bash
cd ShellyScripts
git pull
docker compose -f wiz-bulb-api/docker-compose.yml up -d --build
```

The existing scripts at the repo root, including `smart-bulb-mode.js` and its
raw URL, are untouched.

### Publishing a GHCR image

GitHub only runs workflows from `.github/workflows/` at the repo root, so copy
the template once:

```bash
mkdir -p .github/workflows
cp wiz-bulb-api/ci/docker.yml .github/workflows/wiz-bulb-api.yml
git add .github/workflows/wiz-bulb-api.yml
git commit -m "ci: build wiz-bulb-api image"
git push
```

That publishes `ghcr.io/<owner>/shellyscripts-wiz-bulb-api:latest` on pushes
that touch `wiz-bulb-api/**`. The package is private by default; make it public
in the package settings, or log in on the host:

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u <owner> --password-stdin
```

Then `docker compose pull && docker compose up -d` uses the image instead of
building locally.

## Finding the MACs

```bash
curl -s http://192.168.1.5:8077/bulbs | python3 -m json.tool
```

Match a MAC to a room by toggling it and watching which lamp changes — or just
use the web UI.

## Tests

```bash
python3 -m unittest -v test_app.py
```
