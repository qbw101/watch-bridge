# Douyin Watch Assistant · Computer-Side Bridge

> 中文: [README.md](README.md)

A long-running, visible Chromium session on your computer that exposes Douyin (Chinese
TikTok) direct messages to the companion HarmonyOS client
[`watch-client`](https://github.com/qbw101/watch-client). **The watch never talks to Douyin
directly** — every Douyin action is performed by the browser here; the watch is only a
power-efficient, more comfortable remote control.

The transport is a **custom binary frame protocol** (deliberately *not* HTTP) over a raw
TCP long connection, default port `8787`, with an application-layer X25519 + AES-256-GCM
encryption layer on top.

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Install and Run](#install-and-run)
- [The Setup Page](#the-setup-page)
- [What to Enter on the Watch](#what-to-enter-on-the-watch)
- [Wire Protocol](#wire-protocol)
- [Sticker Library](#sticker-library)
- [Troubleshooting](#troubleshooting)
- [Project Layout](#project-layout)
- [Known Limitations](#known-limitations)
- [Disclaimer](#disclaimer)
- [License](#license)

## Features

| Capability | Description |
| --- | --- |
| Persistent Douyin session | Opens one visible Chromium window on the Douyin DM page at startup — exactly one per process |
| Conversation roster | The `friends` key in `config.json` decides which conversations the watch lists; the setup page can read them straight out of Douyin for you to tick |
| Read messages | Incremental via a `rev` value: an unchanged conversation only returns the `rev`, changed content sends the body |
| Send messages | Text and native stickers; the server watches the bubble for confirmation to avoid "false success" |
| Server-side push | The server polls the browser and only pushes a frame when content actually changed — the watch does not poll every 2 seconds |
| Image delivery | Sticker thumbnails and chat images are downscaled server-side, saving roughly an order of magnitude over tunnel bandwidth |
| Sticker library | Reads the Douyin sticker panel (including your own saved custom stickers) and caches thumbnails on disk |
| QR login | Start a QR login, see who is currently signed in, and sign out — all from the setup page |
| Setup page | Port / token / image sizes / startup behavior are all editable in the browser; no command-line flags to memorize |

## Architecture

```
┌──────────────────────────────┐
│ Huawei WATCH 5 (watch-client)│
│  raw TCP + binary frames      │
└───────────────┬──────────────┘
                │ LAN TCP (default 8787)
                │ X25519 handshake + AES-256-GCM
┌───────────────▼──────────────────────────────────────┐
│ Computer: this project                               │
│                                                      │
│  scripts/watch_server.py     entry point / logs / UI │
│    └─ bridge/tcp_server.py   thread per connection   │
│         ├─ read loop    framing + dispatch only      │
│         ├─ push thread  pushes when a watched chat   │
│         │               actually changes             │
│         └─ thread pool  slow work (read / send / img)│
│              └─ bridge/session.py                    │
│                   └─ persistent Chromium ──► Douyin  │
│                                                      │
│  bridge/setup_web.py         setup page (127.0.0.1)  │
└──────────────────────────────────────────────────────┘
```

### Three deliberate design choices

**Why not HTTP.** Tunnel providers classify traffic by *application protocol*: an HTTP(S)
tunnel requires a registered domain, while a TCP tunnel carrying HTTP can only use
non-mainland nodes (which are slow). A custom binary frame protocol is indistinguishable
from HTTP on the provider's side, so it can ride a mainland-node TCP tunnel. Besides,
HTTP's short connections plus 2-second polling are pure waste on a watch: every poll
re-establishes a connection and carries a full set of headers, and the vast majority of
polls return "nothing changed" — needlessly waking the radio. A long connection naturally
supports server-initiated push, which is the precondition for removing polling.

> To reach the service from outside your home Wi-Fi, use a **TCP tunnel** (not an HTTP
> tunnel) on a mainland node. Do not point your own domain at it via CNAME either — once
> a domain resolves to a mainland node you are back in domain-registration territory.

**The read loop only frames and dispatches.** Anything that can block (reading
conversations, sending messages, fetching images) goes to a thread pool. Otherwise one slow
request stalls every frame on that connection, including heartbeats.

**Images are downscaled server-side.** Tunnel bandwidth is typically 1–5 Mbps, and 65
original sticker images total 7.3 MB — a minute of waiting for the first screen. Scaling
them down to display resolution cuts total size by roughly an order of magnitude. Sticker
thumbnails are additionally cached "once to disk" (the originals include animated WebP
files of 1 MB+ with 71 frames, displayed as a ~70 px static cell on the watch).

## Requirements

| Item | Notes |
| --- | --- |
| Python | 3.10 or newer (developed on 3.13) |
| Playwright | Listed in `requirements.txt`; the browser engine must also be downloaded (see below) |
| Douyin account | Just needs to sign in and send/receive DMs normally |
| Network | Computer and watch on the same Wi-Fi; the computer should stay plugged in |
| Companion client | [`watch-client`](https://github.com/qbw101/watch-client) — neither half works alone |

## Install and Run

```bash
pip install -r requirements.txt
python -m playwright install chromium     # one-time browser engine download
```

Start it:

```bash
# Windows: just double-click (it fixes the working directory, disables console
# QuickEdit mode, and clears a stale lock file)
run_watch_server.bat

# Or manually
python scripts/watch_server.py
```

Command-line flags:

```bash
python scripts/watch_server.py                     # defaults to 0.0.0.0:8787
python scripts/watch_server.py --port 9000         # different port
python scripts/watch_server.py --token mytoken     # explicit token
python scripts/watch_server.py --crypto plain      # disable encryption (debug only)
python scripts/watch_server.py --host ::           # dual-stack, also accepts IPv6
python scripts/watch_server.py --setup             # also open the setup page
```

Precedence: **command line > environment variables > `server_config.json` > built-in
defaults**.

A Douyin browser window opens after startup — **keep it and the console window open**.
Closing Chromium breaks the Douyin session and you will have to restart the service. Logs go
to both the console and `artifacts/tcp_server.log`.

> **Allow the firewall on first run.** Windows Firewall inbound rules match by *program
> path*, so switching Python interpreters (a different virtualenv) silently invalidates the
> rule and LAN packets are dropped by default — and you cannot observe this on the machine
> itself, because `127.0.0.1` goes over the loopback and never crosses the firewall.
> Right-click `allow_lan_firewall.bat` → "Run as administrator" to allow port 8787.

## The Setup Page

On first run a local setup page opens **automatically**; while the service is running it
stays available at the same address (starting from port `8788`). Reopen it with `--setup` or
by visiting `http://127.0.0.1:8788/`.

> The setup page binds **`127.0.0.1` only**. The other listener in the same process (8787)
> is the one exposed to the LAN. Do not merge these onto a single listener.

Seven sections:

| # | Section | Contents |
| --- | --- | --- |
| 1 | Two values to enter on the watch | Shows the machine's LAN address and the access token to transcribe onto the watch |
| 2 | Douyin account | Who is signed in (nickname / avatar / uid), start QR login, sign out |
| 3 | Which friends to show on the watch | Can be read from the Douyin DM list for you to tick; written back to `friends` in `config.json` |
| 4 | Connection settings | Listen address, port, access token, link encryption toggle |
| 5 | Image delivery | Sticker thumbnail edge length, chat image edge length |
| 6 | Startup behavior | Whether to open the setup page on startup |
| 7 | Current status | Read-only: protocol version, listen address, readiness, etc. |

Behavioral guarantees:

- **Validate before writing; if validation fails, not a single byte is written.** The port
  must be an integer in 1–65535, the token cannot contain spaces, and the friends list
  cannot be empty. On failure the page keeps your original text — nothing is silently
  overwritten with a default.
- **Connection settings take effect after a restart** (the page marks this); same for image
  edge lengths.
- **The friends list only ever touches the `friends` key** — every other key in the file is
  preserved verbatim.
- **Broken config never blocks startup**: if `server_config.json` fails to parse it is
  renamed to `server_config.json.bad` and defaults are used, so you can still recover your
  settings afterwards.

## What to Enter on the Watch

Section 1 of the setup page lists both values directly — transcribe them:

| Field | Value |
| --- | --- |
| Computer address | `LAN-IP:8787` (port defaults to 8787 if omitted) |
| Access token | An 8-digit hex string generated on the service's first run |

The token is also mirrored to `artifacts/watch_token.txt`. Both values are persisted locally
on the watch; to change them, tap **设** ("Settings") in the top-right of the watch's list
page.

## Wire Protocol

The frame header is a fixed **8 bytes, big-endian**:

| Offset | Length | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 4 | Length | Payload byte count, excluding the header itself |
| 4 | 1 | Type | See below |
| 5 | 2 | Request ID | `0` means a server-initiated push with no matching request |
| 7 | 1 | Flags | See below |

Types:

| Value | Name | Direction |
| --- | --- | --- |
| `0x01` | `HELLO` | Client handshake carrying the token; server replies ok |
| `0x02` | `REQ` | Client → server |
| `0x03` | `RES` | Server → client (reply or push) |
| `0x04` | `IMAGE` | Server → client: image binary |
| `0x05` | `PING` | Either side |
| `0x06` | `PONG` | Either side |

Flags: `FLAG_MORE = 0x01` (IMAGE: more frames belong to the same image),
`FLAG_ERROR = 0x02` (RES: payload is an error message), `FLAG_PUSH = 0x04` (RES: a
server-initiated push).

Constants: `PROTOCOL_VERSION = 1`, `MAX_PAYLOAD = 4 MB`, `HELLO_TIMEOUT_SECONDS = 10.0`.
Mismatched versions are rejected outright so that old and new implementations never parse
the same bytes under different assumptions.

### Command table (the `op` field in a `REQ` payload)

| `op` | Purpose |
| --- | --- |
| `status` | Whether the service is ready and which conversation is active |
| `messages` | Read a conversation's messages (incremental via `rev`) |
| `send` | Send text or a sticker; returns the fresh message list to save a round trip |
| `conversations` | Read the conversation list |
| `image` | Fetch one image (sticker / chat image), downscaled |
| `sticker_thumbs` | Batch-fetch sticker thumbnails — the whole library in one round trip |
| `refresh_stickers` | Manually refresh the sticker library (the watch's "刷" button) |
| `sync_stickers` | Non-blocking "keep it fresh" nudge when the sticker panel opens |
| `watch` | Subscribe to a conversation; the server pushes when content changes |
| `unwatch` | Unsubscribe |

### Protocol-level caveats

- **Encryption must be serialized.** The nonce is built from a direction prefix plus a
  counter, so concurrent encryption collides on the nonce — and a collision drops the
  connection with **no log entry at all**. `bridge/crypto.py::SecureChannel` is
  single-threaded by contract.
- **The payload carries a second AEAD layer**: X25519 key exchange + HKDF-SHA256 +
  AES-256-GCM, while the frame header stays in the clear — it leaks only the length, in
  exchange for framing that is completely independent of encryption state.
- **Authentication relies on a pre-shared token**, which does not participate in key
  derivation and is compared exactly once, in constant time, during the handshake. Every
  connection uses a fresh ephemeral key pair, so recorded traffic cannot be decrypted later
  even if the token leaks (forward secrecy).
- **TCP is a byte stream** — never assume "one callback = one frame". Everything received
  must go through a framer (sticky packets and partial packets are absorbed there). The
  server uses `bridge/protocol.py::FrameReader`.

To validate an implementation, use the two included scripts: `scripts/tcp_smoke.py` (is the
link alive at all?) and `scripts/tcp_watch_sim.py` (replays the watch's exact call sequence
on the computer and checks the server contract item by item).

## Sticker Library

Stickers in the Douyin panel have no names, and custom ones can only be addressed as "column
N, item M" — so the sticker library is a **scan artifact**, persisted to
`watch_stickers.json`, with thumbnails under `artifacts/stickers/` (originals) and
`artifacts/sticker_thumbs/` (derived small images).

- Original sticker URLs carry signature parameters and expire to 403 after a while. The scan
  stores the binary locally, so the watch reads local files — fast and never stale.
- `artifacts/sticker_thumbs/` is a **pure cache**; delete the whole directory at any time and
  it regenerates on demand.
- While running, the service also refreshes the sticker panel opportunistically (no need to
  stop the service or log in a second time, which reduces risk-control exposure). The
  resulting mtime change makes the push loop send the new state to the watch automatically.
- Offline scanning is still available: `python scripts/scan_stickers.py`, but it needs
  exclusive access to `artifacts/run.lock`, i.e. the service must be stopped.
- To eyeball "which stickers should appear on the watch", render the library into a visual
  board with `python scripts/make_sticker_board.py` — counting 66 cells on a watch face is
  not realistic.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| The service freezes mid-run, then a couple of keystrokes revive it | Windows console **QuickEdit mode**: once the console enters selection state, any write blocks, and Python's `logging` holds a lock — one stalled thread piles every other thread onto that lock. `bridge/console.py` disables the flag at startup; **do not delete that module** |
| The watch cannot reach the computer | ① Service not running; ② different subnet; ③ firewall not opened (see above — invisible from the machine itself) |
| "Invalid token" | The two sides disagree. Trust the value on the setup page (`artifacts/watch_token.txt` mirrors it) |
| "Port already in use" at startup | Change the port, or use the setup page. The setup page's own 8788 automatically tries the next few ports, so ignore that one |
| "Lock file held" at startup | A previous forced interruption (closing the console window, killing the process) skipped the `finally` block. `scripts/clear_stale_lock.py` clears it at startup; manual removal works too |
| QR scanned but nothing happens | Login always runs `scripts/login_auto.py` **as a subprocess**, so a crashed subprocess or an abandoned scan cannot take down the service. The page tails the subprocess output — look there |
| Closing the browser window produces a wall of errors | Closing Chromium breaks the Douyin session; restart the service |
| A just-saved sticker doesn't show on the watch | A delay is normal (the push loop refreshes on an interval). Tap the watch's "刷" button or call `refresh_stickers` to force it |
| Every sticker cell is blank | First verify in a browser that `op=image` returns an image; if it does not, that sticker simply has no thumbnail. Try rebuilding with `python scripts/build_sticker_thumbs.py` |
| Sending takes several seconds | Expected. The server waits on the bubble for confirmation (a minimum 2-second observation window) — that is the correctness guarantee against "false success" |
| Message list comes back empty / DOM mismatch | Douyin changed its markup. Re-probe with `python scripts/probe_chat.py` and compare against the selector list at the top of `bridge/reader.py` |
| The watch shows the "desktop preview shell" | If the watch browser renders as a desktop page, the layout viewport is stretched to 980 px and the detection misfires. `scripts/preview_mode_check.py` is the regression test for that decision |

Diagnostic scripts (under `scripts/`):

| Script | Purpose |
| --- | --- |
| `probe_endpoint.py` | Probe whether an `IP:port` is really running this service and can actually work |
| `probe_chat.py` | Probe the Douyin DM page DOM to pin down selectors |
| `clear_stale_lock.py` | Clear a stale lock file |
| `tcp_smoke.py` | Reference implementation of the protocol + end-to-end smoke test |
| `tcp_watch_sim.py` | Replays the watch's call sequence and validates the contract item by item |
| `console_freeze_check.py` | Verifies the "frozen console → stalled service" chain and that the fix works |
| `crypto_concurrency_check.py` | Verifies the "only one thread may encrypt at a time" contract |
| `bench_bridge.py` / `bench_sticker_images.py` / `bench_watch_image_latency.py` | Performance checks: per-endpoint timing, single-image cost, time to fetch every thumbnail in the watch's current order |
| `setup_page_check.py` | HTTP-level self-check of the setup page (purely local: binds a real server on 127.0.0.1 and tears it down) |
| `setup_page_ui_check.py` | Browser-level walkthrough of the setup page (Playwright) — does clicking a button scramble the page? |
| `setup_page_shot.py` | Renders the setup page to an image so you can preview `setup.html` changes without running the service |
| `make_sticker_board.py` | Renders the sticker library into a visual board |
| `verify_sticker_send.py` | Verifies "what you previewed is what got sent" |

The first two self-check scripts require Playwright and deliberately **do not depend on the
real `config.json` in the project root** (that is runtime data, absent from version control;
they carry their own sample roster), so they run right after a clone:

```bash
python scripts/setup_page_check.py
python scripts/setup_page_ui_check.py
```

## Project Layout

```
watch-bridge/
├── scripts/
│   ├── watch_server.py          entry point: logging, flags, setup page, first-run wizard
│   ├── login_auto.py            QR-login subprocess (auto-detects success, saves credentials)
│   ├── scan_stickers.py         offline sticker-panel scan
│   └── …                        self-check / probe / benchmark scripts (see the table above)
├── bridge/
│   ├── tcp_server.py            raw TCP service: thread per connection, read loop, push, pool
│   ├── protocol.py              binary frame protocol (header, types, flags, framer)
│   ├── crypto.py                X25519 + HKDF-SHA256 + AES-256-GCM
│   ├── session.py               persistent Douyin session (visible Chromium + serialized commands)
│   ├── reader.py                reads the message list (all DOM selectors live here)
│   ├── fastopen.py              conversation switching: click the sidebar, fall back to search
│   ├── console.py               Windows console QuickEdit handling (do not delete)
│   ├── server_config.py         server_config.json read/write/validate
│   ├── watch_friends.py         the single `friends` key in config.json
│   ├── friend_scan.py           harvests the Douyin conversation list for the setup page
│   ├── credentials.py           Douyin account: identity, QR login, sign out
│   ├── sticker_store.py         watch_stickers.json sticker library
│   ├── sticker_thumbs.py        thumbnail disk cache
│   ├── sticker_refresh.py       lets the running service refresh its own sticker library
│   ├── netinfo.py               local LAN / IPv6 address discovery
│   ├── setup_web.py             setup-page HTTP server (binds 127.0.0.1 only)
│   ├── setup.html               setup page front end (single file)
│   └── server.py / watch.html   HTTP v0 archive, referenced by 3 diagnostic scripts (see below)
├── app/                         low-level Douyin page interaction (browser / read / send / selectors)
├── attic/http-v0/               complete HTTP-version snapshot (deprecated, archived)
├── requirements.txt
├── run_watch_server.bat         one-click Windows launcher
├── allow_lan_firewall.bat       allow inbound 8787 by port (needs admin)
├── config.example.json          sample friends roster (the real config.json is not committed)
├── server_config.example.json   sample service settings (the real file is not committed)
└── .env.example                 sample environment variables such as HEADLESS
```

Runtime files (all excluded by `.gitignore`): `config.json`, `server_config.json`,
`storage-state.json` (Douyin credentials), `watch_stickers.json`, and `artifacts/` (logs, lock
file, image caches, token).

## Known Limitations

| Limitation | Notes |
| --- | --- |
| Requires a visible browser | One Chromium per process, in visible mode; closing it breaks the session |
| Single instance | `artifacts/run.lock` serializes access — only one service may hold the browser session at a time |
| Conversation count | Bounded by what is ticked in `config.json`; more entries make a longer watch list |
| Custom stickers are addressed by index | Custom stickers have no names, so they are addressed as "column N, item M"; adding or removing stickers in Douyin shifts those indices and a rescan is needed |
| Inherent automation risk | Douyin may change its markup or API at any time, and may tighten risk controls at any time |
| Plaintext HTTP is deprecated | The old HTTP version is archived in full under `attic/http-v0/` and no longer maintained. `bridge/server.py` and `bridge/watch.html` are the two files from it still referenced by `check_sticker_api.py` / `preview_mode_check.py` / `sticker_grid_check.py`, so they temporarily remain in place |
| Personal use only | See the disclaimer below |

## Disclaimer

- This project is intended for **personal study and research only**. Do not use it commercially, and do not use it in any way that violates Douyin's user agreement or terms of service.
- Automating a Douyin account carries the risk of rate-limiting, feature restrictions, or outright bans. Assess the risk yourself and accept the consequences.
- The author accepts no responsibility for any outcome of using this project, including but not limited to account loss, data loss, or legal disputes.
- Comply with the laws of your jurisdiction. This project comes with no warranty of any kind.

## License

This project is released under the [GNU General Public License v3.0](LICENSE), copyright 仇博文 (qbw).

```
Douyin Watch Bridge  Copyright (C) 2026  仇博文 (qbw)
```

You are free to use, modify, and redistribute this project, but **any derivative work must also be released under GPL-3.0** with the original copyright notice preserved. This project comes with no warranty.
