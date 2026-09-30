#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright (C) 2026 Mark Kuebel
# Use of this software is governed by the Business Source License
# included in the LICENSE file at the root of this repository and at
# https://mariadb.com/bsl11/. On the Change Date stated there, use
# of this software will be governed by the GNU Public License, Version 3.0.
"""
omara_bridge.py — local WebSocket bridge to Omara hardware over BLE or Serial.

  [clients] --ws/wss--> [this bridge, persistent device connection] --> Omara

Hardware facts (owner-confirmed on this unit):
  * The cartridge has 16 channels; at most 3 may emit at the same time.
  * Device: "Stinky", fw 3.4.0, pack "Common Scents".
  * fw 3.4.0 quirk (owner-observed): S_MAX_T read replies carry tube byte
    0xFF instead of the requested index — accepted in _read_field via
    TUBE_ECHO_EXEMPT (safe: exactly one request is ever outstanding).

Device side: ONE persistent local connection (BLE via bleak, or USB serial via
pyserial). Client side: loopback WebSocket server, default ws://127.0.0.1:8765.
All protocol complexity lives HERE; clients send plain JSON commands and never
need to pass channel counts (all sweeps/backup default to 16 in the bridge).

LOADED-NAME CACHE (why sprays are fast)
---------------------------------------
Mix packets address cartridges BY NAME, so a spray must know what is loaded.
That used to mean a full sweep per spray; now it means one sweep per device
connection:
  * Warmed at startup, after the device connects and before any client is
    served (--no-warm-names to skip). Sprays then cost ZERO device reads.
  * The cache NEVER expires on a timer. Polling a cartridge that hasn't moved
    is pure latency. It is invalidated only by evidence:
      - a spray whose name isn't in the table (pack swap signature) — re-read
        once and retry inline, so the caller still just sends one message;
      - a device error arriving within DISPATCH_SUSPECT_WINDOW_S of a burst
        that used cached names (the cache lied);
      - a spontaneously pushed cart_serial / pack_name that differs from the
        pair the cache was built against;
      - any sudo write;
      - an explicit refresh ("refresh_names" on spray, "refresh" on names).
  * Automatic re-reads are rate-limited to one per NAMES_REFRESH_MIN_S so a
    model repeatedly naming an unloaded cartridge cannot turn every spray into
    a sweep. Explicit refreshes bypass the limit.

RATE LIMITING (--rate / --rate-mode)
------------------------------------
Every scent dispatch (spray/mix/v1 shorthand) passes through ONE limiter;
"stop" never does — stopping emission must always be immediate. --rate SECONDS
is the minimum gap between device dispatches (0 = off, default). Requests that
land inside a closed window obey --rate-mode:
  FIRST    fire the first request, drop every later one until the next slot;
  LAST     hold a pending slot and replace it with each newer request — the
           newest wins when the slot opens (idle-past-window fires at once);
  AVERAGE  collect the whole window, average intensity per channel
           (sum / number of requests), fire the top 3 channels as one mix;
  WAIT     queue everything and send one per slot (backlog capped).
Dropped/superseded/averaged replies carry "dispatched":0 plus a "rate_limited"
note so clients can tell exactly what happened to their request.

Safety tiers (omara_serial_api.txt §13):
  Tier A — FIRMWARE/OTA: 0x15/0x16, raw OTA bytes (0xFB), SMP char writes, and
        MFG_BURN_HASH (0x27) are structurally impossible here. No code path
        constructs them; any attempt raises SecurityError. Always.
  Tier B — PERSISTENT WRITES: every MFG_SET_* with a documented payload is
        available under --sudo via "sudo_write" (see SUDO_WRITES below). Each
        write reads the old value first, writes, re-reads, and reports all
        three, so every change is reversible. Deliberately NOT exposed:
          0x27  RGB/burn-hash write — payload "TBD", dual-named BURN_HASH
          0x37  SET_MAX_VALVE_TIME — no payload spec exists anywhere
          0xD0/0xD2 CSV save/delete — recipe contents have no documented read,
                so there would be no restore path
        Without --sudo, NO Tier B frame can leave the bridge.
  Tier C — odorant play/stop, all read-only queries (including MFG scalar and
        RGB reads), and DEVICE_SET_STATE (transport mode + wake only).

Client protocol (JSON text frames — one {ok:...} reply per request; events
are pushed asynchronously, so clients filter for "ok"):
  {"cmd":"status"}                          bridge/device snapshot
  {"cmd":"ping"}                            device hello (name query)
  {"cmd":"spray","scents":[[name,pct],...],"algorithm":"average",
                                "refresh_names":false?}
                                            THE HOT PATH: resolves every name
                                            against the cartridge cache and
                                            fires ONE mix packet. Nothing is
                                            sent if no pick resolves.
  {"cmd":"names","refresh":true?}           loaded names (cached; sweep only
                                            if asked or already stale)
  {"cmd":"mix","scents":[[name,pct],...],"algorithm":"average"}   max 3 scents,
                                            UNVERIFIED names — raw fast path,
                                            a typo is a wasted burst
  {"cmd":"stop","scents":[names]}           stop channels by name
  {"cmd":"query","what":field}              read-only field query
  {"cmd":"tubes","deep":true?}              per-channel sweep (all 16); deep adds
                                            t_max_t / scent_version / channel_rgb
  {"cmd":"backup_calibration","path":f}     full read-only snapshot: every device
                                            string + all 16 channels incl. MFG
                                            scalars and RGB (deep by definition)
  {"cmd":"set_max_t","tube":N,"value":0-255}         SUDO ONLY (--sudo)
                                ("name" instead of "tube" resolves from cache)
  {"cmd":"sudo_write","what":field,"value":v,"tube":N?}   SUDO ONLY (--sudo)
  {"odor":name,"intensity":pct}             single-scent shorthand (v1 compat),
                                            resolved through the name cache

Backup key names vs sudo_write field names: "name"=scent_name, "max_t"=s_max_t;
all other keys match their sudo_write name.
"""

import argparse
import asyncio
import json
import ssl
import sys
import time
from pathlib import Path

try:    # websockets is a server-side dependency only; keep protocol core clean
    from websockets.exceptions import ConnectionClosed as _WSClosed
    WS_CLOSED = (_WSClosed,)
except Exception:                                     # pragma: no cover
    WS_CLOSED = ()

# ------------------------------------------------------------ protocol core --

ODORANT_COMMANDS = 0x14      # multi-scent play packet type (start byte 0xE3)
START_MULTI = 0xE3           # first byte of every odorant packet
STOP_BYTE = 0x0A             # terminates each scent name in a mix payload
ERROR_STATUS = 0x3C          # heartbeat error/status block; len=0 -> no active error

# At most 3 channels may emit simultaneously (owner-confirmed on this hardware).
# The source's host-side constant allows 9 sub-commands per packet
# (U Source/API/OdorantManager.cs:19), but we cap at the real emission limit.
MAX_SCENTS = 3

DEFAULT_TUBES = 16           # cartridge channel count (owner-confirmed)

ALGORITHMS = {"add": 0x00, "subtract": 0x01, "min": 0x02, "max": 0x03, "average": 0x04}
# NOTE: official software only ever sends AVERAGE; the other operators exist in
# the enum but their firmware behavior is unverified.

REQ = {   # Tier C read-only queries (response type == request + 1)
    "firmware": 0x04, "name": 0x06, "serial": 0x08, "version": 0x0A,
    "battery": 0x0C, "state": 0x0E, "pressure": 0x11, "pack_name": 0x22,
    "fill_date": 0x1E, "first_use_date": 0x38, "cart_serial": 0x20,
}
# Response id -> field name, derived: every REQ response is request+1.
# battery (0x0D), state (0x0F) and pressure (0x12) answer with BYTES, not
# strings, and are decoded in dedicated branches of handle_frame / cmd_query.
STRING_REQ = {f: t for f, t in REQ.items() if f not in ("battery", "state", "pressure")}
RESP_STRING = {req + 1: field for field, req in STRING_REQ.items()}

TUBE_NAME = 0x1C       # per-tube scent name request   (READ ONLY)
TUBE_BURST = 0x17      # per-tube lifetime burst count (READ ONLY)
TUBE_MAXT = 0x19       # per-tube S_MAX_T read         (READ ONLY)
                       # FW QUIRK (3.4.0, owner-observed): the 0x1A response
                       # does NOT echo the tube index — payload[0] arrives as
                       # 0xFF. Handled in _read_field via TUBE_ECHO_EXEMPT.
TUBE_TMAXT = 0x30      # MFG per-tube scalar T_MAX_T read (resp 0x31, 3-byte BE)
TUBE_SCENTVER = 0x32   # MFG scent version/freq read    (resp 0x33, 3-byte BE)
CHANNEL_RGB = 0x2E     # per-channel RGB read; response REUSES id 0x2E:
                       # `00 2E 04 00 <ch> R G B`

DEVICE_SET_STATE = 0x10      # state change; no response
SERIAL_MODE_VALUE = 0x02     # value sent to enter serial mode
SLEEP_ERROR_BYTE = 0x02      # byte in error payload marking device asleep

DEFAULT_TIMEOUT = 2.0    # plugin MessageQueueManager window (s)
SWEEP_TIMEOUT = 0.8      # absent channels never answer; fail fast in a sweep
STALE_DRAIN_S = 0.15     # let stragglers land while nothing is expected (s)
REQUEST_GAP_S = 0.05     # small pause between device requests (s)

# Rate limiting (--rate/--rate-mode): WAIT backlog cap, so an unbounded client
# cannot build an infinite queue of future bursts.
RATE_WAIT_MAX = 32

# ------------------------------------------------------- name-cache policy --
# The cache has NO expiry: a cartridge that hasn't moved answers the same way
# every time, so re-asking is pure latency. Only evidence refreshes it.
NAMES_REFRESH_MIN_S = 30.0   # min gap between AUTOMATIC re-reads (storm guard)
DISPATCH_SUSPECT_WINDOW_S = 10.0   # error this soon after a burst => cache lied
# Free, push-only identity pair: if the device ever reports a different pack or
# cartridge than the one the cache was built against, the names are wrong.
PACK_ID_FIELDS = ("cart_serial", "pack_name")

ION_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"   # scan filter (omara_ble.py)
WRITE_CHAR = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NOTIFY_CHAR = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
READY_CHAR = "6e400005-b5a3-f393-e0a9-e50e24dcca9e"
ERROR_CHAR = "6e400006-b5a3-f393-e0a9-e50e24dcca9e"
BATTERY_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"

# TIER A — never construct, never write, regardless of --sudo.
FORBIDDEN_TYPES = {0x15, 0x16, 0xFB, 0x27}   # firmware req/resp, OTA data, burn hash
FORBIDDEN_CHARS = {"da2e7828-fbce-4e01-ae9e-261174997c48"}
# Normal outbound set (Tier C + SET_STATE), including the MFG read queries.
OUTBOUND_ALLOWLIST = ({ODORANT_COMMANDS, DEVICE_SET_STATE} | set(REQ.values())
                      | {TUBE_NAME, TUBE_BURST, TUBE_MAXT,
                         TUBE_TMAXT, TUBE_SCENTVER, CHANNEL_RGB})

# Readable persistent fields (Tier C). Maps field name -> (request type,
# response type). Response is normally request+1; the RGB query is the
# documented exception (it reuses 0x2E).
FIELD_READS = {
    # device-level
    "firmware":         (0x04, 0x05),
    "device_name":      (0x06, 0x07),
    "device_serial":    (0x08, 0x09),
    "device_version":   (0x0A, 0x0B),
    "fill_date":        (0x1E, 0x1F),
    "cart_serial":      (0x20, 0x21),
    "pack_name":        (0x22, 0x23),
    "first_use_date":   (0x38, 0x39),
    # per-tube (request payload is the tube index byte)
    "scent_name":       (TUBE_NAME,     0x1D),
    "burst_count":      (TUBE_BURST,    0x18),
    "s_max_t":          (TUBE_MAXT,     0x1A),
    "t_max_t":          (TUBE_TMAXT,    0x31),
    "scent_version":    (TUBE_SCENTVER, 0x33),
    "channel_rgb":      (CHANNEL_RGB,   0x2E),   # response reuses the request id
}
PER_TUBE_FIELDS = {"scent_name", "burst_count", "s_max_t",
                   "t_max_t", "scent_version", "channel_rgb"}

# Per-tube responses normally begin with the requested tube index (framing
# rule, omara_serial_api.txt §2). One exception observed on this hardware:
# S_MAX_T (0x19 -> 0x1A) answers with payload[0] = 0xFF instead of echoing
# the index. Because the link is strictly serialized — exactly one outstanding
# request at a time, with stale-response draining after any timeout — an 0x1A
# frame can only be the answer to our most recent 0x19, so we accept it and
# log rather than discard the value. Other fields echo correctly on this unit
# (burst count / scent name) and keep the strict check.
TUBE_ECHO_EXEMPT = {"s_max_t"}

# Tier B writes that --sudo unlocks. Every entry has a documented payload in
# the source (omara_serial_api.txt §4). kind: byte | u3be | str | str_tube.
SUDO_WRITES = {
    "s_max_t":        (0x1B, "byte"),      # 00 1B 02 00 <tube> <byte>
    "burst_count":    (0x24, "u3be"),      # 00 24 04 00 <tube> <3-byte BE>
    "scent_name":     (0x25, "str_tube"),  # 00 25 <len+1> 00 <tube><name bytes>
    "scent_version":  (0x26, "u3be"),      # 00 26 <len+1> 00 <tube><3-byte>
    "t_max_t":        (0x2F, "u3be"),      # 00 2F <len+1> 00 <tube><3-byte>
    "device_name":    (0x28, "str"),       # 00 28 <len> 00 <name>
    "device_version": (0x29, "str"),       # 00 29 <len> 00 <ver>
    "fill_date":      (0x2A, "str"),       # 00 2A <len> 00 <date>
    "pack_name":      (0x2B, "str"),       # 00 2B <len> 00 <name>
    "cart_serial":    (0x2C, "str"),       # 00 2C <len> 00 <sn>
    "device_serial":  (0x2D, "str"),       # 00 2D <len> 00 <sn>
    "first_use_date": (0x3A, "str"),       # 00 3A <len> 00 <date>
}
SUDO_TYPES = {mtype for mtype, _ in SUDO_WRITES.values()}

BURST_COUNT_MAX = 100_000   # documented cap on the burst counter [N OVRMessageTypes.h:37]
SUDO_STRING_MAX = 32        # sanity cap on string writes; cartridge fields are short


class SecurityError(Exception):
    """Raised when a frame would violate the safety tiers."""


def intensity_byte(pct: float) -> int:
    """Normalized 0.0-1.0 -> device byte, floor(pct*255). (0.75 -> 191.)"""
    return int(max(0.0, min(1.0, float(pct))) * 255)


def clean(value: str) -> str:
    """Device strings are NUL-padded; strip padding without touching interiors."""
    return value.replace("\r", "").strip("\x00 ")


def norm_name(value: str) -> str:
    """Canonical form for scent-name matching.

    Palette keys use underscores ("terra_silva"), the cartridge reports display
    names with spaces ("Terra Silva"). Casefold alone would make every such
    lookup miss, so collapse runs of whitespace AND underscores, then fold case.
    """
    return " ".join(str(value).replace("_", " ").split()).casefold()


def frame(mtype: int, payload: bytes = b"", allow_sudo: bool = False) -> bytes:
    """Build a framed packet `00 <type> <len> 00 <payload>` with tier checks."""
    if mtype in FORBIDDEN_TYPES:
        raise SecurityError(f"message type 0x{mtype:02X} is Tier A — never sent")
    if mtype not in OUTBOUND_ALLOWLIST and not (allow_sudo and mtype in SUDO_TYPES):
        raise SecurityError(
            f"message type 0x{mtype:02X} is forbidden by safety tier "
            f"(Tier B writes need --sudo)")
    return bytes([0x00, mtype, len(payload), 0x00]) + payload


def mix_packet(scents, algorithm="average"):
    """Build the multi-scent play packet: E3 14 <len> 00 [algo][int][name]0A ..."""
    algo = ALGORITHMS[algorithm]
    payload = b""
    for name, pct in scents:
        payload += bytes([algo, intensity_byte(pct)]) + name.encode("utf-8") + bytes([STOP_BYTE])
    return bytes([START_MULTI, ODORANT_COMMANDS, len(payload), 0x00]) + payload


def check_scent_list(scents, algorithm):
    """Validate a scent list BEFORE anything is queued or sent.

    Runs at request arrival so a bad algorithm, oversized mix, or non-numeric
    intensity fails fast even when the burst would only fire later (rate-limited
    window). Intensity range is not checked: intensity_byte clamps by design.
    """
    if not scents:
        raise ValueError('scents must contain at least one [name,intensity] pair')
    if len(scents) > MAX_SCENTS:
        raise ValueError(f"max {MAX_SCENTS} scents can emit at once "
                         f"(got {len(scents)}); split into sequential mixes")
    if algorithm not in ALGORITHMS:
        raise ValueError(f"unknown algorithm {algorithm!r}; "
                         f"choose from {'|'.join(ALGORITHMS)}")
    for name, pct in scents:
        try:
            float(pct)
        except (TypeError, ValueError):
            raise ValueError(f"intensity for {name!r} must be a number, got {pct!r}")

# -------------------------------------------------------- framing (shared) --

def parse_frames(buf: bytearray):
    """Yield complete framed packets from a stream buffer (serial-style)."""
    out = []
    while True:
        # Skip delimiters / garbage until a candidate header is found.
        while buf and (buf[0] == 0x0A or (len(buf) >= 4 and buf[3] != 0x00)):
            buf.pop(0)
        if len(buf) < 4:
            break                       # incomplete header; wait for more bytes
        length = 4 + buf[2]
        if length > 4096:
            buf.pop(0)                  # nonsense length; resync one byte at a time
            continue
        if len(buf) < length:
            break                       # incomplete packet; wait for more bytes
        out.append(bytes(buf[:length]))
        del buf[:length]
    return out

# ------------------------------------------------------------- rate gate ----

class RateGate:
    """Single-lane dispatch rate limiter for scent bursts (--rate/--rate-mode).

    --rate is the minimum SECONDS between device dispatches (0 disables the
    gate entirely). Requests landing while the lane is closed obey the mode:
      FIRST   fire the first, drop every later one until the next slot opens;
      LAST    hold ONE pending request and replace it with each newer arrival —
              newest wins when the slot opens (a superseded waiter is resolved
              with a note, never silently);
      AVERAGE collect a window that opens on the first straggler after a fire
              and runs `rate` seconds; on close, average intensity per channel
              over ALL requests in the window (sum / number of requests), fire
              the TOP 3 channels as one mix packet;
      WAIT    FIFO queue (backlog capped at RATE_WAIT_MAX), one send per slot.
    A lone request arriving with the lane open always fires immediately — no
    mode adds latency when there is nothing to coalesce. Every submit returns
    exactly one reply dict: fired requests carry dispatched/bytes; held ones
    additionally carry a rate_limited note describing what happened. stop
    commands bypass this gate entirely (see cmd_stop).
    """

    def __init__(self, bridge):
        self.bridge = bridge
        self.mode = str(bridge.args.rate_mode).upper()
        self.interval = max(0.0, float(bridge.args.rate))
        self._next_slot = 0.0          # lane reopens at this monotonic time
        self._pending = None           # LAST: the one held waiter
        self._window = []              # AVERAGE: waiters in the open window
        self._window_due = 0.0         # when the AVERAGE window closes
        self._waitq = []               # WAIT: FIFO of waiters
        self._lock = None              # created on first use (needs a loop)
        self._wake = None              # pump wakeup event
        self._pump = None

    def start(self):
        # FIRST drops instead of holding, so it has no state for the pump to
        # wake on — skip the pump in that mode (the lock is still needed).
        if self.interval > 0:
            self._lock = asyncio.Lock()
            self._wake = asyncio.Event()
            if self.mode != "FIRST":
                self._pump = asyncio.create_task(self._pump_loop())
            print(f"[rate] {self.mode} mode: max 1 burst per {self.interval:g}s")

    async def stop(self):
        if self._pump is not None:
            try:
                self._pump.cancel()
                await self._pump
            except (asyncio.CancelledError, Exception):
                pass          # Ctrl-C path runs in a NEW loop: cross-loop cancel raises
            self._pump = None
        # Never leave a held waiter's future unresolved across shutdown.
        outstanding = [self._pending] + self._window + self._waitq
        self._pending, self._window, self._window_due, self._waitq = None, [], 0.0, []
        for w in outstanding:
            if w is not None:
                self._resolve(w, {"dispatched": 0, "rate_limited":
                                  "bridge shutting down; request dropped"})

    # -- submission ----------------------------------------------------------
    async def submit(self, scents, algorithm):
        """Gate one scent dispatch; returns the reply dict for this request."""
        if self.interval <= 0:
            return await self.bridge._dispatch(scents, algorithm)
        waiter = {"fut": asyncio.get_running_loop().create_future(),
                  "scents": scents, "algorithm": algorithm}
        async with self._lock:
            now = time.monotonic()
            open_slot = now >= self._next_slot
            if self.mode == "FIRST":
                if open_slot:
                    await self._fire(waiter)
                    return await waiter["fut"]
                return {"dispatched": 0,
                        "rate_limited": f"FIRST: dropped, lane closed for "
                                        f"{self._next_slot - now:.2f}s more"}
            if self.mode == "LAST":
                if open_slot and self._pending is None:
                    await self._fire(waiter)
                    return await waiter["fut"]
                if self._pending is not None:
                    self._resolve(self._pending, {
                        "dispatched": 0,
                        "rate_limited": "LAST: superseded by a newer request"})
                self._pending = waiter
            elif self.mode == "AVERAGE":
                if open_slot and not self._window:
                    await self._fire(waiter)          # lone arrival is its own average
                    return await waiter["fut"]
                self._window.append(waiter)
                if not self._window_due:
                    self._window_due = now + self.interval
            else:  # WAIT
                if len(self._waitq) >= RATE_WAIT_MAX:
                    raise ValueError(f"WAIT backlog full ({RATE_WAIT_MAX} queued); "
                                     f"slow down or use a different --rate-mode")
                if open_slot and not self._waitq:
                    await self._fire(waiter)
                    return await waiter["fut"]
                self._waitq.append(waiter)
            self._wake.set()
        return await waiter["fut"]

    # -- firing (always under the gate lock; NEVER raises) --------------------
    async def _fire(self, waiter):
        """Dispatch one waiter and close the lane for another interval.
        Device errors land in the waiter's own reply — never as an exception.
        The lane closes from END of the write, so pacing is measured between
        actual device writes regardless of dispatch latency."""
        try:
            res = await self.bridge._dispatch(waiter["scents"], waiter["algorithm"])
        except Exception as e:
            res = {"dispatched": 0, "error": f"dispatch failed: {e}"}
        self._next_slot = time.monotonic() + self.interval
        self._resolve(waiter, res)

    async def bypass(self, scents, algorithm):
        """Immediate dispatch that skips rate limiting but stays write-serialized
        with gate dispatches (stop must be instant, yet never byte-interleave)."""
        if self._lock is None:                 # gate disabled: plain dispatch
            return await self.bridge._dispatch(scents, algorithm)
        async with self._lock:
            return await self.bridge._dispatch(scents, algorithm)

    def _resolve(self, waiter, obj):
        try:
            if not waiter["fut"].done():
                waiter["fut"].set_result(obj)
        except RuntimeError:
            pass      # shutdown across a closed loop: callback can no longer run

    async def _fire_pending(self):                            # LAST slot expiry
        held, self._pending = self._pending, None
        await self._fire(held)

    async def _fire_window(self):                             # AVERAGE window close
        members, self._window, self._window_due = self._window, [], 0.0
        sums, spellings = {}, {}
        for w in members:
            for name, pct in w["scents"]:
                key = norm_name(name)
                sums[key] = sums.get(key, 0.0) + float(pct)
                spellings.setdefault(key, name)               # keep first spelling seen
        n = len(members)
        ranked = sorted(sums.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_SCENTS]
        avg = [[spellings[k], round(v / n, 4)] for k, v in ranked if v > 0]
        note = f"AVERAGE: {n} request(s) averaged over window"
        if not avg:
            for w in members:
                self._resolve(w, {"dispatched": 0, "rate_limited": note + "; all-zero"})
            return
        try:
            res = await self.bridge._dispatch(avg, "average")
        except Exception as e:
            res = {"dispatched": 0, "error": f"averaged dispatch failed: {e}"}
        # An averaged fire is a REAL burst: it must close the lane like any
        # other fire, or two bursts could leave inside one interval.
        self._next_slot = time.monotonic() + self.interval
        res["rate_limited"] = note
        res["channels"] = avg
        for w in members:
            self._resolve(w, dict(res))

    async def _fire_next_wait(self):                          # WAIT slot expiry
        await self._fire(self._waitq.pop(0))                  # _fire resolves the future

    # -- pump ------------------------------------------------------------------
    async def _pump_loop(self):
        while True:
            next_wake = None
            async with self._lock:
                now = time.monotonic()
                if self.mode == "LAST" and self._pending is not None \
                        and now >= self._next_slot:
                    await self._fire_pending()
                elif self.mode == "AVERAGE" and self._window \
                        and now >= self._window_due:
                    await self._fire_window()
                elif self.mode == "WAIT" and self._waitq \
                        and now >= self._next_slot:
                    await self._fire_next_wait()
                due = {"LAST": self._next_slot if self._pending else None,
                       "AVERAGE": self._window_due if self._window else None,
                       "WAIT": self._next_slot if self._waitq else None}[self.mode]
                next_wake = due
            if next_wake is None:
                await self._wake.wait()
            else:
                try:
                    await asyncio.wait_for(self._wake.wait(),
                                           timeout=max(0.0, next_wake - time.monotonic()))
                except asyncio.TimeoutError:
                    pass
            self._wake.clear()


# ------------------------------------------------------------- device base --

class DeviceLink:
    """Persistent device connection with a serialized, plugin-style request queue.

    Exactly ONE framed request may be outstanding at a time (same as the
    plugins' MessageQueueManager). A lock serializes callers; after any
    timeout we drain stragglers so a late reply can't be delivered to the
    next request.
    """

    def __init__(self, bridge):
        self.bridge = bridge
        self.buf = bytearray()
        self.ready = True          # optimistic at connect, per plugin behavior
        self.sleeping = False
        self.pending = None        # (mtype, expected_resp_type, future)
        self._lock = None
        self._need_drain = False   # a previous attempt timed out; stragglers possible

    def _ensure_lock(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def feed(self, data: bytes):
        """Feed raw inbound bytes; dispatch each complete frame."""
        self.buf.extend(data)
        for pkt in parse_frames(self.buf):
            await self.handle_frame(pkt)

    async def handle_frame(self, pkt: bytes):
        mtype = pkt[1]
        payload = pkt[4:]
        # Any successful inbound message clears the sleep flag
        # [U ConnectionManager.cs:327, 4268-4297].
        if self.sleeping and mtype != ERROR_STATUS:
            self.sleeping = False
        # 1) A response to our outstanding request? Deliver and stop.
        if self.pending is not None and self.pending[1] == mtype:
            _, _, fut = self.pending
            self.pending = None
            if not fut.done():
                fut.set_result((mtype, payload))
            return
        # 2) Otherwise it's an unsolicited push.
        if mtype == ERROR_STATUS:
            self.bridge.on_error_status(payload)
        elif mtype in RESP_STRING:
            self.bridge.on_device_field(RESP_STRING[mtype], clean(payload.decode("ascii", "replace")))
        elif mtype == 0x0D:
            self.bridge.on_battery(payload[0] if payload else 0)
        elif mtype in (0x0F, 0x12):
            pass                       # state / pressure echo, informational
        elif mtype in (0x01, 0x02):
            pass                       # INFO / app-connect
        else:
            self.bridge.on_unknown(pkt)

    def note_anomaly(self):
        """A response didn't match what was asked for; expect stragglers."""
        self._need_drain = True

    async def drain_stale(self):
        """Let stragglers from a bad attempt arrive while nothing is expected."""
        self.pending = None
        await asyncio.sleep(STALE_DRAIN_S)
        self._need_drain = False

    async def request(self, mtype: int, payload: bytes = b"", timeout: float = DEFAULT_TIMEOUT,
                      resp_type: int = None):
        """Send one framed request, wait for the matching response.

        Response type is normally request+1; pass resp_type explicitly for the
        documented exception (the 0x2E RGB query answers on its own id).
        """
        async with self._ensure_lock():
            if self._need_drain:
                await self.drain_stale()
            return await self._request_locked(mtype, payload, timeout, resp_type)

    async def _request_locked(self, mtype: int, payload: bytes, timeout: float,
                              resp_type: int = None):
        loop = asyncio.get_running_loop()
        expected = resp_type if resp_type is not None else mtype + 1
        fut = loop.create_future()
        self.pending = (mtype, expected, fut)
        await self.write(frame(mtype, payload))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            # The device may still answer attempt 1 — and possibly BOTH attempts.
            self._need_drain = True
        # One retry with a FRESH future (wait_for cancelled the old one).
        if self.pending is not None and self.pending[2] is not fut:
            raise TimeoutError(f"request 0x{mtype:02X} superseded")
        fut = loop.create_future()
        self.pending = (mtype, expected, fut)
        try:
            await self.write(frame(mtype, payload))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            if self.pending is not None and self.pending[2] is fut:
                self.pending = None
            self._need_drain = True
            raise TimeoutError(f"request 0x{mtype:02X} got no response after retry")

    async def send_fire_and_forget(self, data: bytes):
        await self.write(data)

    # subclass hooks ---------------------------------------------------------
    async def write(self, data: bytes):
        raise NotImplementedError

    async def wake_if_sleeping(self):
        if self.sleeping:
            await self.write(frame(DEVICE_SET_STATE, bytes([SERIAL_MODE_VALUE])))
            self.sleeping = False

    async def close(self):
        pass


class BLELink(DeviceLink):
    """Bluetooth LE transport (bleak). Subscribes to the four ION chars."""

    def __init__(self, bridge, address):
        super().__init__(bridge)
        self.address = address
        self.client = None

    async def connect(self):
        from bleak import BleakClient
        self.client = BleakClient(self.address, timeout=30.0)
        await self.client.connect()

        def mk(label):
            def cb(_, data: bytearray):
                asyncio.create_task(self._notify(label, bytes(data)))
            return cb

        await self.client.start_notify(NOTIFY_CHAR, mk("data"))
        # Optional subscriptions are tolerated if the stack refuses them.
        for char, label in ((READY_CHAR, "ready"), (ERROR_CHAR, "error-status"),
                            (BATTERY_CHAR, "battery")):
            try:
                await self.client.start_notify(char, mk(label))
            except Exception as e:
                print(f"[ble] {label} subscription failed: {e}")

    async def _notify(self, label, data: bytes):
        if label == "data":
            await self.feed(data)                  # framed protocol packets
        elif label == "ready" and data:
            self.ready = data[0] == 1
            self.bridge.on_ready(self.ready)
        elif label == "error-status" and data:
            self.bridge.on_error_status(data, characteristic=True)
        elif label == "battery" and data:
            self.bridge.on_battery_percent(data[0])

    async def write(self, data: bytes):
        await self.client.write_gatt_char(WRITE_CHAR, data, response=True)

    async def close(self):
        if self.client is not None:
            await self.client.disconnect()


class SerialLink(DeviceLink):
    """USB serial transport (pyserial) with the plugin handshake."""

    def __init__(self, bridge, port, baud=115200):
        super().__init__(bridge)
        import serial
        self.serial = serial.Serial(port, baud, timeout=0.05)
        self.port = port

    async def connect(self):
        # Reader FIRST so handshake replies are actually parsed.
        self._reader = asyncio.create_task(self._read_loop())
        await self.write(frame(DEVICE_SET_STATE, bytes([SERIAL_MODE_VALUE])))
        await asyncio.sleep(0.06)
        try:
            mtype, payload = await self.request(0x0E, timeout=1.5)
        except TimeoutError as e:
            raise RuntimeError("serial handshake got no response "
                               "(is the Omara powered and idle?)") from e
        state = payload[0] if payload else None
        # Unreal accepts 0x01 OR 0x02 as verified (N SerialDevice.cpp:678-680).
        if state not in (SERIAL_MODE_VALUE, 0x01):
            raise RuntimeError(f"serial handshake failed (state={state!r})")
        self.ready = True

    async def _read_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            data = await loop.run_in_executor(None, lambda: self.serial.read(64))
            if data:
                await self.feed(data)

    async def write(self, data: bytes):
        def _w():
            self.serial.write(data)
            self.serial.flush()
        await asyncio.get_running_loop().run_in_executor(None, _w)

    async def close(self):
        # Exit serial mode (SET_STATE=0) so the device re-advertises on BLE.
        try:
            await self.write(frame(DEVICE_SET_STATE, bytes([0x00])))
            await asyncio.sleep(0.2)
        except Exception as e:
            print(f"[serial] graceful SET_STATE=0 not delivered: {e}")
        reader = getattr(self, "_reader", None)
        if reader is not None:
            try:
                reader.cancel()      # may belong to a dead loop after Ctrl-C
            except Exception:
                pass
        try:
            self.serial.close()
        except Exception:
            pass

# ---------------------------------------------------------------- bridge ----

class Bridge:
    """WebSocket server + device command layer. All complexity lives here."""

    def __init__(self, args):
        self.args = args
        self.link = None
        self.ws_clients = set()
        self.fields = {}             # last known values of string device fields
        self.battery_percent = None
        self._last_battery_raw = None
        self._sleeping_reported = False
        self._closed = False
        # ---- loaded-name cache (warmed at startup, invalidated by evidence) --
        self.names_cache = {}        # norm_name -> exact spelling from the device
        self.tube_index = {}         # norm_name -> tube index (same sweep)
        self.names_at = 0.0          # monotonic time the cache was filled; 0 = never
        self._names_stale = False    # evidence says: re-read before next use
        self._names_last_auto = 0.0  # monotonic time of last AUTOMATIC re-read
        self._pack_id = {}           # pack/cart identity the cache was built against
        self._last_dispatch = None   # (monotonic time, [names sent])
        self.gate = RateGate(self)   # scent-dispatch rate limiter (--rate/--rate-mode)

    @property
    def tube_count(self) -> int:
        """Channel ceiling used by every sweep/backup (default 16)."""
        return int(self.args.tubes or DEFAULT_TUBES)

    # ---- client fan-out ---------------------------------------------------
    def broadcast(self, obj: dict):
        """Push an event to every connected client; reap failures quietly."""
        msg = json.dumps(obj)
        for ws in list(self.ws_clients):
            try:
                fut = asyncio.ensure_future(ws.send(msg))
                fut.add_done_callback(lambda f, w=ws: self._reap_send(f, w))
            except Exception:
                self.ws_clients.discard(ws)

    def _reap_send(self, fut, ws):
        """Retrieve the exception so asyncio never logs 'never retrieved'."""
        try:
            fut.result()
        except Exception:
            self.ws_clients.discard(ws)

    # ---- device event handlers (called from DeviceLink) -------------------
    def on_ready(self, ready: bool):
        print(f"[device] ready={ready}")
        self.broadcast({"event": "ready", "ready": ready})

    def on_battery(self, raw: int):
        pct = max(0, min(100, round(raw * 100 / 255)))
        if raw == self._last_battery_raw and self.battery_percent is not None:
            return                      # periodic heartbeat repeat; stay quiet
        self._last_battery_raw = raw
        self.battery_percent = pct
        print(f"[device] battery {pct}% (raw={raw})")
        self.broadcast({"event": "battery", "percent": pct, "raw": raw})

    def on_battery_percent(self, pct: int):
        self.battery_percent = max(0, min(100, pct))
        self.broadcast({"event": "battery", "percent": self.battery_percent})

    def on_error_status(self, data: bytes, characteristic: bool = False):
        """ERROR block 0x3C / GATT error char: len==0 means no active error."""
        sleeping = SLEEP_ERROR_BYTE in data
        if self.link is not None:
            self.link.sleeping = sleeping
        # An actual fault landing right after we dispatched a burst whose names
        # came from the cache is the only post-hoc evidence that the cache lied
        # (e.g. the pack was swapped). Sleeping is not a fault; ignore it.
        if data and not sleeping:
            self._suspect_names_after_dispatch(data)
        changed = sleeping != self._sleeping_reported
        self._sleeping_reported = sleeping
        if not data and not changed:
            return                      # routine "no error" heartbeat: stay quiet
        src = " via GATT char" if characteristic else ""
        print(f"[device] error-status {data.hex() or '(empty)'} sleep={sleeping}{src}")
        self.broadcast({"event": "error", "bytes": data.hex(), "sleeping": sleeping})

    def on_device_field(self, field: str, value: str):
        self.fields[field] = value
        print(f"[device] {field} = {value!r}")
        # A spontaneously reported pack/cart serial that differs from the pair
        # the name cache was built against means a different cartridge is in.
        if self.names_at and field in PACK_ID_FIELDS:
            known = self._pack_id.get(field)
            if known is not None and known != value:
                self.mark_names_stale(f"{field} changed {known!r} -> {value!r}")
                self._pack_id[field] = value      # don't re-fire on the same push
        self.broadcast({"event": "field", "field": field, "value": value})

    def on_unknown(self, pkt: bytes):
        print(f"[device] unhandled packet {pkt.hex()}")

    # ---- loaded-name cache ---------------------------------------------------
    def mark_names_stale(self, reason: str):
        """Flag the cache as suspect; the next lookup re-reads (within the guard)."""
        if not self._names_stale:
            print(f"[names] cache marked stale ({reason})")
        self._names_stale = True

    def _refresh_allowed(self) -> bool:
        """Storm guard: automatic re-reads are rate-limited, explicit ones aren't."""
        return (time.monotonic() - self._names_last_auto) >= NAMES_REFRESH_MIN_S

    async def _sweep_names(self):
        """Read every channel's scent name once and rebuild the cache."""
        fresh_names, fresh_index = {}, {}
        for t in range(self.tube_count):
            try:
                n = await self._read_field("scent_name", t)
            except Exception:
                continue                     # empty/absent channel never answers
            n = (n or "").strip()
            if n:
                fresh_names[norm_name(n)] = n
                fresh_index[norm_name(n)] = t
            await asyncio.sleep(REQUEST_GAP_S)
        self.names_cache = fresh_names
        self.tube_index = fresh_index
        self.names_at = time.monotonic()
        self._names_last_auto = self.names_at
        self._names_stale = False
        # Record which pack these names belong to, so a later spontaneous push
        # can tell us the cartridge changed. Free: no extra reads if we already
        # know them (warm-up queries once, then it rides on device pushes).
        for what in PACK_ID_FIELDS:
            if self.fields.get(what) is None:
                try:
                    await self.cmd_query(what)
                except Exception as e:
                    print(f"[names] pack identity query {what} failed: {e}")
        self._pack_id = {f: self.fields.get(f) for f in PACK_ID_FIELDS}
        print(f"[names] {len(self.names_cache)} loaded: "
              f"{sorted(self.names_cache.values())} "
              f"(pack={self._pack_id.get('pack_name')!r}, "
              f"sn={self._pack_id.get('cart_serial')!r})")

    async def names_table(self, force: bool = False, reason: str = ""):
        """Return the cached name table, sweeping only on evidence or request."""
        if self.names_at and not force and not self._names_stale:
            return self.names_cache
        if not force and not self._refresh_allowed():
            print(f"[names] stale ({reason or 'auto'}) but re-reading too soon; "
                  f"using cached {len(self.names_cache)} name(s)")
            return self.names_cache
        await self._sweep_names()
        return self.names_cache

    async def resolve_name(self, name: str):
        """(exact spelling, tube index) for a palette name, or None.

        A name that isn't in the table is the classic pack-swap signature, so we
        re-read once and retry inline — the caller still sends one message.
        """
        key = norm_name(name)
        await self.names_table()
        if key in self.names_cache:
            return self.names_cache[key], self.tube_index[key]
        if self._refresh_allowed():
            self.mark_names_stale(f"unknown name {name!r}")
            await self.names_table(reason=f"unknown name {name!r}")
            if key in self.names_cache:
                return self.names_cache[key], self.tube_index[key]
        return None

    def _suspect_names_after_dispatch(self, data: bytes):
        """A fault shortly after a burst means what we thought was loaded isn't."""
        if not self._last_dispatch or not self.names_at:
            return
        when, names = self._last_dispatch
        age = time.monotonic() - when
        if age <= DISPATCH_SUSPECT_WINDOW_S:
            self.mark_names_stale(f"device error {data.hex()} "
                                  f"{age:.1f}s after burst {names}")
        self._last_dispatch = None

    # ---- field readers ------------------------------------------------------
    def _decode_field(self, what: str, p: bytes):
        """Decode a raw response payload for the named field."""
        if what == "scent_name":
            return clean(p[1:].decode("ascii", "replace")) if len(p) > 1 else ""
        if what in ("firmware", "device_name", "device_serial", "device_version",
                    "fill_date", "cart_serial", "pack_name", "first_use_date"):
            return clean(p.decode("ascii", "replace"))
        if what == "s_max_t":
            # Unity reads payload[1] as 1 byte; Unreal reads payload[1..3] BE.
            # Branch on the declared length so both wire forms parse.
            return (int.from_bytes(p[1:4], "big") if len(p) >= 4
                    else (p[1] if len(p) > 1 else None))
        if what == "burst_count":
            # This unit answers burst counts in the full 3-byte BE form; keep strict.
            return int.from_bytes(p[1:4], "big") if len(p) >= 4 else None
        if what in ("t_max_t", "scent_version"):
            # Two wire forms exist (Unreal 3-byte BE, Unity 1 byte). fw 3.4.0
            # answers short — accept both instead of silently returning None.
            if len(p) >= 4:
                return int.from_bytes(p[1:4], "big")
            if len(p) > 1:
                print(f"[device] {what}: short response {p.hex()}; using 1-byte form")
                return p[1]
            return None
        if what == "channel_rgb":
            return [p[1], p[2], p[3]] if len(p) >= 4 else None
        raise ValueError(f"no decoder for field {what!r}")

    async def _read_field(self, what: str, t: int = None):
        """Read one persistent field (device-level, or per-tube when t given).

        Per-tube responses begin with the tube index byte; a mismatch means a
        stray from an earlier attempt was delivered here — flag it, don't store
        it. Exception: fields in TUBE_ECHO_EXEMPT (S_MAX_T on fw 3.4.0) answer
        with a non-echoed tube byte (0xFF); the serialized request queue makes
        such a frame unambiguous, so we accept and log instead of raising.
        """
        if what not in FIELD_READS:
            raise ValueError(f"unknown field {what!r}")
        mtype, rtype = FIELD_READS[what]
        payload = bytes([t]) if t is not None else b""
        _, p = await self.link.request(mtype, payload, timeout=SWEEP_TIMEOUT, resp_type=rtype)
        if t is not None:
            if not p:
                self.link.note_anomaly()
                raise ValueError(f"empty response for {what!r} tube {t}")
            if p[0] != t and what not in TUBE_ECHO_EXEMPT:
                self.link.note_anomaly()
                raise ValueError(f"device answered for tube {p[0]}, expected {t}")
            if p[0] != t:
                print(f"[device] {what} tube {t}: device echoed tube {p[0]} "
                      f"(known fw quirk, accepted)")
        return self._decode_field(what, p)

    async def _find_tube(self, name: str):
        """Resolve a loaded scent name to its tube index (cache; no blind walk)."""
        hit = await self.resolve_name(name)
        if hit is None:
            raise ValueError(f"no loaded tube named {name!r}; "
                             f"loaded: {sorted(self.names_cache.values())}")
        return hit[1]

    # ---- commands -----------------------------------------------------------
    async def _dispatch(self, scents, algorithm="average"):
        """The single device-write path for scent packets (gate-bypassable)."""
        await self.link.wake_if_sleeping()
        if not self.link.ready and not self.args.queue_while_busy:
            raise RuntimeError("device busy (not ready); pass queue_while_busy or retry")
        pkt = mix_packet(scents, algorithm)
        await self.link.send_fire_and_forget(pkt)
        # Remember what we just sent so a following device fault can invalidate
        # the name cache (the burst is unacknowledged; errors are our only signal).
        self._last_dispatch = (time.monotonic(), [n for n, _ in scents])
        return {"dispatched": len(scents), "bytes": pkt.hex()}

    async def cmd_mix(self, scents, algorithm="average"):
        """UNVERIFIED raw fast path (names go out as given); rate-gated."""
        check_scent_list(scents, algorithm)
        return await self.gate.submit(scents, algorithm)

    async def cmd_stop(self, names):
        """Stop channels by name at intensity 0. NEVER rate-limited: stopping
        emission must always be immediate, even ahead of a full WAIT backlog."""
        scents = [(n, 0.0) for n in names]
        check_scent_list(scents, "average")
        return await self.gate.bypass(scents, "average")

    async def cmd_spray(self, scents, algorithm="average", refresh_names=False):
        """ONE message = resolve + fire. This is what clients should send.

        Names are matched against the cartridge's own spelling (case/underscore
        insensitive) from the startup cache; nothing is read from the device in
        the normal case. Picks that aren't loaded are skipped and reported in
        "skipped"; the request only fails if NONE of them resolve, in which case
        no frame is sent at all. The dispatch itself passes through the rate
        gate (see RateGate).
        """
        if not scents:
            raise ValueError('spray requires "scents": [[name,pct],...]')
        check_scent_list(scents, algorithm)     # >3 or bad algo fails at arrival
        if refresh_names:
            self.mark_names_stale("client requested refresh")
            await self.names_table(force=True, reason="client requested refresh")
        resolved, skipped = [], []
        for name, pct in scents:
            hit = await self.resolve_name(name)
            if hit is None:
                skipped.append(str(name))
            else:
                resolved.append((hit[0], pct))
        if not resolved:
            raise ValueError(f"none of {skipped} are loaded on this cartridge; "
                             f"loaded: {sorted(self.names_cache.values())}")
        out = await self.cmd_mix(resolved, algorithm)
        out["skipped"] = skipped
        return out

    async def cmd_query(self, what: str):
        if what not in REQ:
            raise ValueError(f"unknown query {what!r}; try: {sorted(REQ)}")
        mtype, payload = await self.link.request(REQ[what])
        if mtype == 0x0D:
            raw = payload[0] if payload else 0
            return {"battery_raw": raw, "percent": round(raw * 100 / 255)}
        if mtype == 0x0F:
            return {"state": payload[0] if payload else None}
        if mtype == 0x12:
            return {"pressure_raw": payload[0] if payload else None}
        key = RESP_STRING.get(mtype, what)
        return {key: clean(payload.decode("ascii", "replace"))}

    async def cmd_tubes(self, count=None, deep=False):
        """Per-tube sweep over all 16 channels by default.

        Shallow rows: name / burst_count / max_t (legacy keys, stable).
        Deep adds t_max_t / scent_version / channel_rgb (the MFG reads).
        Absent channels answer nothing; that's normal and recorded as *_error.
        This is a DIAGNOSTIC sweep — sprays never call it.
        """
        n = int(count) if count else self.tube_count
        out = []
        for t in range(n):
            row = {"tube": t}
            try:
                row["name"] = await self._read_field("scent_name", t) or ""
            except Exception as e:
                row["name_error"] = str(e)
            await asyncio.sleep(REQUEST_GAP_S)
            try:
                row["burst_count"] = await self._read_field("burst_count", t)
            except Exception as e:
                row["burst_error"] = str(e)
            await asyncio.sleep(REQUEST_GAP_S)
            try:
                row["max_t"] = await self._read_field("s_max_t", t)
            except Exception as e:
                row["max_t_error"] = str(e)
            await asyncio.sleep(REQUEST_GAP_S)
            if deep:
                for what in ("t_max_t", "scent_version", "channel_rgb"):
                    try:
                        row[what] = await self._read_field(what, t)
                    except Exception as e:
                        row[what + "_error"] = str(e)
                    await asyncio.sleep(REQUEST_GAP_S)
            out.append(row)
        return out

    async def cmd_backup_calibration(self, path=None, count=None):
        """READ-ONLY snapshot of every persistent field. Never writes to device.

        Device strings (all of them have sudo-writable counterparts), battery
        (transient, informational), and a DEEP per-tube sweep: scent name,
        burst count, S_MAX_T, T_MAX_T scalar, scent version/freq, RGB — i.e.
        everything readable that could be written, so every sudo_write has a
        restore value in this file.
        """
        snap = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "device": {}, "tubes": []}
        for what in ("name", "serial", "firmware", "version", "pack_name",
                     "fill_date", "first_use_date", "cart_serial"):
            try:
                snap["device"].update(await self.cmd_query(what))
            except Exception as e:
                snap["device"][what + "_error"] = str(e)
        try:
            snap["device"].update(await self.cmd_query("battery"))
        except Exception as e:
            snap["device"]["battery_error"] = str(e)
        snap["tubes"] = await self.cmd_tubes(count, deep=True)
        p = Path(path or f"omara_calibration_backup_{time.strftime('%Y%m%d_%H%M%S')}.json")
        p.write_text(json.dumps(snap, indent=2))
        return {"saved": str(p.resolve()), "snapshot": snap}

    async def cmd_set_max_t(self, tube=None, name=None, value=None):
        """Tier B write — ONLY when the bridge was started with --sudo.

        Sets the per-tube S_MAX_T emission-cap byte (0x1B). Sugar over
        sudo_write for the common case; same read-old / write / read-back flow.
        The scale on fw 3.4.0 is UNVERIFIED (this device reports 72), so change
        it in small steps and restore the reported old value when done.
        "name" resolves from the cached tube index — no sweep.
        """
        if not self.args.sudo:
            raise SecurityError("set_max_t requires the bridge to run with --sudo")
        if tube is None and name is not None:
            tube = await self._find_tube(name)
        return await self.cmd_sudo_write("s_max_t", value, tube)

    async def cmd_sudo_write(self, what=None, value=None, tube=None):
        """Tier B write of any documented persistent field. Requires --sudo.

        Reads the current value first, writes, re-reads, and reports all three —
        so every change is reversible from the reply or the backup file.
        Per-tube fields need "tube" (0-15); device-level fields don't.
        """
        if not self.args.sudo:
            raise SecurityError("sudo writes require the bridge to run with --sudo")
        if what not in SUDO_WRITES:
            raise ValueError(f"unknown field {what!r}; writable: {sorted(SUDO_WRITES)}")
        mtype, kind = SUDO_WRITES[what]
        per_tube = what in PER_TUBE_FIELDS
        if per_tube and (tube is None or not 0 <= int(tube) < self.tube_count):
            raise ValueError(f"field {what!r} needs a valid \"tube\" index "
                             f"0-{self.tube_count - 1}")
        tube = int(tube) if per_tube else None

        # --- validate + encode the value by kind (before any device I/O) ----
        if kind == "byte":
            v = int(value)
            if not 0 <= v <= 255:
                raise ValueError("value must be a byte 0-255 (physical wire limit)")
            payload = bytes([tube, v])
            expected = v
        elif kind == "u3be":
            v = int(value)
            if not 0 <= v <= 0xFFFFFF:
                raise ValueError("value must fit 3 bytes (0-16777215)")
            if what == "burst_count" and v > BURST_COUNT_MAX:
                raise ValueError(f"burst_count is capped at {BURST_COUNT_MAX} "
                                 f"(documented device limit)")
            payload = bytes([tube]) + v.to_bytes(3, "big")
            expected = v
        else:  # "str" / "str_tube"
            s = clean(str(value))
            if not s or len(s) > SUDO_STRING_MAX:
                raise ValueError(f"value must be 1-{SUDO_STRING_MAX} chars of ASCII")
            b = s.encode("ascii")      # raises if non-ASCII; caught below
            payload = bytes([tube]) + b if kind == "str_tube" else b
            expected = s

        # --- read old, write, read back --------------------------------------
        old = await self._read_field(what, tube)
        # None of these writes has a response type — verify by re-reading.
        await self.link.write(frame(mtype, payload, allow_sudo=True))
        await asyncio.sleep(0.25)
        new = await self._read_field(what, tube)
        where = f"tube={tube}" if per_tube else "device"
        print(f"[sudo] write {what} ({where}) old={old!r} requested={value!r} readback={new!r}")
        # A name/pack/cart serial write can change what is loaded or which pack
        # is in place; never let a stale cache resolve the next spray. (Writes
        # bypass the storm guard: they are deliberate, not repeated by accident.)
        self.mark_names_stale(f"sudo write {what}")
        self._names_last_auto = 0.0
        return {"field": what, "tube": tube, "old": old, "requested": value,
                "readback": new, "applied": (new == expected)}

    # ---- WS server -----------------------------------------------------------
    def _auth_header(self, ws) -> str:
        """Authorization header across websockets versions (legacy API used
        request_headers; 14+ moved them to ws.request.headers). The Headers
        object is case-insensitive — do NOT dict() it before .get()."""
        try:
            return ws.request.headers.get("Authorization", "")
        except AttributeError:
            pass
        try:                                 # legacy websockets (<14) API
            return ws.request_headers.get("Authorization", "")
        except Exception:
            return ""

    async def handler(self, ws):
        if self.args.token and self._auth_header(ws) != f"Bearer {self.args.token}":
            await ws.send(json.dumps({"ok": False, "error": "unauthorized"}))
            await ws.close()
            return
        self.ws_clients.add(ws)
        print(f"[ws] client connected ({len(self.ws_clients)})")
        try:
            await ws.send(json.dumps({"event": "hello", "bridge": "omara-local",
                                      "protocol": 1,
                                      "device_connected": self.link is not None}))
            async for raw in ws:
                await self.dispatch(ws, raw)
        except WS_CLOSED:
            pass
        finally:
            self.ws_clients.discard(ws)
            print(f"[ws] client left ({len(self.ws_clients)})")

    async def dispatch(self, ws, raw):
        """Route one client message to a command; always reply exactly once."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await self._reply(ws, {"ok": False, "error": "Invalid JSON payload."})
            return
        resp = {"ok": True}
        try:
            if "odor" in msg and "cmd" not in msg:          # v1 shorthand
                # Same resolver as "spray": a blind mix of an unloaded name is a
                # wasted burst, so verify against the cache before sending.
                resp.update(await self.cmd_spray(
                    [(msg["odor"], msg.get("intensity", 1.0))]))
            else:
                cmd = msg.get("cmd")
                if cmd == "status":
                    resp.update({"device_connected": self.link is not None,
                                 "ready": self.link.ready if self.link else None,
                                 "sleeping": self.link.sleeping if self.link else None,
                                 "battery_percent": self.battery_percent,
                                 "names_loaded": sorted(self.names_cache.values()),
                                 "names_stale": self._names_stale,
                                 "fields": self.fields})
                elif cmd == "spray":
                    resp.update(await self.cmd_spray(
                        [(s[0], s[1]) for s in msg.get("scents", [])],
                        msg.get("algorithm", "average"),
                        bool(msg.get("refresh_names"))))
                elif cmd == "names":
                    await self.names_table(force=bool(msg.get("refresh")),
                                           reason="client names query")
                    resp["names"] = sorted(self.names_cache.values())
                    resp["tubes"] = {self.names_cache[k]: self.tube_index[k]
                                     for k in self.names_cache}
                elif cmd == "mix":
                    scents = [(s[0], s[1]) for s in msg.get("scents", [])]
                    resp.update(await self.cmd_mix(scents, msg.get("algorithm", "average")))
                elif cmd == "stop":
                    names = msg.get("scents")
                    if not names:
                        raise ValueError('stop requires "scents": [names]')
                    resp.update(await self.cmd_stop(names))
                elif cmd == "query":
                    resp.update(await self.cmd_query(msg.get("what", "")))
                elif cmd == "tubes":
                    resp["tubes"] = await self.cmd_tubes(
                        msg.get("count"), deep=bool(msg.get("deep")))
                elif cmd == "backup_calibration":
                    resp.update(await self.cmd_backup_calibration(
                        msg.get("path"), msg.get("count")))
                elif cmd == "set_max_t":
                    resp.update(await self.cmd_set_max_t(
                        msg.get("tube"), msg.get("name"), msg.get("value")))
                elif cmd == "sudo_write":
                    resp.update(await self.cmd_sudo_write(
                        msg.get("what"), msg.get("value"), msg.get("tube")))
                elif cmd == "ping":
                    resp.update(await self.cmd_query("name"))
                else:
                    raise ValueError(f"unknown cmd {cmd!r}")
        except SecurityError as e:
            resp = {"ok": False, "error": f"SAFETY: {e}"}
        except Exception as e:
            resp = {"ok": False, "error": str(e)}
        # A fire that happened later (rate gate) can still fail on the device;
        # its error lands in the merged reply dict — keep ok consistent.
        if "error" in resp:
            resp["ok"] = False
        await self._reply(ws, resp)

    async def _reply(self, ws, obj: dict):
        try:
            await ws.send(json.dumps(obj))
        except WS_CLOSED:
            print("[ws] client vanished before the reply was delivered")

    # ---- lifecycle -------------------------------------------------------------
    async def warm_names(self):
        """Populate the loaded-name table once, before any client is served."""
        try:
            await self.names_table(force=True, reason="startup")
        except Exception as e:
            # Never let a slow/awkward cartridge keep the bridge from starting:
            # the first spray re-sweeps instead.
            print(f"[names] startup sweep failed ({e}); first spray will retry")

    async def run(self):
        if self.args.ble:
            self.link = BLELink(self, self.args.ble)
        elif self.args.serial:
            self.link = SerialLink(self, self.args.serial)
        else:
            sys.exit("a device transport is required: --ble ADDRESS or --serial PORT")
        await self.link.connect()
        print(f"[bridge] device connected via {'BLE' if self.args.ble else 'serial'}")
        self.gate.start()
        if not self.args.no_warm_names:
            await self.warm_names()
        if self.args.sudo:
            print("[bridge] SUDO MODE: all documented Tier B writes ENABLED "
                  f"({len(SUDO_WRITES)} fields) — firmware/OTA still impossible")

        import websockets
        ctx = None
        if self.args.cert and self.args.key:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(self.args.cert, self.args.key)
            print("[bridge] TLS enabled (wss)")
        try:
            async with websockets.serve(self.handler, self.args.host, self.args.port, ssl=ctx):
                scheme = "wss" if ctx else "ws"
                print(f"[bridge] serving {scheme}://{self.args.host}:{self.args.port}")
                await asyncio.Future()
        except SystemExit:
            raise
        except Exception:
            await self.shutdown()
            raise

    async def shutdown(self):
        if self._closed:
            return
        self._closed = True
        await self.gate.stop()
        if self.link is None:
            return
        try:
            await asyncio.wait_for(self.link.close(), timeout=2.0)
            print("[bridge] device closed gracefully (SET_STATE=0 sent)")
        except Exception as e:
            print(f"[bridge] graceful device close incomplete: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    dev = ap.add_argument_group("device transport (exactly one, local only)")
    dev.add_argument("--ble", metavar="ADDRESS", help="BLE address of Omara device")
    dev.add_argument("--serial", metavar="PORT", help="serial port (COMx or /dev/cu.*)")
    wsg = ap.add_argument_group("websocket server")
    wsg.add_argument("--host", default="127.0.0.1")
    wsg.add_argument("--port", type=int, default=8765)
    wsg.add_argument("--cert", help="TLS certificate PEM (enables wss with --key)")
    wsg.add_argument("--key", help="TLS private key PEM")
    wsg.add_argument("--token", help="require Authorization: Bearer <token>")
    beh = ap.add_argument_group("behavior")
    beh.add_argument("--tubes", type=int, default=DEFAULT_TUBES,
                     help=f"channel count for sweeps/backup (default {DEFAULT_TUBES})")
    beh.add_argument("--no-warm-names", action="store_true",
                     help="skip the startup name sweep; the first spray pays for it")
    beh.add_argument("--queue-while-busy", action="store_true",
                     help="allow scent dispatch while device reports busy (default: refuse)")
    beh.add_argument("--rate", type=float, default=0.0, metavar="SECONDS",
                     help="minimum seconds between device scent bursts; "
                          "0 (default) disables rate limiting")
    beh.add_argument("--rate-mode", default="WAIT", choices=["FIRST", "LAST", "AVERAGE", "WAIT"],
                     help="what to do with requests landing inside a closed window: "
                          "FIRST keeps the first and drops the rest; LAST keeps only "
                          "the newest; AVERAGE averages intensity per channel over the "
                          "window and fires the top 3 as one mix; WAIT queues them all "
                          "(default). Ignored while --rate is 0.")
    beh.add_argument("--sudo", action="store_true",
                     help="enable all documented Tier B writes via sudo_write/set_max_t "
                          "(names, serials, dates, counts, per-tube scalars). "
                          "Firmware/OTA remain impossible. Back up calibration first.")
    args = ap.parse_args()
    if not ((args.ble is None) ^ (args.serial is None)):
        sys.exit("give exactly one of --ble / --serial")
    if args.rate < 0:
        sys.exit("--rate must be >= 0 seconds")

    bridge = Bridge(args)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\n[bridge] shutting down")
        try:
            asyncio.run(bridge.shutdown())
        except Exception as e:
            print(f"[bridge] close failed: {e} — unplug/replug if you need BLE to re-advertise")


if __name__ == "__main__":
    main()