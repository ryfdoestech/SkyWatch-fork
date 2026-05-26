# 📡 SkyWatch

> A unified SDR monitoring dashboard. **Aircraft and drones on one map.** Built on FastAPI + Leaflet, no build step.

<p align="center">
  <img src="docs/screenshots/dashboard-vessels.png" alt="SkyWatch dashboard" width="90%"/>
</p>

<table>
<tr>
<td align="center" width="50%">

### ✈️ Aircraft (ADS-B 1090 MHz)
RTL-SDR + pure-Python decoder (or `readsb`)<br/>or OpenSky online

</td>
<td align="center" width="50%">

### 🛸 Drones (Remote ID)
ASTM F3411 over Bluetooth LE<br/>+ WiFi monitor mode

</td>
</tr>
</table>

> [!NOTE]
> **v1.1.0** — the dashboard was trimmed to Aircraft + Drones and visually overhauled (DroneWatch-inspired dark palette, cyan accent bars, chip-style badges, flat primary buttons). The AIS / APRS / NOAA Python modules still ship in the codebase ([skywatch/ais/](skywatch/ais/), [skywatch/aprs/](skywatch/aprs/), [skywatch/noaa/](skywatch/noaa/)) and accept their existing CLI flags — see [Backend-only modules](#-backend-only-modules-no-ui-as-of-v110) below — they just have no in-dashboard Start button right now.

---

## 🚀 Quick start

> [!TIP]
> **Are you a beta tester?** Head straight to **[QUICKSTART.md](QUICKSTART.md)** — short, no jargon, no Python install, includes the Zadig driver step. The section below is for developers building from source.
>
> **Shipping a build to testers?** See **[DISTRIBUTION.md](DISTRIBUTION.md)**.
>
> Visual How To Install Guide: https://link.excalidraw.com/readonly/v1V5Qwd0FsqBwJbmmPyN 

### One-click launch (developer mode)



| Platform | Command |
|---|---|
| 🪟 Windows | double-click `start.bat` |
| 🐧 Linux / 🍎 macOS | `./start.sh` |

The launcher creates a `.venv`, pip-installs everything from `requirements.txt`, and starts the server on `http://localhost:8080`. After the first run it skips the install step (only re-installs when `requirements.txt` changes), so it's also the everyday way to launch the app. Any flags you pass are forwarded — e.g. `start.bat -wifi wlan0`.

### Manual launch

```bash
pip install -r requirements.txt
python -m skywatch -addr :8080
```

Open **http://localhost:8080** → click ⚙ Settings → pick a module → **Start**.

> [!NOTE]
> **No hardware?** No problem. Pick the **Aircraft** tab in the sidebar, choose **Online (OpenSky)** in the device dropdown, and click **Start** — live aircraft appear the moment the map renders.

---

## 🧰 What's in the dashboard

<details open>
<summary><strong>✈️ Aircraft (ADS-B 1090 MHz)</strong></summary>

- **RF mode:** RTL-SDR dongle + a 1090 MHz antenna. Pure-Python decoder built in (no `readsb` install required) — `readsb` is supported as an alternative via `-readsb`.
- **Online mode:** OpenSky Network. Free tier rate-limited to ~400 calls/day; the dashboard polls only your visible map view to stretch the budget.
- **Database:** Aircraft type / registration / operator lookup. Importable from OpenSky in Settings.
- **Alert zones:** Drop a radius on the map and get a push notification when a new aircraft enters (filterable by military / helicopter / callsign).

</details>

<details open>
<summary><strong>🛸 Drone Remote ID (ASTM F3411)</strong></summary>

Catches drones broadcasting their identity and position. **Three independent receive paths**, any one is enough to put a drone on the map — and they can all run simultaneously for extra antenna coverage:

- **📶 Bluetooth LE — onboard radio** — uses the host's built-in Bluetooth radio via WinRT/bleak. **Just works**, catches DJI Mini 3/4 Pro, Mavic 3, Air 3, etc.
- **🔌 Bluetooth LE — Realtek dongle (WinUSB)** — a second BT radio over a Realtek RTL8761B(U) USB dongle, talked to via raw HCI through libusb. Bypasses the Microsoft Bluetooth stack so it runs alongside the onboard radio without the Code-31 "only one BT radio at a time" Windows limitation. One-time Zadig swap required; firmware blobs ship in `tools/win64/`.
- **📡 WiFi monitor mode** — sniffs 802.11 beacons + probe-responses for the F3411 vendor IE. Requires Npcap + a chipset whose driver supports monitor mode. **Most laptop WiFi cards refuse** — see [SETUP.md §6](SETUP.md#6-drone-remote-id) for known-good adapters.

</details>

---

## 🧪 Backend-only modules (no UI as of v1.1.0)

The Python modules below still ship in the codebase and accept their existing CLI flags. They speak the same REST + WebSocket protocol they always did — a custom client can still drive them — but the dashboard's sidebar tabs and Settings rows for them were removed in v1.1.0 pending a UX rework.

<details>
<summary><strong>🚢 Ships (AIS 162 MHz) — backend only</strong></summary>

- **RF mode:** RTL-SDR + `rtl_ais` (Linux/macOS) or **AIS-catcher** (the Windows-friendly drop-in, pre-built binaries). Auto-detected at startup.
- **Online mode:** [aisstream.io](https://aisstream.io) WebSocket feed. Free API key needed.
- Ship name, type, MMSI country, dimensions, draught, ETA, destination — all parsed from AIS message types 1/2/3/5/18.
- Start via `-ais-device N` or `-aisstream-key KEY`.

</details>

<details>
<summary><strong>📻 APRS (Amateur Packet Radio) — backend only</strong></summary>

Two independent receive paths plus an internet TX path:

- **APRS-IS** (internet) — `rotate.aprs2.net`, filtered by lat/lon/radius. Receive everywhere; transmit beacons / messages / status with your callsign + APRS-IS passcode.
- **APRS RF** (off-air) — `rtl_fm` + `multimon-ng` chain decodes Bell-202 AFSK1200 + HDLC + AX.25 from your RTL-SDR on 144.390 MHz (US) / 144.800 MHz (EU). See [SETUP.md §6.5](SETUP.md#65-aprs-rf-rtl_fm--multimon-ng).
- Start via `-aprs-is` (plus `-aprs-call`, `-aprs-pass`, `-aprs-lat/lon/radius`).

</details>

<details>
<summary><strong>🛰️ NOAA Weather Satellites — backend only</strong></summary>

- **Tracking:** SGP4 + Celestrak TLEs. Predicts NOAA-15 / 18 / 19 passes for your observer location.
- **APT capture:** RTL-SDR + `rtl_fm` records the 137 MHz transmission, recovers sync, and renders the grayscale image. Geometric correction (Doppler / earth-curvature) is on the roadmap.

</details>

<details>
<summary><strong>🌩️ NOAA Weather Radio + weather.gov — backend only</strong></summary>

- **NWR live audio** — REST endpoints stream any of the 7 NWR channels (162.4 – 162.55 MHz). The browser audio player UI was removed in v1.1.0; the `/api/noaa/radio/*` endpoints still work.
- **NWR transmitter map** — every NWR transmitter location is still in [data/nwr_stations.csv](data/nwr_stations.csv) and exposed via `/api/noaa/radio/stations`; the Leaflet overlay was removed.
- **weather.gov** — `/api/noaa/weather?lat=&lon=` still returns active alerts and forecasts.

</details>

---

## 📦 What runs where

**In the dashboard:**

| Capability | 🐧 Linux | 🍎 macOS | 🪟 Windows |
|---|:---:|:---:|:---:|
| ADS-B (RF) | ✅ | ✅ | ✅ (WinUSB via Zadig) |
| OpenSky online aircraft | ✅ | ✅ | ✅ |
| Drone RID — Bluetooth LE (onboard radio) | ✅ | ✅ | ✅ |
| Drone RID — Bluetooth LE (Realtek RTL8761B USB dongle, raw HCI) | — | — | ✅ Zadig → WinUSB; see [§6.4](SETUP.md#64-second-bt-radio-realtek-rtl8761b-via-winusb) |
| Drone RID — WiFi monitor mode | ✅ | ✅ * | ⚠️ Npcap **and** a compatible chipset — see [§6](SETUP.md#6-drone-remote-id) |

**Backend-only modules (CLI flags / REST API; no UI as of v1.1.0):**

| Capability | 🐧 Linux | 🍎 macOS | 🪟 Windows |
|---|:---:|:---:|:---:|
| AIS (RF) | ✅ | ✅ | ✅ (WinUSB via Zadig) |
| aisstream.io online vessels | ✅ | ✅ | ✅ |
| NOAA satellite tracking | ✅ | ✅ | ✅ |
| NOAA APT capture (`rtl_fm`) | ✅ | ✅ | ✅ |
| NOAA Weather Radio (`rtl_fm`) | ✅ | ✅ | ✅ |
| APRS-IS gateway | ✅ | ✅ | ✅ |
| APRS RF (`rtl_fm` + `multimon-ng`) | ✅ | ✅ | ✅ |
| weather.gov forecasts/alerts | ✅ | ✅ | ✅ |

<sub>* macOS WiFi monitor mode requires a compatible USB adapter; built-in Apple silicon WiFi is locked.</sub>

---

## 🗺️ How online feeds bound their queries

The dashboard sends the visible map bounds with every `/api/start` and on every pan/zoom (debounced **500 ms** for ADS-B). OpenSky polls only that bounding box, clamped to its 20°×30° free-tier limit. The AISStream backend uses the same pattern (re-subscribes only when the box shifts ≥0.5°) for callers that drive it via the REST API.

---

## 🚦 CLI flags

Run `python -m skywatch -h` for the full list. Defaults match the Go version.

<details>
<summary><strong>Show all flags</strong></summary>

| Flag | Default | Purpose |
|------|---------|---------|
| `-addr` | `:8080` | Dashboard listen address (binds to 127.0.0.1 by default — use `-addr 0.0.0.0:8080` for LAN) |
| `-readsb` | `readsb` | Path to readsb binary |
| `-rtl-ais` | `rtl_ais` | Path to rtl_ais binary |
| `-aisstream-key` | (empty) | aisstream.io API key (auto-starts AIS online feed) |
| `-adsb-device` | `-1` | Auto-start ADS-B on RTL-SDR index N |
| `-ais-device` | `-1` | Auto-start AIS on RTL-SDR index N |
| `-wifi` | (empty) | WiFi adapter for drone-RID |
| `-monitor` | `true` | Auto-enable monitor mode |
| `-channel` | `6` | WiFi channel (0 = hop 1/6/11) |
| `-aprs-is` | `false` | Auto-connect to APRS-IS internet feed |
| `-aprs-call` | `N0CALL` | Callsign |
| `-aprs-ssid` | `9` | SSID |
| `-aprs-pass` | `-1` | Passcode (-1 = receive only) |
| `-aprs-lat` / `-aprs-lon` / `-aprs-radius` | 0 / 0 / 150 | Filter center + radius (km) |
| `-aprs-freq` | `144.390` | RF freq (US=144.390, EU=144.800) |
| `-aprs-beacon` | `false` | Enable position beacon |
| `-aprs-interval` | `10m` | Beacon interval |

</details>

---

## 🏗️ Architecture

```
┌──────────────┐   subprocess   ┌──────────────┐
│   readsb     │── SBS:30003 ──▶│              │
└──────────────┘                │              │
┌──────────────┐   subprocess   │              │
│ AIS-catcher  │── NMEA:10110 ─▶│              │
└──────────────┘                │              │
┌──────────────┐  rtl_fm |      │              │
│ APRS RF      │  multimon-ng ─▶│   tracker    │──▶  FastAPI + WebSocket
└──────────────┘                │   + APRS     │      localhost:8080
┌──────────────┐    scapy       │   store      │
│ WiFi monitor │── F3411 IE ───▶│              │
└──────────────┘                │              │
        OpenSky / aisstream     │              │
        APRS-IS / Celestrak     │              │
        weather.gov  ──────────▶└──────────────┘
```

<details>
<summary><strong>Project layout</strong></summary>

```
skywatch/
├── __main__.py        Entry point
├── cli.py             argparse — every flag the Go main.go had
├── tracker.py         Unified target store
├── sdr.py             RTL-SDR enumeration
├── adsb/              readsb + SBS parser, OpenSky, aircraft DB, classifier
├── ais/               rtl_ais + NMEA, aisstream.io, ship-type/MMSI tables
├── aprs/              IS gateway, RF decoder (rtl_fm | multimon-ng),
│                      parser (uncompressed + base-91 compressed),
│                      station + message store, beacon/message TX
├── noaa/              SGP4 tracker, APT capture, NWR weather radio,
│                      weather.gov client
├── remoteid/          scapy WiFi sniffer, ASTM F3411 parser,
│                      bleak BLE scanner (onboard radio), and
│                      ble_hci raw-USB HCI driver for a second
│                      Realtek RTL8761B(U) BT dongle (firmware
│                      upload + LE scan over libusb)
├── web/
│   ├── server.py      FastAPI app + REST routes + WebSocket
│   ├── manager.py     Module lifecycle
│   └── static/        Dashboard (HTML + Leaflet + plain JS)
└── util/geo.py        Bounding-box helpers
```

</details>

---

## 🔬 Origins & lineage

SkyWatch started as a PyQt desktop app, was rewritten in Go for headless server deployment, and is now back in Python on top of FastAPI for the dashboard you see today. Every CLI flag, REST route, WebSocket message shape, dialed-in RF constant, and timeout from the Go version is preserved here — so existing setups keep working.

> [!IMPORTANT]
> **`main` is the only supported branch.** Earlier Go and PyQt implementations have been retired; please test only against `main`.

---

## 🚧 What's deferred

UI / UX rework:

- **AIS / APRS / NOAA tabs.** Backend modules ship and work; the dashboard's sidebar tabs, Settings rows, and detail panels for them were pulled in v1.1.0 pending a redesign.

Backend gaps inherited from the Go version:

- **APRS UV-Pro Bluetooth TNC.** TX path through APRS-IS works; UV-Pro KISS framing is stubbed.
- **APT image geometric correction.** Capture + sync detection + grayscale image work; Doppler / earth-curvature correction is on the roadmap.

> [!NOTE]
> **APRS RF demod was on this list — it's now shipping** (backend only as of v1.1.0). Bell-202 AFSK + HDLC + AX.25 decoded via the `rtl_fm | multimon-ng` chain. See [SETUP.md §6.5](SETUP.md#65-aprs-rf-rtl_fm--multimon-ng).

---

## 📜 License

MIT.
