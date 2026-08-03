# claude-buddy — Desktop Creature for Claude Code (Linux)

**Status:** the pet is finished, and so is the HUD's v1 — see `ROADMAP.md` for the phases
and `phases/` for one document each. This file is about **the pet**: the art pipeline, the
window, the state machine, and every gotcha hit getting a transparent click-through GTK3
window to behave on X11 and Wayland. Read it before touching `sprites.py`, `build_sheet.py`
or the draw path.
**Last updated:** 2026-08-02

**Built so far:** `sprites.py` (49 frames, 14 animations, 14 props), `build_sheet.py`
(validator + upscale + shading pass + atlas baker), `sprites.png` (246x588 atlas),
`sprites.json` (manifest v2, includes per-frame prop anchors), `preview.png` (contact
sheet), `guy.py` (GTK3 daemon), `notify.sh` (hook writer), plus the HUD modules listed in
section 8. Ten hooks in `~/.claude/settings.json`; the pre-hook backup is at
`~/.claude/settings.json.bak-deckguy`.
Rebuild art after editing grids with `python3 build_sheet.py`.
Daemon output goes to `guy.log` (truncated past 256KB), so a crash leaves evidence.
`python3 check_hooks.py` answers "are the hooks wired, real, and firing".

### Resolution: two grids, one sprite

The finished sprite is **40 x 32 cells, drawn at scale 3, so 120 x 96 px on screen**.

The body is still authored at **20 x 16** and doubled by the baker (`sprites.UPSCALE = 2`).
His proportions were tuned at that size and chunky is the whole look, so there was no reason
to redraw him - and a doubled grid means any single frame can be re-authored at full
resolution later just by marking it `"fine": True`.

Props are authored at the **full 40 x 32**. That is the entire point of the change: props
carry the meaning, and a book drawn in 7 x 6 cells is the same shape as a brick. At 14 x 11
it has a cover, a spine and page lines, and it reads as a book at a glance. Same for the
binoculars (twin barrels with eyepieces) and the pan (bowl plus handle).

Consequences worth remembering:

- **Prop anchors in `COSTUMES` are in fine cells.** Double anything you measure off a body grid.
- **Shading runs after the doubling**, so the outline is one fine pixel - half as thick as
  before, which is why he looks crisper rather than just bigger.
- Body cells are 2 sprite pixels, prop cells are 1. Mixed density is a deliberate trade;
  keep props bold and it reads as intentional rather than as an accident.

### What he does, and when

Same table renders as the key at the top of `preview.png` - both come from `MEANING` and
`TOOL_COSTUME` in `sprites.py`, so the picture cannot describe a costume the daemon does
not wear.

| Shows as | When | Tools |
|---|---|---|
| `idle` | Nothing running. Stands, breathes, blinks every 3-7s. | — |
| `patrol` | Ambient. Wanders up to 150px either side of home, then turns back. | — |
| `working` | A tool is running that has no costume of its own. | — |
| `read` | Reading or searching the codebase. | `Read` `Grep` `Glob` `NotebookRead` |
| `write` | Editing or creating files. | `Edit` `Write` `MultiEdit` `NotebookEdit` |
| `build` | Running shell commands. | `Bash` `BashOutput` `KillShell` |
| `scan` | Fetching or searching the web. | `WebFetch` `WebSearch` |
| `cook` | A subagent is running - or any one task has passed 30 seconds. | `Task` `Agent` |
| `jump` | Task finished. 1 hop under 5s, 2 hops under 30s, 3 and sparkles over 30s. | — |
| `wave` | You clicked him. | — |
| `sleep` | Five minutes with no Claude Code activity at all. | — |
| `wake` | First activity after a nap, then straight into whatever is happening. | — |
| `error` | A tool failed. Red `!` badge on his shoulder; the next step clears both. | — |

Unlisted tools fall back to `working`, so a new Claude Code tool degrades quietly.

Two badges sit on his shoulders and are **not** costumes, because a costume says what he is
*doing* and these are about the session: amber `!` on the left when something is waiting on
you, red `!` when it failed, and a small context pip on the right past 75% full.

### Adding a new activity (the whole procedure)

1. Draw the prop as an ASCII grid in `PROPS` in `sprites.py`.
2. Add a `COSTUMES` entry: which body animation to borrow (`base`), how many frames
   (`length`), and one `[col, row]` anchor per frame per prop (`null` hides it that frame).
3. Map the tool to it in `TOOL_COSTUME` in `sprites.py` - it lives beside the art so
   the daemon and `preview.png` cannot disagree about who wears what.
4. `python3 build_sheet.py`, check `preview.png`, restart the daemon.

No body art and no draw code. The daemon mirrors anchors automatically when he faces left.

### Verified on this machine

- Window lands at exactly `1920x200+0+880`, feet render at y=1073 (screen bottom).
- Transparent, draws over VS Code, pixels stay hard-edged.
- Patrol clamps to home ±150px. Drag works and persists to `pos.json`.
- Jump scaling measured headless: 1 hop / 30px, 2 hops / 38+22px, 3 hops / 46+30+18px.
- End-to-end: `notify.sh prompt` → 6s → `notify.sh jump` produced 2 hops, 52px airborne
  (38px arc + 15px of leg-tuck in the apex pose).
- Sleep/wake headless: asleep after 300s idle, poke plays `wake` then lands in the new mode.
- `notify.sh` costs ~2.5ms per call on a small payload, ~4ms on a 3KB one - safe on
  every `PreToolUse`. Phase 3 wires `PostToolUse` too, so a tool call runs it twice.
- All ten hook event names confirmed present in the installed CLI binary (2.1.220),
  which advertises 31 of them - see `phases/phase-3-attention.md` for the full list.
- Costumes end-to-end: `{"tool_name":"Bash"} | notify.sh working` puts the hard hat on.
  All five costumes plus wave, jump and sleep cycled clean under `--demo`, no exceptions.
- **Repaint is keyed on the visible frame, not on the bounding box.** Keying on the box was
  a real bug: standing still, his box never changes, so the breath and the blink never
  repainted. The key is now (anim, frame, facing, box, clock bucket).
- Tick rate follows posture (`MODE_HZ`): 30Hz moving, 12Hz working, 8Hz idle, 5Hz asleep.
  Idle cost measured at under 0.5s of CPU across 45s of wall clock, down from ~1.8%.
- **Two bugs found by making him log his own transitions** (`guy.log` now records every
  mode and costume change - that is how both of these surfaced):
  1. *He kept cooking after the task finished.* Landing from a jump assigned `mode = "idle"`
     directly instead of going through `_enter()`, so the costume was never taken off. The
     next task then started in the previous task's outfit. Landing now calls `_enter("idle")`.
  2. *A session that dies without a `Stop` left him working forever.* `working` is state we
     only ever hear about second hand, so it now times out: no hook for `STALE_SECS` (45)
     and he goes idle. Verified - `working went stale after 45s` in the log.
- **Hook payloads carry more than the pet used** (checked against the installed CLI by
  dumping a real `PreToolUse`): `session_id`, `transcript_path`, `cwd`, `tool_name`,
  `tool_input`, `hook_event_name`, `permission_mode`, `effort`, `tool_use_id`, `prompt_id`.
  `hook_event_name` is what tells `SessionStart` and `SessionEnd` apart — both are wired to
  `notify.sh idle`, so without it the hook cannot know which one ended the session.
- **Reading the hook payload costs about 0.25ms per KB**, because bash reads a pipe one byte
  at a time. A `Write` call puts the whole new file on stdin, so `notify.sh` reads 1KB and
  only goes back for more if that found nothing useful. 3.6ms typical, 4.0ms on a 235KB
  `Write`.
- **He is fenced to the screen.** `Creature.set_limits()` holds the fence and every mover
  goes through it - drag, patrol, restore from `pos.json`, monitor hotplug. Patrol turns at
  whichever comes first, his patch edge or the screen edge, so a home near a corner just
  shortens the walk. Simulated 60s of patrol from home x=10, x=960 and x=1915 on a 1920px
  screen: body stayed inside 0..1920 in all three, and drags to -500 / 3000 clamp to 64 / 1856.

### GOTCHA: second machine is Wayland, not X11

Installed on a second box (2026-07-29). Two blockers the original X11 machine never hit:

- **`XDG_SESSION_TYPE=wayland`.** On GTK3's native Wayland backend `move()`, `set_keep_above()`
  and `input_shape_combine_region()` are all silent no-ops — he would land wherever the
  compositor felt like, behind other windows, and eat every click across the 1920px strip.
  Fix: launch with `GDK_BACKEND=x11` so he runs under XWayland. `notify.sh --ensure` now sets it.
  Verified: `xwininfo -root -tree` shows `guy.py  1920x200+0+880`, same as the X11 box.
- **`python3-gi-cairo` was missing.** `python3-cairo` alone is not enough — without the GI
  bridge every tick raised `KeyError: 'could not find foreign type Region'` and
  `TypeError: Couldn't find foreign struct converter for 'cairo.Context'`, so nothing drew.
  `sudo apt install python3-gi-cairo`.

Also: copy the folder without `daemon.pid` / `state.json` / `pos.json` / `prompt_ts`, or the
stale pid and the other machine's `home_x` come along for the ride.

### More gotchas found while wiring

- **`setsid` alone is not enough.** It is a silent no-op when the caller is already a process
  group leader, leaving the daemon exposed to SIGHUP. Must use `setsid --fork` (plus `nohup`).
- **`signal.signal()` does not work under `Gtk.main()`.** Python defers its handlers until it
  regains control, which never reliably happens; SIGTERM was ignored entirely and instances
  piled up. Use `GLib.unix_signal_add(...)` instead.
- **Do not `rm -f daemon.pid` to restart.** That bypasses the single-instance guard and you end
  up with several creatures drawn on top of each other. Kill the pid in the file instead.
- **`pgrep -f "deck-guy/guy.py"` misses it** when launched by relative path - the cmdline reads
  `python3 ./guy.py`. Match on `guy\.py` or read `daemon.pid`.
- Beware `pkill -f` patterns that also match your own shell's command line.

### GOTCHA: the GTK API you call depends on the PyGObject you happen to have

On a box with **PyGObject 3.48** the daemon never appeared and left no evidence at all:

```
AttributeError: 'gi.repository.GLibUnix' object has no attribute 'signal_add'
```

`GLib.unix_signal_add` warns as deprecated on 3.50+, and the replacement is
`GLibUnix.signal_add` — but 3.48 ships the GLibUnix typelib carrying only
`signal_add_full`. The guard around it caught `ValueError, ImportError`, which is what a
*missing* typelib raises; a typelib that exists and is a version older raises
`AttributeError` and went straight through. Rule: **probe with `getattr`, never assume a
name exists because the current version has it.** The same applies to the other three
version-split APIs in `guy.py`, all handled the same way:

| API | Old | New |
|---|---|---|
| unix signal handler | `GLib.unix_signal_add` | `GLibUnix.signal_add` / `signal_add_full` |
| monitor rect | `Gdk.Screen.get_monitor_geometry` | `Gdk.Display.get_primary_monitor` (3.22+) |
| monitor hotplug | `Gdk.Screen::size-changed` | `Gdk.Display::monitor-added` (3.22+) |
| context menu | `Gtk.Menu.popup` | `Gtk.Menu.popup_at_pointer` (3.22+) |

Two things made a five-line fix cost an hour, and both are now fixed as well:

- **`notify.sh` sent the daemon's output to `/dev/null`**, and `guy.py` only redirects to
  `guy.log` once it is running — so an import-time crash was completely invisible. It now
  goes to `guy.crash.log`, truncated per attempt, empty whenever he starts properly.
- **`install.sh`'s dependency check imported `gi` and `cairo`, not `guy.py`.** It passed
  happily on a machine where the daemon could not start. It now imports the real module,
  which needs no display and fails loudly at install time.

### GOTCHA: snap environment breaks GTK

Launching from a shell with a snap-polluted environment fails with:

```
python3: symbol lookup error: /snap/core20/current/lib/x86_64-linux-gnu/libpthread.so.0:
undefined symbol: __libc_pthread_init, version GLIBC_PRIVATE
```

`notify.sh --ensure` and any manual launch must strip those vars first:

```bash
env -u LD_LIBRARY_PATH -u GTK_PATH -u GIO_MODULE_DIR python3 guy.py
```

Also required: `gi.require_version("Gdk", "3.0")` before importing Gdk, or Gdk 4.0 wins and
the import blows up.
**Next:** the pet is finished and **HUD phases 1, 2 and 3 are built, which is v1** — he says
what Claude is doing one line above his head, hovering him shows context fill, token burn,
cost and model read from the transcript JSONL, and a badge on his shoulder says when a
session is blocked on you. See `phases/phase-1-session-model.md`, `phase-2-metrics.md` and
`phase-3-attention.md` for what changed on contact with the real data — each one found
something the spec had wrong. `ROADMAP.md` holds the ordering, and `phases/` holds one
document per phase. Phase 4 (many sessions, one buddy) is the natural v1.1.

**Resume with:** open this file and say "continue deck-guy from PLAN.md"

**Project lives at `~/Desktop/claude-buddy/`.** The hooks in `~/.claude/settings.json` point
at `~/Desktop/claude-buddy/notify.sh` by absolute path — if this folder ever moves again,
re-run `./install.sh` from the new location or all ten hook commands stop reacting.

---

## 1. Goal

A little pixel creature that lives at the bottom of the screen and reacts to Claude Code
activity happening in the terminal:

- hangs out on the dock area doing idle things
- **jumps in the air when a task completes**
- **falls asleep if not bothered for a period of time**
- (stretch) performs activity animations with props depending on what tool Claude is running

Claude Code runs in a terminal; the creature must appear on the **desktop/OS**, not in the
terminal statusline.

---

## 2. Reference material

User-supplied, in `~/Downloads/`:

| File | Content |
|---|---|
| `SaveClip.App_AQNdaM5...mp4` | **Primary reference.** 720x900, 7.7s, 24fps. "Desktop creatures, built with Claude Code" |
| `SaveClip.App_732130709_...jpg` | Pixel dachshund walking along a macOS dock |
| `SaveClip.App_756143680_...jpg` | Flat-illustration guy standing on dock ("lil claude agents that walk around your dock") |
| `SaveClip.App_759727228_...jpg` | Black terminal-window mascot on dock — this is the tweet the user quoted verbatim in their original request |

### What the video actually shows

A chunky pixel-art creature in Claude clay-orange stands on top of the macOS dock:

1. Idles — slight body/leg shift
2. A puff appears, a white **chef hat** pops onto its head
3. It raises a dark **pan** with green food in it
4. **Flips the food** — green blob launches upward with white sparkles, lands back in pan
5. Repeats the flip a few times
6. Hat comes off, returns to idle

Key takeaway: the creature performs **activities with props**, not just idle/jump. That is the
charm engine, and it maps cleanly onto Claude Code tool events.

The original project is macOS-only (the "dock" is a macOS concept). The Linux version is what
we are building.

### Extracting frames again later

`ffmpeg` is now installed (user installed it during planning).

```bash
V=~/Downloads/SaveClip.App_AQNdaM5*.mp4
O=/tmp/deckguy-frames && mkdir -p $O
# contact sheet of whole frames
ffmpeg -v error -i "$V" -vf "select='not(mod(n\,12))',scale=360:-1,tile=4x4" -frames:v 1 $O/sheet.png
# zoomed creature, nearest-neighbour, 5x6 grid
ffmpeg -v error -i "$V" -vf "crop=200:170:50:610,select='not(mod(n\,6))',scale=iw*3:ih*3:flags=neighbor,tile=5x6" -frames:v 1 $O/creature.png
```

---

## 3. Environment (probed, confirmed)

| Item | Value |
|---|---|
| Display server | **X11** (`XDG_SESSION_TYPE=x11`) |
| Desktop | Ubuntu GNOME |
| Monitor | HDMI-0, **1920x1080**, single, primary |
| Dock position | **BOTTOM** (`dash-to-dock dock-position = 'BOTTOM'`) |
| Dock auto-hide | Yes (`dock-fixed = false`) |
| Dock icon size | 28 |
| PyGObject / GTK3 | Present |
| librsvg pixbuf loader | Present (SVG loads natively — not needed now, we went pixel) |
| Pillow | 10.2.0 |
| ImageMagick `convert` | Present |
| `ffmpeg` | Installed during planning |
| tkinter | **Missing** (don't use it) |
| rsvg-convert / inkscape / cairosvg | Missing (not needed) |

X11 is good news: transparent always-on-top windows and input-shape click-through both work
properly. On Wayland this would have been much harder.

---

## 4. Architecture

Three parts, decoupled by a single state file. The terminal side never touches the GUI.
Inside the daemon there are three more layers, kept apart on purpose:

| Layer | Owns | Knows nothing about |
|---|---|---|
| `Sprites` | baked atlas, both facings, prop anchors | behaviour, GTK |
| `Creature` | posture, costume, which frame is current | GTK, the window |
| `Deck` | window, polling, compositing, input shape | how animations are built |

**Posture and costume are separate axes.** `mode` is idle / patrol / working / jump / sleep /
wake; `costume` is what he wears while working. Keeping them apart is why the fifteenth
activity costs a prop grid and a dict entry instead of a fifteenth branch in the state machine.


```
Claude Code (terminal)
      │  hooks fire
      ▼
  notify.sh  ──writes──►  state.json  ──polled──►  guy.py  (GTK3 window on desktop)
                        {mode, tool, ts}           always-on-top, transparent
```

Why a file instead of a socket: hooks are short-lived shell commands with no connection to
hold open. A file means zero dependencies, survives daemon restarts, and works when several
Claude Code sessions run at once.

`notify.sh` must be fast and must never fail — a single `printf` to a temp file plus `mv`
(atomic), and always `exit 0`. A slow or failing hook slows down Claude Code itself.

---

## 5. Art direction — PIXEL SPRITES

> **Decision reversal:** an earlier draft of this plan proposed an SVG cutout/puppet rig with
> interpolated motion. That was wrong for this reference. The video is genuine pixel art at
> roughly 10fps with chunky, deliberately choppy motion. A smooth vector rig would destroy the
> charm. **We are doing pixel sprites.**

### Sprite spec

```
grid 24 x 20 cells,  cell = 5px on screen  →  creature ≈ 120 x 100 px

      ▓▓▓▓▓▓▓▓▓▓
      ▓▓██▓▓▓▓██▓          2 black square eyes, no mouth
   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓        side nubs protrude at eye level
      ▓▓▓▓▓▓▓▓▓▓
      ▓▓  ▓▓  ▓▓           3 stubby legs
```

### Palette (body color measured directly from video pixels)

| Role | Hex | Note |
|---|---|---|
| body | `#D77656` | **measured exact** from frame — Claude clay orange |
| eye | `#101010` | near-black squares |
| chef hat | `#F0EDE4` | with `#E3DFD2` shading |
| pan | `#3A3A3A` | dark grey, with handle |
| food | `#4A7C2F` | green |
| sparkle | `#FFFFFF` | |

### How the art is authored

Frames live as **ASCII grids in a Python data file**, one character per pixel, resolved
through a palette dict. Pillow bakes them into a PNG atlas at build time.

```python
PALETTE = {"▓": "#D77656", "█": "#101010", "░": "#F0EDE4", ".": None}

FRAMES = {
    "idle": ["""
....▓▓▓▓▓▓▓▓....
....▓█▓▓▓▓█▓....
..▓▓▓▓▓▓▓▓▓▓▓▓..
....▓▓▓▓▓▓▓▓....
....▓▓..▓▓..▓▓..
""", ...],
}
```

Benefits: hand-editable in any text editor, readable git diffs, no drawing tool needed, no
binary assets to manage. Changing a pixel means changing a character.

### Layering

Base creature sprite + **independent prop overlay** sprites (hat, pan, food, glasses), each
with its own anchor offset. Adding an activity means adding one small prop grid plus a couple
of frames — not redrawing the whole creature.

### Frame budget

| Animation | Frames |
|---|---|
| idle | 4 (breathe / leg shift) + 2 blink |
| walk | 4 |
| jump | 5 (crouch, launch, apex arms up, fall, land squash) |
| sleep | 3 (sit, lie down, breathe) + `z` overlay |
| wake | 3 |
| props | 2-4 each |

Playback at **10fps** to match the reference's chunky cadence. Drops to 2fps while asleep.
Effectively zero CPU.

---

## 6. States and hook mapping

| State | Triggered by | Behavior |
|---|---|---|
| `idle` | `SessionStart`, `SessionEnd` | stand, breathe, blink every 3-7s, occasional short patrol |
| `patrol` | ambient | legs alternate, body bob, wanders within ±150px of home x, then turns back |
| `working` | `UserPromptSubmit`, `PreToolUse` | busy animation; tool-specific costume deferred to a later phase |
| `jump` | `Stop` | crouch → hop, arms up, land squash. Hop count scales with task duration (see §11.2) |
| `sleep` | no state write for N minutes | sit → lie down, `z z Z` rising and fading |
| `wake` | any hook after sleep | pop up, `!` bubble, ~0.9s |

### Tool-aware costumes (the video's cooking bit, generalized)

| Claude tool | Creature does |
|---|---|
| `Read` / `Grep` | reading glasses + tiny book |
| `Edit` / `Write` | pencil, scribbling |
| `Bash` | hard hat, swinging wrench |
| `WebFetch` / `WebSearch` | binoculars, scanning |
| long-running task | **chef hat + pan flip** (the reference animation) |

`PreToolUse` hook JSON already carries the tool name on stdin, so `notify.sh` can read it and
write it into `state.json`. No extra plumbing needed.

### Hook wiring (to add to `~/.claude/settings.json`)

Add a `hooks` block only — leave every existing key untouched. Current settings.json already
has `model`, `statusLine`, `enabledPlugins`, `extraKnownMarketplaces`, `effortLevel`, `tui`,
`theme` and **no hooks block yet**.

```json
"hooks": {
  "SessionStart":     [{ "hooks": [{ "type": "command", "command": "~/Desktop/deck-guy/notify.sh --ensure idle" }]}],
  "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "~/Desktop/deck-guy/notify.sh prompt" }]}],
  "PreToolUse":       [{ "matcher": "*", "hooks": [{ "type": "command", "command": "~/Desktop/deck-guy/notify.sh working" }]}],
  "Stop":             [{ "hooks": [{ "type": "command", "command": "~/Desktop/deck-guy/notify.sh jump" }]}],
  "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "~/Desktop/deck-guy/notify.sh idle" }]}]
}
```

Notes:
- `--ensure` spawns the daemon if it isn't already running, guarded by a pidfile.
- The `PreToolUse` refresh is what stops him falling asleep in the middle of a long tool run.
- Do **not** hook `SubagentStop` — he would jump constantly.

---

## 7. Window mechanics

| Concern | Approach |
|---|---|
| Window | 1920x200 transparent strip at `y=880`, bottom of HDMI-0, RGBA visual, undecorated |
| Stays on top | `set_keep_above(True)` + `stick()` + `Gdk.WindowTypeHint.DOCK` — no alt-tab entry, no taskbar, never steals focus from the terminal |
| **Click-through** | `input_shape_combine_region()` set to only the creature's bounding box, updated as he moves. The rest of the 1920px strip passes clicks through to the dock and desktop |
| Interaction | drag to reposition (saved to `pos.json`), right-click menu (Hide / Quit), click him to make him wave |
| Scaling | nearest-neighbour upscale so pixels stay sharp |
| Multi-monitor | phase 3; single monitor for now |

### Known GNOME limitation

`dash-to-dock` is drawn by the GNOME Shell compositor, in a layer above all normal windows.
So a normal always-on-top window sits **behind** the dock whenever the dock is visible.

The user's dock is set to auto-hide, so it is hidden nearly all the time and the creature is
unobstructed in practice. The only true fix is porting to a GNOME Shell extension (see
phase 4) — likely unnecessary.

---

## 8. File layout

```
~/Desktop/deck-guy/
  README.md        install, requirements, troubleshooting - start here on a new machine
  install.sh       dependency check, art build, hook merge, start
  uninstall.sh     stop, unwire hooks, clear state (--purge, --dry-run)
  PLAN.md          this document - the pet, built and running
  ROADMAP.md       pet -> live HUD, ordering and reasoning (1-3 built = v1, 4-7 planned)
  phases/          one document per HUD phase
  guy.py           GTK3 daemon — window, input shape, state machine, sprite blitter
  sprites.py       ASCII frame grids + palette   ← the art lives here
  build_sheet.py   Pillow: grids → sprites.png atlas (re-run after editing art)
  sessions.py      live sessions on disk: parse, reap, name the target. No GTK
  bubble.py        speech bubble, hover panel and the context meter. No GTK
  transcript.py    tails the transcript JSONL from a byte offset. No GTK
  prices.py        $/token, context windows, a CHECKED date. Expect this to rot
  alerts.py        escalation, dedupe, acknowledgement, toasts, sound. No GTK
  dump_transcript.py  what is really in a transcript today - re-run when numbers look wrong
  test_sessions.py python3 test_sessions.py - no display needed
  test_transcript.py  python3 test_transcript.py - prices, tailing, acceptance
  test_alerts.py   python3 test_alerts.py - the hook layer, escalation, silence
  test_interactions.py  drives the real daemon: click/drag/hover in every mode
  check_hooks.py   audits settings.json, the CLI's event names, and a live trace
  install.sh       deps check, art build, hook merge (10 events), start
  uninstall.sh     the reverse: stop by pid, strip his 10 hooks, rm ~/.deck-guy
  notify.sh        hook writer; one file per session under ~/.deck-guy/
  pos.json         last drag position
  prefs.json       right-click menu settings, e.g. {"bubble": false}
  guy.log          daemon stdout/stderr, truncated past 256KB
~/.deck-guy/
  sessions/<session_id>.json   one live session; deleted when it ends or goes stale
  steps/<session_id>.{start,prompt}   turn timestamps, reaped with the session
~/.claude/settings.json    + hooks block only
```

---

## 9. Build phases

0. ~~**`sprites.py` + `build_sheet.py` + a rendered PNG preview**~~ **DONE**
1. ~~**`guy.py --demo`**~~ **DONE** — cycles every state on the real desktop.
2. ~~**Wire hooks**~~ **DONE** — real reactions to Claude Code, duration-scaled jump.
3. ~~**Polish**~~ **DONE** — drag + position memory, right-click menu (wave / jump / nap /
   force any costume / quit), click-to-wave, horizontal flip when patrolling, ground shadow
   that tightens with jump height, automatic shading pass, `guy.log`, monitor hotplug refit.
4. ~~**Activity costumes**~~ **DONE** — prop overlay layer with per-frame anchors, five
   tool-aware costumes, puff burst on costume change, chef hat after 30s on one task.
5. *(optional)* **GNOME Shell extension port** — genuinely above the dock, can walk between
   dock icons. GNOME-only, version-pinned, JS instead of Python, slower dev loop. Only if the
   dock overlap turns out to be annoying.

---

## 10. Edge cases already accounted for

- **No `DISPLAY`** (ssh session): `notify.sh` still writes state, skips spawning the daemon, exits 0.
- **Two Claude Code sessions at once**: shared state file, last event wins. He jumps when either
  finishes. Alternative (one creature per session) would clutter the screen — rejected.
- **Daemon killed**: next `SessionStart` respawns it via `--ensure`.
- **Long tool run**: `PreToolUse` refresh prevents a false sleep.
- **Sprite cache**: atlas is loaded and scaled once at startup, so per-frame cost is a blit.

---

## 11. Decisions (answered 2026-07-29 — build is unblocked)

1. **Movement: patrol a small home zone.** He wanders within roughly ±150px of his home x
   position, not across the whole 1920px width. Walks a short distance, stops, looks around,
   walks back. Home x is wherever the user last dragged him (`pos.json`), default screen center.
2. **Jump: every `Stop`, but scaled by task duration.** No hard cutoff — instead the
   celebration size scales with how long the task took:
   | Task duration | Celebration |
   |---|---|
   | < 5s | 1 small hop |
   | 5-30s | 2 hops |
   | > 30s | 3 hops, higher, with sparkles |
   Duration = `now - ts` of the last `UserPromptSubmit`. `notify.sh` writes both timestamps so
   the daemon can compute it.
3. **Sleep timeout: 5 minutes.** Configurable via `--idle-secs`, default 300.
4. **Activity costumes: deferred.** Base states first (idle / patrol / working / jump / sleep /
   wake). Costumes and props become their own phase once the base loop feels right.
5. **Sound: off.** Not implemented.

---

## 12. Rejected alternatives (so we don't relitigate)

| Idea | Why rejected |
|---|---|
| Terminal statusline creature | Statusline only repaints on conversation events, so sleep/jump animations freeze. Also the user explicitly wants him on the OS, not the terminal |
| Claude Code subagent | Subagents only exist for the duration of one task; nothing persistent to hang out on the dock |
| SVG cutout/puppet rig with interpolated motion | Wrong aesthetic — reference is chunky pixel art at ~10fps |
| tkinter | Not installed on this machine |
| Electron | Massively heavier than a ~450-line GTK3 script for the same result |
| GNOME Shell extension as phase 1 | Correct layering but GNOME-only, version-pinned, and a much slower iteration loop. Kept as optional phase 4 |
