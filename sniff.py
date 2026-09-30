#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
#
# SoothingSpaces sniffer
# Copyright (C) 2026 Mark J. Kuebel
"""Pick Omara cartridges for content using a local model server.

    bridge.py handler   -> Sniffer.sniff(text)         -> one scent for a subject
    session.py          -> Sniffer.sniff_mix(text, n)  -> an n-scent blend
    py sniff.py --text "pine cabin after rain"         (standalone test)

One JSON-schema-constrained call per request: the model returns
{odor, intensity, why} (or a scents array for mixes); code validates and
clamps. The palette file is hot-reloaded on edit so tuning never needs a
restart.

This module also carries the GENERAL SCENT-MIXING MECHANIC shared by every
caller: normalize_mix() validates/merges/orders several scents into one blend
(highest first), keep_strongest()/drop_least() filter it, and sniff_mix() asks
the model for an n-scent complementary blend in one call. The omara bridge
sprays every member of a mix — each channel at its own intensity, resolved
against the tubes actually loaded on the cartridge.

Speaks the OpenAI-compatible /chat/completions API: any local server works —
a llama.cpp/llama-server style endpoint or Ollama's OpenAI layer — with one
code path. Images are sent as data URLs; the model must be a vision-capable
one for them to matter.

Auth is optional: the SOOTHINGSPACES_API_KEY env var, else a local Studio
agent key file if one exists on this machine. No token is ever printed.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Callable

log = logging.getLogger("soothingspaces.sniff")

VALID_ODORS = [
    "marine", "petrichor", "kindred", "beach", "floral", "sweet", "barnyard",
    "winter", "evergreen", "terra_silva", "citrus", "desert", "savory_spice",
    "timber", "smoky", "machina",
]

DEFAULT_BASE_URL = "http://127.0.0.1:8888/v1"   # Studio; Ollama's OpenAI API is /v1 too
STUDIO_KEY_FILE = os.path.expandvars(
    r"C:\Users\%USERNAME%\.unsloth\studio\auth\agent_api_key.json")

DISPLAY_TO_KEY = {
    "winter": "winter", "barnyard": "barnyard", "sweet": "sweet",
    "floral": "floral", "beach": "beach", "kindred": "kindred",
    "petrichor": "petrichor", "marine": "marine", "evergreen": "evergreen",
    "terra silva": "terra_silva", "citrus": "citrus", "desert": "desert",
    "savory spice": "savory_spice", "timber": "timber", "smoky": "smoky",
    "machina": "machina",
}

SNIFF_SCHEMA = {
    "name": "scent_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "odor": {"enum": VALID_ODORS + [None]},
            "intensity": {"type": "number"},
            "why": {"type": "string"},
        },
        "required": ["odor", "intensity", "why"],
        "additionalProperties": False,
    },
}

MIX_SCHEMA = {
    "name": "scent_mix",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "scents": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "odor": {"enum": VALID_ODORS},
                        "intensity": {"type": "number"},
                        "why": {"type": "string"},
                    },
                    "required": ["odor", "intensity", "why"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["scents"],
        "additionalProperties": False,
    },
}


PROMPT_HEADER = """\
You assign ONE Omara scent cartridge to a described subject or moment so the \
smell matches being there. You may only choose from these {n} cartridges: \
{odors}.

CARTRIDGE PALETTE (what each smells like; USE IN SAFE SPACE is authoritative):
{palette}

Rules:
- Smell the SUBJECT of the description, not its grammar. A place, person, \
weather, food, plant or material topic should smell like being there.
- Places are never abstract. ALWAYS map a smell to a place: coastal or \
ocean-facing -> marine; hot and arid -> desert; deep forest -> evergreen; \
rich farm country -> terra_silva or barnyard; Mediterranean fruit country \
-> citrus; polar -> winter.
- A calm or comforting moment with no stronger hook gets odor sweet; never \
invent a smell to seem helpful. Only give odor null if literally nothing in \
the palette fits at all.
- intensity in (0, 1] measures how strongly the subject pulls at the nose: \
faint topic ~0.2-0.4, clear match ~0.5-0.7, you-could-be-there ~0.8-1.
- why: max 8 words saying what is happening in the described moment that \
puts this scent there, as a short phrase — "the fireplace is calmly burning", \
"rain just started on the hot porch".

Description:
{text}
{images_note}
Answer with the structured object only.
"""

MIX_PROMPT_HEADER = """\
You design ONE Omara scent BLEND of exactly {n} cartridges for a moment in \
an EMDR safe-space exercise, so the blend makes the moment memorable and \
calming. You may only choose from these {total} cartridges: {odors}.

CARTRIDGE PALETTE (what each smells like; USE IN SAFE SPACE is authoritative):
{palette}

Rules:
- The blend must fit the MOMENT described below, not a generic "nice smell". \
People are what make places safe: if a person or animal is named, one cartridge \
must carry that (kindred, timber, sweet...).
- Cartridges must be DIFFERENT and complementary: one strong anchor at \
0.7-1.0, one support at 0.4-0.6, one accent at 0.2-0.4. They should blend \
pleasantly, never fight — every cartridge you name WILL spray, each at its \
own intensity. Choose the lead with care: it is the loudest note.
- If recent leads are listed below, you MUST NOT choose any of them as your
  lead; roam the scene and let a different facet lead this frame. (Support
  and accent cartridges may still be any scent.)
- Never choose barnyard or machina unless the moment itself is a farm, \
workshop, engine or animals-with-smell scene.
- why: max 10 words saying what is happening in the moment that puts this \
scent there, as a short phrase — "the fireplace is calmly burning", \
"rain just started on the hot porch".
{recent}
Moment:
{text}
{images_note}
Answer with the structured object only.
"""



def load_token() -> str | None:
    env = os.environ.get("SOOTHINGSPACES_API_KEY")
    if env and env.strip():
        return env.strip()
    try:
        with open(STUDIO_KEY_FILE, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        server = doc["servers"]["http://127.0.0.1:8888"]
        toks = server.get("minted") or server.get("saved") or []
        return toks[-1] if toks else None
    except Exception:
        return None


def load_palette(path: str) -> dict[str, str]:
    """Parse 'Name / SMELLS LIKE / USE IN ...' blocks into {key: text}."""
    palette: dict[str, str] = {}
    cur_key: str | None = None
    cur_lines: list[str] = []

    def close() -> None:
        nonlocal cur_key, cur_lines
        if cur_key is not None:
            joined = " ".join(cur_lines)
            palette[cur_key] = " ".join(joined.split())
        cur_key, cur_lines = None, []

    with open(path, "r", encoding="utf-8-sig") as fh:
        for raw in fh.read().splitlines():
            line = raw.strip()
            if not line:
                continue
            low = line.lower()
            key = DISPLAY_TO_KEY.get(low)
            if key and not low.startswith(("smells", "use")):
                close()
                cur_key = key
                continue
            if cur_key is not None:
                cur_lines.append(line)
    close()
    missing = set(VALID_ODORS) - set(palette)
    if missing:
        raise RuntimeError(f"palette {path} missing cartridges: {sorted(missing)}")
    return palette


def build_prompt(palette: dict[str, str], text: str, n_images: int) -> str:
    lines = [f"- {k}: {palette[k]}" for k in sorted(palette)]
    note = ("An image is attached; weigh it with the text."
            if n_images else "No image attached.")
    return PROMPT_HEADER.format(n=len(VALID_ODORS),
                                odors=", ".join(sorted(VALID_ODORS)),
                                palette="\n".join(lines),
                                text=text, images_note=note)


def build_mix_prompt(palette: dict[str, str], text: str, n: int,
                     n_images: int = 0, recent_leads: list[str] | None = None) -> str:
    """Prompt for an n-cartridge complementary blend of one moment.

    recent_leads are the scents that led the previous frames; naming them is
    how we keep one theme from hogging the whole minute."""
    n = max(1, min(int(n), len(VALID_ODORS)))
    lines = [f"- {k}: {palette[k]}" for k in sorted(palette)]
    note = ("An image is attached; weigh it with the text."
            if n_images else "No image attached.")
    recent = ""
    if recent_leads:
        recent = ("\nRecent lead scents (do not repeat unless truly needed): "
                  + ", ".join(recent_leads) + "\n")
    return MIX_PROMPT_HEADER.format(n=n, total=len(VALID_ODORS),
                                    odors=", ".join(sorted(VALID_ODORS)),
                                    palette="\n".join(lines), recent=recent,
                                    text=text, images_note=note)



def parse_model_json(msg: str) -> dict | None:
    """Model answers arrive as JSON, sometimes quoted or fenced. Dig it out."""
    msg = msg.strip()
    if not msg:
        return None
    if msg.startswith("'") and msg.endswith("'"):
        msg = msg[1:-1]                     # grammar quote artifacts (Studio)
    try:
        obj = json.loads(msg)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", msg, re.S)
    if m:
        try:
            obj = json.loads(m.group(1))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}", msg, re.S)      # last resort: first {...} blob
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    return None


def clamp_mix(raw: dict | None) -> list[dict] | None:
    """Normalize a mix answer into an ordered blend (strongest first).
    Returns None only when nothing usable came back; [] never happens."""
    if not isinstance(raw, dict):
        return None
    entries = raw.get("scents")
    if not isinstance(entries, list):                      # single-scent drift
        entries = [raw] if raw.get("odor") is not None else []
    mix = normalize_mix(entries)
    return mix or None


def clamp_decision(raw: dict | None) -> dict | None:
    """Normalize a schema answer. Returns None only for garbage outside the enum."""
    if not raw:
        return None
    odor = raw.get("odor")
    odor = odor.strip().lower() if isinstance(odor, str) else None
    if odor in ("none", "null", ""):
        odor = None
    why = (raw.get("why") or "").strip()[:60]
    try:
        intensity = float(raw.get("intensity"))
    except (TypeError, ValueError):
        intensity = 0.4
    if odor is None:
        return {"odor": None, "intensity": 0.0, "why": why or "abstract"}
    if odor not in VALID_ODORS:
        return None                          # outside the enum: unusable
    return {"odor": odor,
            "intensity": min(1.0, max(0.02, round(intensity, 3))),
            "why": why}


# ─────────────── general scent-mixing mechanic (shared by callers) ───────────
def intensity_curve(idx: int, total: int) -> float:
    """Breathing-shaped gain across a keyframe track: dips at the edges,
    peaks mid-sequence, so the strongest hits land in the middle."""
    return 0.75 + 0.25 * (1.0 - abs(idx - total / 2) / max(1.0, total / 2))

def normalize_scent(entry: dict) -> dict | None:
    """Validate one {odor, intensity, why} entry. None if unusable."""
    return clamp_decision(entry)

def normalize_mix(entries: list[dict], n: int | None = None) -> list[dict]:
    """Blend a list of scent entries into ONE mix.

    Drops unusable entries; duplicates collapse to their strongest member;
    the result is ordered highest-intensity first so index 0 is the lead note.
    If n is given, only the top n survive (the weakest get filtered out).
    Returns [] for an empty/fully-invalid mix — never None — so callers can
    treat "no scent" uniformly."""
    best: dict[str, dict] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        ns = normalize_scent(entry)
        if ns is None or ns["odor"] is None:
            continue
        prev = best.get(ns["odor"])
        if prev is None or ns["intensity"] > prev["intensity"]:
            best[ns["odor"]] = ns
    ordered = sorted(best.values(), key=lambda s: -s["intensity"])
    if n is not None and n >= 1:
        ordered = ordered[:n]
    return ordered

def keep_strongest(mix: list[dict]) -> dict | None:
    """The lead note of a mix — what hardware that cannot blend should spray."""
    return max(mix, key=lambda s: s["intensity"]) if mix else None

def drop_least(mix: list[dict], keep: int) -> list[dict]:
    """Filter out the least intense members, keeping at most `keep` scents.
    (For the Omara bridge: keep=1 emits only the strongest of a frame.)"""
    return sorted(mix, key=lambda s: -s["intensity"])[:max(0, keep)]



class Sniffer:
    """One-shot subject->scent calls against a local OpenAI-compatible server."""

    def __init__(self, base_url: str, model: str, palette_path: str, *,
                 timeout: float = 25.0, max_tokens: int = 160) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.palette_path = palette_path
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.on_raw: Callable[[str], None] | None = None   # callback(raw_answer_text)
        self.token = load_token()
        self._mtime = 0.0
        self._palette: dict[str, str] = {}
        self.reload_palette(force=True)
        self.cache: dict[int, dict] = {}    # successes only; redrills cost nothing

    def reload_palette(self, force: bool = False) -> None:
        try:
            mtime = os.path.getmtime(self.palette_path)
            if force or mtime != self._mtime:
                self._palette = load_palette(self.palette_path)
                self._mtime = mtime
                if not force:
                    print("[sniff] palette reloaded after edit", flush=True)
        except Exception as exc:
            if not self._palette:
                raise
            log.warning("palette reload failed, keeping previous: %s", exc)

    # -- HTTP -----------------------------------------------------------------
    def _post(self, payload: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def _ask(self, text: str, images: list[str], schema: bool):
        return self._ask_prompt(build_prompt(self._palette, text, len(images)),
                                images, schema, SNIFF_SCHEMA)

    def _ask_prompt(self, prompt: str, images: list[str], schema: bool,
                    json_schema: dict):
        content = [{"type": "text", "text": prompt}]
        for b64 in images:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if schema:
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": json_schema}
        else:
            payload["response_format"] = {"type": "json_object"}
        out = self._post(payload)
        msg = (out["choices"][0]["message"].get("content") or "").strip()
        if self.on_raw:
            try:
                self.on_raw(msg)
            except Exception:
                pass
        return msg

    def _ask_free(self, prompt: str, max_tokens: int = 512) -> str:
        """One free-form prose call — no JSON schema, no json_object format.

        Returns the raw text answer; raises on any transport failure so the
        caller can fall back to quoting saved data verbatim."""
        payload = {
            "model": self.model,
            "messages": [{"role": "user",
                          "content": [{"type": "text", "text": prompt}]}],
            "temperature": 0.4,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        out = self._post(payload)
        return (out["choices"][0]["message"].get("content") or "").strip()


    # -- public ------------------------------------------------------------------
    def sniff(self, text: str, images: list[str] | None = None) -> dict | None:
        """Return {odor, intensity, why}; odor None means 'no scent fits'.
        None (the whole result) means the model could not be reached."""
        text = (text or "").strip()[:2000]
        if not text and not images:
            return {"odor": None, "intensity": 0.0, "why": "empty"}
        self.reload_palette()
        images = list(images or [])[:2]
        key = hash((text, tuple(images)))
        hit = self.cache.get(key)
        if hit is not None:
            log.debug("sniff cache hit")
            return dict(hit)

        last_exc: Exception | None = None
        for schema in (True, False):        # strict grammar, then plain json
            try:
                msg = self._ask(text, images, schema)
                decision = clamp_decision(parse_model_json(msg))
                if decision is not None:
                    if len(self.cache) >= 128:
                        self.cache.clear()
                    self.cache[key] = decision
                    return dict(decision)
                log.warning("model answer unusable despite format=%s: %r",
                            "schema" if schema else "json", msg[:120])
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode(errors="replace")[:200]
                except Exception:
                    pass
                last_exc = exc
                # A server without grammar support answers 4xx on json_schema;
                # fall through to the plain-json retry. Anything else is fatal.
                if exc.code < 500 and schema:
                    log.warning("json_schema rejected (%s: %s); retrying plain json",
                                exc.code, body)
                    continue
                log.error("sniff HTTP %s: %s", exc.code, body)
                return None
            except Exception as exc:
                last_exc = exc
                log.error("sniff call failed: %s", exc)
                return None
        del last_exc
        return None

    def sniff_mix(self, text: str, n: int = 3,
                  images: list[str] | None = None,
                  recent_leads: list[str] | None = None) -> list[dict] | None:
        """Ask the model for an n-scent complementary blend of a moment.

        Returns a normalized mix — strongest first, at most n members — or
        None when the model could not be reached. Empty text is not an error:
        it returns [] after nothing usable came back, so callers can treat
        "no scent" uniformly. recent_leads names the scents that led recent
        frames so the prompt can steer away from repeating one theme."""
        text = (text or "").strip()[:2000]
        if not text and not images:
            return []
        self.reload_palette()
        n = max(1, min(int(n), len(VALID_ODORS)))
        images = list(images or [])[:2]
        key = hash((text, tuple(images), n, tuple(recent_leads or ())))
        hit = self.cache.get(key)
        if hit is not None:
            log.debug("sniff_mix cache hit")
            return dict(hit) if isinstance(hit, dict) else [dict(h) for h in hit]

        prompt = build_mix_prompt(self._palette, text, n, len(images), recent_leads)
        last_exc: Exception | None = None
        for schema in (True, False):        # strict grammar, then plain json
            try:
                msg = self._ask_prompt(prompt, images, schema, MIX_SCHEMA)
                mix = clamp_mix(parse_model_json(msg))
                if mix is not None:
                    mix = normalize_mix(mix, n=n)
                    if len(self.cache) >= 128:
                        self.cache.clear()
                    self.cache[key] = [dict(s) for s in mix]
                    return [dict(s) for s in mix]
                log.warning("mix answer unusable despite format=%s: %r",
                            "schema" if schema else "json", msg[:120])
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode(errors="replace")[:200]
                except Exception:
                    pass
                last_exc = exc
                if exc.code < 500 and schema:
                    log.warning("json_schema rejected (%s: %s); retrying plain json",
                                exc.code, body)
                    continue
                log.error("sniff_mix HTTP %s: %s", exc.code, body)
                return None
            except Exception as exc:
                last_exc = exc
                log.error("sniff_mix call failed: %s", exc)
                return None
        del last_exc
        return None

    def describe(self, prompt: str, max_tokens: int = 512) -> str:
        """Free-form prose call for narrative text (safe-space summaries).

        Returns the model's answer as plain text; raises on transport failure
        or an empty answer so the caller can fall back to quoting saved data."""
        self.reload_palette()
        answer = self._ask_free(prompt, max_tokens=max_tokens)
        if not answer:
            raise RuntimeError("model returned no prose")
        return answer



# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
def post_to_bridge(url: str, frame: dict, timeout: float) -> dict:
    """Send one sniff frame to a running bridge's HTTP port and await its reply.

    This is --send mode: the bridge does the model call, the rate limiting and
    the spray, exactly as it does for session.py. Using it means your test
    really sprays -- Omara fires if it is up. Replies arrive once the model has
    answered, so the timeout should cover a full generation."""
    req = urllib.request.Request(
        url, data=json.dumps(frame).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Sniff a subject for its scent (standalone test of the model link)")
    ap.add_argument("--text", required=True, help="the subject or moment to smell")
    ap.add_argument("--image", action="append", default=[], metavar="FILE",
                    help="attach an image file (repeatable, max 2 used)")
    ap.add_argument("--mix", type=int, metavar="N", default=0,
                    help="blend mode: ask for an N-scent complementary mix "
                         "(1-4) instead of a single cartridge")
    ap.add_argument("--model", default="unsloth/Qwen3.8-Flash-Next-GGUF")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--palette", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "palette.txt"))
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--send", metavar="URL", nargs="?", const="http://127.0.0.1:8445/",
                    help="post the frame to a running bridge instead of calling the "
                         "model directly (default http://127.0.0.1:8445/). The bridge "
                         "sniffs, rate limits and SPRAYS -- Omara fires if it is up.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(name)s %(levelname)s %(message)s")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    images: list[str] = []
    for path in args.image[:2]:
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
            if not blob:
                raise OSError("empty file")
            images.append(base64.b64encode(blob).decode())
        except Exception as exc:
            print(f"image {path!r} unusable: {exc}", file=sys.stderr)

    # --send: hand the card to the bridge, which owns model + rate limit + spray.
    if args.send:
        frame = {"sniff": True, "text": args.text.strip()[:2000],
                 "images": [f"data:image/jpeg;base64,{b}" for b in images]}
        if args.mix:
            frame["mix"] = max(1, min(args.mix, 4))
        t0 = time.monotonic()
        try:
            reply = post_to_bridge(args.send, frame, timeout=args.timeout * 2 + 30)
        except Exception as exc:
            print(f"FAIL bridge unreachable at {args.send}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 1
        dt = time.monotonic() - t0
        if not reply.get("ok"):
            print(f"[{dt:.1f}s] bridge rejected: {reply.get('error')}", file=sys.stderr)
            return 1
        if args.mix:
            members = reply.get("mix") or []
            print(f"[{dt:.1f}s] via bridge {reply.get('how', '?')}: "
                  + (", ".join(f"{m['odor']}@{m['intensity']:.2f}" for m in members)
                     if members else "SILENCE"))
            emit = reply.get("odor")
            print(f"emitted strongest: {emit}@{reply.get('intensity'):.2f}"
                  if emit else "emitted nothing (mix empty)")
            return 0
        odor = reply.get("odor") or "SILENCE"
        how = reply.get("how", "?")
        print(f"[{dt:.1f}s] via bridge {how}: {odor}"
              + (f" @ {reply['intensity']:.2f}" if reply.get("odor") else ""))
        print("(the bridge did the model call and sprayed -- check its window for "
              "the [llm]/[sniff] lines)")
        return 0

    sn = Sniffer(args.base_url, args.model, args.palette, timeout=args.timeout)
    t0 = time.monotonic()
    if args.mix:
        mix = sn.sniff_mix(args.text, n=max(1, min(args.mix, 4)), images=images)
        dt = time.monotonic() - t0
        if mix is None:
            print(f"FAIL: no usable mix answer after {dt:.1f}s", file=sys.stderr)
            return 1
        print(f"[{dt:.1f}s] mix ({len(mix)} scents, strongest first):")
        for s in mix:
            print(f"  {s['odor']} @ {s['intensity']:.2f}  ({s['why']})")
        top = keep_strongest(mix)
        if top:
            print(f"strongest (what a non-mixing device sprays): "
                  f"{top['odor']}@{top['intensity']:.2f}")
        return 0
    decision = sn.sniff(args.text, images)

    dt = time.monotonic() - t0
    if decision is None:
        print(f"FAIL: no usable answer after {dt:.1f}s", file=sys.stderr)
        return 1
    odor = decision["odor"] or "SILENCE"
    print(f"[{dt:.1f}s] {odor}"
          + (f" @ {decision['intensity']:.2f}" if decision["odor"] else "")
          + (f"  ({decision['why']})" if decision["why"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
