# SoothingSpaces — EMDR safe-space sessions that smell like you built them

**Mark J. Kuebel** — Licensed under the Business Source License 1.1; see
`LICENSE`. Production or commercial use requires a license from the author.

`session.py` runs a guided EMDR **safe-place** intake over the console: it
asks where your safe space is, what you see/hear/smell/feel there, who is
with you, and how calm you are — with follow-ups when an answer is thin, so
a substantial session gets real detail to work with. It then builds **one
scent mix** from your own words — up to 3 channels, each with its own
intensity — and sprays it through the bridge as a single burst. That mix is
saved to `safe_space.json`; the next run finds it and asks whether you want
to **return to your safe space** instead of doing intake again.

`--keyframes N` (N>1) keeps the multi-frame option: an N-frame keyframe
sequence at a 2-second interval (~60 s of breathing-paced scent for N=30),
one mix per frame, delivered on schedule.

Scent is the memory hook: smell routes straight into the brain regions that
handle emotion and memory, so reinforcing the visualized safe place with a
blend drawn from *its own* named smells makes the place stickier and easier
to summon mid-session later.

## Data flow

    session.py   (intake -> one mix per scene, or N keyframe mixes)
      -> ws://127.0.0.1:8765/     omara_bridge.py WebSocket port (loopback)
      -> COM4 / BLE               Omara device; ONE packet per mix, every
                                  channel firing at its own intensity

## Scent mixing

- `sniff.py` owns the general mixing mechanic: `normalize_mix()` validates
  and merges scent entries into one blend (dedupe to strongest, ordered
  highest-first), `keep_strongest()` / `drop_least()` filter it, and
  `Sniffer.sniff_mix(text, n)` asks the model for an n-scent complementary
  blend in one schema-constrained call (anchor ~0.9 / support ~0.5 / accent
  ~0.3).
- `session.py` builds **one mix per scene** by default (or per keyframe with
  `--keyframes N`) — from the model when it is up, from palette rules over
  your named smells when it is not. In multi-frame mode a breathing-shaped
  intensity curve peaks mid-track so the strongest hits land in the middle
  of the minute.
- The bridge's `spray` command resolves every channel name against the
  **tubes actually loaded on the cartridge** and fires them as one mix
  packet (max 3 per packet, device policy). Picks that aren't loaded are
  skipped and reported — you see exactly what sprayed and what didn't.

When hardware mixing changes, nothing else has to change: send mixes through
`normalize_mix` and adjust only the bridge edge.

## Quick start

    run.bat                       starts the bridge; leave its window open
    start_session.bat             runs a session — bridge window must be open

Options worth knowing in `session.py`:

| Flag | Meaning |
|---|---|
| `--keyframes N` | `1` (default): one mix for the scene, every channel spraying at its own intensity. `N>1`: multi-frame keyframe track |
| `--preview` | build and print; send nothing, save nothing (test mode) |
| `--no-model` | skip the LLM; rule-based mixes from your named smells |
| `--base-url` / `--model` | any OpenAI-compatible server (`:11434/v1` for Ollama) |
| `--bridge-url` | default `ws://127.0.0.1:8765` (omara_bridge.py) |
| `--safe-space FILE` | reference file for the calculated scent (default `safe_space.json`) |
| `--skip-recall` | ignore the saved safe space; go straight to intake |

A session prints each emission with what caused it — a line from your scene,
not just a cartridge name:

    frame 16/30 @   30s  sprayed petrichor@0.90   because: rain just stopped on wet earth

## Returning to your safe space

After a session builds its mix, the calculated scent and every answer you
gave are saved to `safe_space.json`. The next time you run
`start_session.bat`, it opens with:

    A safe space is on file from 2026-09-30 15:06:00: petrichor@0.90,
    evergreen@0.50, smoky@0.30 (A small cabin in the pine woods beside a
    quiet creek, rain just stopped)
    Would you like to return to your safe space? [Y/n]

Saying yes recites what is in the file — *remember: petrichor@0.90 +
evergreen@0.50 + smoky@0.30 to set the scene with the stuff you told it*,
followed by your own place/sounds/smells/feeling lines, all quoted from the
file, nothing invented — and re-sprays the saved mix in one burst. Saying no
runs a fresh intake, which overwrites the file when it completes.

## Testing without spraying cartridges

Run the session with `--preview`: it builds and prints everything but sends
nothing to the bridge and saves nothing. To exercise delivery paths against a
dead port (no Omara), point at a port nothing listens on:

    py session.py --bridge-url ws://127.0.0.1:8181

The run reaches its spray step, reports `bridge unreachable`, and exits 1 —
the bridge is never contacted until then.

Standalone mix check (this one *does* reach the model):

    py sniff.py --mix 3 --text "Safe place: pine cabin after rain, wet earth, my dog"

## Palette

`palette.txt` lists the Omara cartridges: what each smells like, plus
`USE IN SAFE SPACE` guidance for when a cartridge fits a safe-space moment
(and when it does not — machina is nearly never; barnyard only for clients
whose safe place genuinely is a farm or ranch). Hot-reloads on edit;
`session.py --preview` after an edit shows the effect immediately.

## Files

| File | Role |
|---|---|
| `start_session.bat` | session launcher: EMDR intake -> scent mix (or keyframe track) -> bridge |
| `session.py` | therapy state machine, mix/track builder, delivery, safe-space recall |
| `omara_bridge.py` | persistent device bridge (BLE or serial), WebSocket server on :8765, rate gate |
| `sniff.py` | model calls; general mixing mechanic; standalone CLI (`--mix`) |
| `palette.txt` | cartridges and safe-space guidance (hot-reload) |
| `safe_space.json` | the saved safe space: calculated scent + your scene words (created by a session) |
| `run.bat` | bridge launcher (`--serial com4 --rate 2 --rate-mode LAST`) |
| `requirements.txt` | websockets (session/sniff need nothing else) |

## Notes

- Nothing leaves the machine: intake answers stay in this console; all traffic
  is loopback. No internet access anywhere in the pipeline.
- A session never dies on a missing model: every failed model frame falls back
  to the rule-based mix, and a fully unreachable model reports that plainly.
