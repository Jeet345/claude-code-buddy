# claude-buddy

A pixel creature who lives at the bottom of your screen and tells you what Claude Code is
doing. He walks around while you think, puts on a hard hat when Claude runs `Bash`, jumps
when a task finishes, and falls asleep if you leave him alone — and a line above his head
says which tool is running on what:

```
   ┌──────────────────────────┐
   │ Edit  src/auth.py    4s  │      the timer ticks, so a hung Bash call shows
   └─────────────▽────────────┘
           (buddy)                   hover him for project, session runtime and mode
```

The bubble appears while a step runs and fades a couple of seconds after it ends, so an
idle desktop has nothing on it but him.

He also **tells you when you are the blocker**: a permission prompt waiting in a terminal
you are not looking at puts an amber `!` on his shoulder and holds it there until you
answer. See [Attention](#attention) below.

Linux only, X11 or XWayland. No dependencies outside the distro packages below.

![what he does](preview.png)

`preview.png` is generated and is the reference for what each pose means — open it after
any art change.

---

## Requirements

| Need | Why | Debian / Ubuntu package |
|---|---|---|
| Python 3.8+ | everything | `python3` |
| PyGObject + GTK 3 | the window | `python3-gi`, `gir1.2-gtk-3.0` |
| **`python3-gi-cairo`** | the GI↔cairo bridge | `python3-gi-cairo` |
| *(optional)* Pillow | only for `build_sheet.py`, which bakes the retired pixel atlas | `python3-pil` |
| X11, or Wayland with XWayland | positioning and click-through | `xwayland` |
| *(optional)* libwnck **or** `xprop` | jumping to a session's terminal | `gir1.2-wnck-3.0`, or `x11-utils` |

Any GTK 3.x and any PyGObject: the four APIs that moved between versions (the unix signal
handler, monitor geometry, monitor hotplug, and the context menu popup) are probed at
runtime rather than assumed, so 3.48 and 3.50+ both work. `install.sh` imports `guy.py`
during its check, so a version this code has not met fails at install time with a
traceback instead of silently never appearing.

**`python3-gi-cairo` is the one people miss.** `python3-cairo` alone is not enough — without
the bridge every frame raises `KeyError: 'could not find foreign type Region'` and nothing
draws. It is a separate package.

```bash
sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0
```

<details>
<summary>Other distros</summary>

```bash
# Fedora
sudo dnf install python3-gobject python3-cairo-gobject gtk3
# Arch
sudo pacman -S python-gobject python-cairo gtk3 python-pillow
# openSUSE
sudo zypper install python3-gobject-Gdk typelib-1_0-Gtk-3_0 python3-cairo
```
</details>

Not required: tkinter, ffmpeg, ImageMagick, any pip package, any network access.

### Wayland

He runs under **XWayland**, always. On GTK3's native Wayland backend `move()`,
`set_keep_above()` and `input_shape_combine_region()` are all silent no-ops — he lands
wherever the compositor likes, sits behind other windows, and swallows every click across
the strip. `notify.sh` sets `GDK_BACKEND=x11` for you; you only need XWayland present.

---

## Install

Copy the folder anywhere (`~/Desktop/claude-buddy` is the tested spot), then:

```bash
cd claude-buddy
./install.sh
```

Restart Claude Code afterwards — hooks load at session start.

That script is safe to re-run and does five things:

1. checks the dependencies — the exact cairo call that fails without the bridge, then an
   import of `guy.py` itself, which is the only thing that proves *this code* runs against
   *these* library versions (needs no display)
2. stops any running copy **by pid** and clears stale session state
3. bakes `sprites.png`, `sprites.json` and `preview.png` from `sprites.py`
4. merges ten hook entries into `~/.claude/settings.json`, backing it up first
5. starts the daemon and tells you the pid

The hook merge keeps every other key and every hook that is not his, and replaces
his own entries rather than appending to them — so re-running after moving the
folder fixes the paths instead of doubling them. It recognises its own entries by the
`notify.sh` at the end of the command, not by the folder name, because a folder gets
renamed and a rename that silently doubles every hook is not a good failure.

`./install.sh --check` runs step 1 only and changes nothing.

### Doing it by hand instead

If you would rather not run the script, this is all it does that matters:

```bash
python3 build_sheet.py && python3 build_sheet.py --preview
```

then add to `~/.claude/settings.json`, with **absolute paths** — hooks do not expand `~`
reliably in every context, and the arguments are not interchangeable:

```jsonc
{
  "hooks": {
    "SessionStart":       [{ "hooks": [{ "type": "command", "command": "/ABS/PATH/claude-buddy/notify.sh --ensure idle" }] }],
    "UserPromptSubmit":   [{ "hooks": [{ "type": "command", "command": "/ABS/PATH/claude-buddy/notify.sh --ensure prompt" }] }],
    "PreToolUse":         [{ "matcher": "*",
                             "hooks": [{ "type": "command", "command": "/ABS/PATH/claude-buddy/notify.sh working" }] }],
    "PostToolUse":        [{ "matcher": "*",
                             "hooks": [{ "type": "command", "command": "/ABS/PATH/claude-buddy/notify.sh working" }] }],
    "Stop":               [{ "hooks": [{ "type": "command", "command": "/ABS/PATH/claude-buddy/notify.sh jump" }] }],
    "SessionEnd":         [{ "hooks": [{ "type": "command", "command": "/ABS/PATH/claude-buddy/notify.sh idle" }] }],

    // attention - these four are what make him notice you are the blocker
    "Notification":       [{ "hooks": [{ "type": "command", "command": "/ABS/PATH/claude-buddy/notify.sh alert" }] }],
    "PostToolUseFailure": [{ "matcher": "*",
                             "hooks": [{ "type": "command", "command": "/ABS/PATH/claude-buddy/notify.sh alert" }] }],
    "PermissionDenied":   [{ "matcher": "*",
                             "hooks": [{ "type": "command", "command": "/ABS/PATH/claude-buddy/notify.sh working" }] }],
    "StopFailure":        [{ "hooks": [{ "type": "command", "command": "/ABS/PATH/claude-buddy/notify.sh error" }] }]
  }
}
```

`--ensure` is what launches the daemon. It is on `UserPromptSubmit` as well as
`SessionStart` so a daemon that died at 11am is back by your next prompt instead of staying
gone until you open a new session tomorrow — the check is `[ -r ]`, `read` and `kill -0`,
all bash builtins, so it costs about 0.1ms and never forks. The others only write state.
`PreToolUse` and `PostToolUse` fire on every single tool call, which is why `notify.sh` is a
bash script with no forks except one `mv` (~4ms per call, so ~8ms per tool call for the
pair).

`PostToolUse` and `PermissionDenied` look redundant — they say "working", which `PreToolUse`
already said. They are not: there is no *"the permission prompt was answered"* event, so the
badge clears on the next hook of any kind, and those two are the first thing that happens
after you approve or deny. Leave them out and the badge stays lit through the whole of the
tool run you just authorised.

**If you move the folder, rewrite those paths** — or just re-run `./install.sh` from the new
location.

---

## Running him

The daemon starts itself on your next Claude Code session. To drive it yourself:

```bash
env -u LD_LIBRARY_PATH -u GTK_PATH -u GIO_MODULE_DIR GDK_BACKEND=x11 python3 guy.py --demo
```

The `env -u` strip is needed if your shell has a snap-polluted environment, which breaks
GTK's linking. `notify.sh` always strips them.

| Flag | Does |
|---|---|
| `--demo` | cycles every pose and costume, ignores hooks — the fastest way to see the art |
| `--scale N` | pixel size, default 3 (he is 40×32 authored, so 120×96 on screen) |
| `--height N` | height of the invisible strip he lives in, default 400 |
| `--idle-secs N` | seconds of nothing before he sleeps, default 300 |
| `--no-log` | print to the terminal instead of `guy.log` |
| `--no-shape` | skip click-through, for debugging where the window actually is |

Drag him with the mouse to move him; his home position persists in `pos.json`. Click him and
he **jumps you to that session's terminal** and waves. Hover him for the readout panel.
**Right-click him for the menu**, which has:

| Item | Does |
|---|---|
| **Speech bubble** | turns the bubble above his head on and off — remembered in `prefs.json` |
| **Desktop notifications** | `notify-send` toasts for alerts. On by default, remembered |
| **Alert sound** | a beep. **Off** by default, remembered. Independent of the toast |
| **Click jumps to terminal** | what a left-click does. On by default, remembered |
| Focus terminal | do it now, without clicking — still there when the click is turned off |
| Wave / Jump! / Nap now | play a state on demand |
| Costume: … | force a costume, for checking the art |
| Quit | stop the daemon, **and keep him stopped** |

Turning the bubble off leaves the hover panel working, so the same information is one hover
away rather than gone.

### Jumping to the terminal

The badge tells you a session is waiting on you. Clicking him takes you there — the window
that session is running in comes to the front.

Nothing in a hook payload says which window that is, so `notify.sh` records its own place
in the process tree instead, and the daemon matches that against the pids the window
manager reports:

```
hook → zsh → claude → zsh → code → code → systemd
                                   ^^^^
                                   owns the window
```

Nearest ancestor wins, so you land on the terminal rather than on its grandparent. If one
process owns several windows — an editor with two projects open — the one whose title names
the session's project wins, then whichever you used most recently.

**X11 and XWayland only.** A terminal running as a native Wayland client cannot be raised by
another client, by anyone, ever. When the jump is impossible he **copies the session's `cwd`
to the clipboard and says so in the bubble**, rather than doing nothing and letting you
think the click missed.

Window lookup uses `libwnck` if its typelib is installed and falls back to forking `xprop`
if not; neither is required at install time and the failure is reported, not hidden.

Turn the click off from the menu if you would rather he never moved your focus — **Focus
terminal** stays in the menu either way.

**The toast and the sound are two switches because they are two situations**, and all four
combinations do something:

| Notifications | Sound | You get | Good for |
|---|---|---|---|
| on | off | badge + toast | the default: you are at the screen, looking at something else |
| on | on | badge + toast + beep | you wander off, or the terminal is on another workspace |
| **off** | **on** | **badge + beep** | nothing allowed to cover your work, but tell me out loud |
| off | off | badge only | you will look when you look |

The third row is the one worth knowing about — a sound reaches you when the screen is
covered or you are not at it, which is the case this whole feature exists for.

Claude Code's permission message is the same generic sentence whatever is blocked —
literally `Claude needs your permission`, with no tool in it — so the buddy splices in the
step that is actually stuck: **`Bash pytest -q - needs your permission`**. And several
sessions blocking at the same moment produce **one** toast that says how many, not one
popup each.

There is no "mute for an hour". The bottom row **is** mute, it says so on the label, and it
survives a restart — a timed one was a third route into a state these two already cover,
and the only thing it added was a way to be silenced without knowing it.

### Attention

The thing this is actually for: a session sitting on a permission prompt in a terminal you
are not looking at.

| What happened | What he does |
|---|---|
| Claude needs permission, or has been waiting on your input | amber `!` on his shoulder, he waves, the bubble holds the message and a running clock |
| Still unanswered after 10s | a desktop toast, plus a beep if you turned sound on |
| Still unanswered after 5 min | one repeat of both, and then it stops |
| A tool failed | red `!`, no noise — a failed `grep` is a normal part of a session |
| The session itself failed, or stopped answering mid-tool | he slumps, red `!`, and a toast |

Both thresholds are in `prefs.json` as `alert_secs` and `alert_repeat_secs`. 10s reads as
aggressive and is not — see the 6-second caveat below, which puts the real figure nearer 16s
of a prompt sitting unanswered. Raise them if that turns out to be too keen.

**Answering it in the terminal clears everything within about 30ms.** That is the whole
design: the badge is cleared by the next hook event of any kind, so approving, denying, or
just carrying on all switch it off. Nothing here waits for a timeout.

Clicking him acknowledges an alert without answering it — the badge goes, the alert stays
gone until it clears on its own, and a *new* prompt gets a new badge.

Two honest limits:

- **Claude Code sits on the notification for about 6 seconds first.** It only notifies once
  you have not touched the keyboard for that long, which is good behaviour — but it means
  "he reacts instantly" is instant *from the notification*, not from the dialog opening.
  An idle-input notification waits 60s the same way.
- **`notify-send` is optional.** Without it you get the badge and nothing else, which is
  most of the value. Nothing is installed for you and nothing errors if it is missing.

**`python3 alerts.py` is the thing to run when you are not getting alerts.** It prints what
this machine can actually do and then sends one of each:

```
notify-send  /usr/bin/notify-send
sound        /usr/bin/canberra-gtk-play -i message
thresholds   first toast 10s unanswered, one repeat at 300s; the sound goes with both
```

Add `--quiet` to print without sending. If it says `NO USABLE PLAYER`, install
`libcanberra-gtk-module` (for `canberra-gtk-play`) or `pipewire-audio-client-libraries`
(for `pw-play`). **`aplay` alone is not enough** — it is ALSA's WAV player, and the
freedesktop sound theme is Ogg, which `aplay` will happily play as raw 8-bit noise at exit
code 0 rather than admit it cannot read it. It is only ever handed a WAV.

### The hover panel

```
project  claude-buddy
mode     acceptEdits
session  1h 04m
step     Bash pytest -q
elapsed  7s
waiting  Bash pytest -q - needs your permission  (41s)
context  44%  439k / 1.00M
         ████████░░░░░░░░░░░░░░
tokens   3.1M in · 249k out
cost     ~$16.00
model    claude-opus-5 high
```

The `waiting` row (or `failed`) only appears when there is something to say.
The bottom four rows come from your session's transcript file, which Claude Code writes as
it goes. **Context** is the one worth watching: it is the input tokens of the most recent
turn against the model's window, so it is what actually predicts a compaction — the bar
turns amber past 75% and red past 90%, and at those levels a small badge appears on his
shoulder so you can see it without hovering.

Two caveats, both deliberately visible in the UI rather than buried here:

- **Cost is an estimate and always shows a `~`.** It is tokens times a table in
  `prices.py`, with cache reads and cache writes priced separately. That table carries a
  `CHECKED` date; once it is more than 90 days old the figure is drawn grey.
- **The context window is not in the transcript**, so it also comes from a table. If your
  session ever reads higher than the table thinks possible, the window is re-estimated and
  every number in that row goes grey — meaning "this is a guess", not "this is fine".

A session attached partway through a very large transcript shows `(partial)` on the tokens
row: the running totals only count from where reading started, so they under-report.
`python3 dump_transcript.py` prints what is actually in your transcripts, which is the
first thing to run if these numbers ever look wrong.

**Quit means quit for the sitting.** Because `--ensure` runs on every prompt, Quit leaves a
marker at `~/.deck-guy/off` that the hooks respect, or it would undo itself within a minute.
Two things clear it: running `./notify.sh --ensure idle` by hand, and opening Claude Code
again. Both are you asking for him back; a mid-session hook never is. `/clear` and an
auto-compact fire `SessionStart` too, but they are the same sitting and leave him quit.

### Is it actually wired?

```bash
python3 check_hooks.py            # audit + exercise every event
python3 check_hooks.py --trace    # then use Claude Code, then re-run
```

Three separate questions, and the third is the one nothing else can answer:

1. **Is it wired?** settings.json against what `install.sh` writes — catches a moved folder,
   a half-finished install, a hand-edit, or the same hook wired twice.
2. **Is the event name still real?** Checked against the installed `claude` binary. A hook
   pointing at an event the CLI has renamed is silent, and silent looks exactly like
   working-with-nothing-to-say.
3. **Does it fire?** The session file only holds the *last* event, so one that fires and is
   overwritten a moment later leaves no evidence at all. `--trace` makes `notify.sh` append
   a line per call, which turns the question into arithmetic — including the `Pre/Post`
   pairing, where a shortfall means a badge that stays lit after you approve.

`--off` stops recording and deletes the log. When tracing is off the cost is one `[ -e ]`,
which is a shell builtin.

### Where the live state lives

```
~/.deck-guy/sessions/<session_id>.json   one file per running Claude Code session
~/.deck-guy/steps/<session_id>.*         turn timestamps
~/.deck-guy/off                          you chose Quit; hooks will not restart him
                                         until the next Claude Code launch
~/.deck-guy/events.log                   only while check_hooks.py --trace is on
```

`notify.sh` writes them on every hook event; the daemon watches the directory and deletes a
session when it ends or stops sending heartbeats. Deleting the whole `~/.deck-guy` directory
is safe at any time — it is rebuilt on the next event. Set `DECK_GUY_HOME` to move it.

### Stop / restart

```bash
kill "$(cat daemon.pid)"        # stop
./notify.sh --ensure idle       # start
```

**Do not `rm daemon.pid` to restart.** That bypasses the single-instance guard and you get
two creatures drawn on top of each other. Kill the pid in the file.

### Uninstall

```bash
./uninstall.sh              # stop him, unwire the hooks, clear ~/.deck-guy
./uninstall.sh --dry-run    # print what that would do and change nothing
./uninstall.sh --purge      # also his position, prefs, log and the baked art
```

It removes **only** his ten hook entries — recognised by the `notify.sh` at the end of the
command, same rule as the install — and leaves every other key and every other hook in
`settings.json` alone, with a backup at `settings.json.bak-deckguy-uninstall` taken only on
a run that actually removes something. Restart Claude Code afterwards so it stops loading
them.

A plain run keeps `pos.json` and `prefs.json`, so reinstalling puts him back where he was
with the same switches. It never deletes the folder — do that yourself:

```bash
rm -rf claude-buddy
```

---

## Troubleshooting

Start with `guy.log` in the project folder — he records every mode and costume change, which
is how both of the last two real bugs were found.

| Symptom | Cause |
|---|---|
| `KeyError: could not find foreign type Region` | `python3-gi-cairo` missing |
| `Couldn't find foreign struct converter for 'cairo.Context'` | same |
| `symbol lookup error: /snap/core20/.../libpthread.so.0` | snap environment; launch with the `env -u` prefix above |
| Nothing appears at all | read **`guy.crash.log`** first — it holds anything the daemon printed before it got as far as opening `guy.log`, which is where an import error lands. Empty file and still nothing: no `DISPLAY`/`WAYLAND_DISPLAY` (ssh), or run `guy.py --no-log` and read the traceback |
| `AttributeError: 'GLibUnix' object has no attribute 'signal_add'` | a build from before the PyGObject 3.48 fix — `git pull`, or see the version table in `PLAN.md` |
| He is behind other windows, or in the wrong place, or eats clicks | running on native Wayland instead of XWayland; check `GDK_BACKEND=x11` reached him |
| Two of him | `daemon.pid` was deleted while he was alive; `pkill -f 'guy\.py'` and start once |
| He never reacts to Claude | hooks not loaded (restart Claude Code), or the paths in `settings.json` point at the old folder |
| He reacts but never stops working | a session died without a `Stop`; he times out after 45s on his own |
| No bubble, but he still animates | the payload had no target field for that tool — expected for `BashOutput`, `WebSearch` and anything new; he shows the bare tool name |
| He is reacting to a session you closed | check `~/.deck-guy/sessions/` — a file with an old `heartbeat` should vanish within 45s; if it does not, the daemon is not running |
| No badge when a permission prompt is open | the `Notification` hook is not wired (re-run `./install.sh`, restart Claude Code), or you answered inside 6s — the CLI only notifies once you have been idle that long |
| Badge stays up after you answered | `PostToolUse` and `PermissionDenied` are missing from `settings.json`; without them nothing tells him you replied |
| Badge but never a toast or a sound | `grep alerts: guy.log` shows the thresholds he actually loaded; `grep toast guy.log` shows every one he sent. Answering or clicking him inside 10s is the alarm working |
| Toast but no sound | no player that reads Ogg — see the `alerts.py` note above; `aplay` on its own does not count |
| He slumps for no reason | a session went 45s mid-tool without a hook. Usually a terminal closed with Claude still running; click him to clear it |
| Sprites look wrong after editing `sprites.py` | re-run `python3 build_sheet.py`, then restart the daemon |
| `pgrep -f claude-buddy/guy.py` finds nothing | he was launched by relative path; match `guy\.py` or read `daemon.pid` |

---

## What is in here

```
guy.py           the daemon: window, animation, state machine
sprites.py       the art, as ASCII grids + palette + anchors  <- edit this
build_sheet.py   bakes sprites.png / sprites.json / preview.png
sessions.py      live sessions: parse, reap, work out what to call the target
bubble.py        the speech bubble, hover panel and context meter
transcript.py    tails the transcript JSONL for tokens, context and cost
prices.py        the price table and context windows  <- this one rots, check it
alerts.py        who is waiting on you, escalation, toasts and sound
windows.py       pid chain -> X window -> raise it, and an honest "cannot"
dump_transcript.py  what is really in a transcript today; run it when numbers look wrong
test_sessions.py python3 test_sessions.py    (no display needed)
test_transcript.py  python3 test_transcript.py  (no display needed)
test_alerts.py   python3 test_alerts.py      (no display needed)
test_windows.py  python3 test_windows.py     (live checks skipped without a display)
test_interactions.py  clicking him at the worst possible moment  (needs a display)
check_hooks.py   are the hooks wired, real, and actually firing
notify.sh        the hook target; writes a session file, starts the daemon
install.sh       dependency check, art build, hook wiring
uninstall.sh     stop him, unwire the hooks, clear state  (--purge, --dry-run)
PLAN.md          how the pet is built and why, plus every gotcha hit
ROADMAP.md       the plan to turn him into a live Claude Code HUD
phases/          one document per HUD phase (1-3 built = v1, 4-7 specced)
```

To add a new activity: draw a prop grid in `PROPS`, add a `COSTUMES` entry with per-frame
anchors, map the tool in `TOOL_COSTUME`, rebuild. No draw code, no new branch in the state
machine. Full procedure in `PLAN.md`.
