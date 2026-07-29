#!/usr/bin/env python3
"""Deck guy - a pixel creature that lives at the bottom of the screen.

    python3 guy.py --demo     cycle every state and costume, ignore hooks
    python3 guy.py            react to ~/.deck-guy/sessions/, written by notify.sh

Left-drag him to move (that also moves his patrol home). Click him to get a wave.
Hover him for the readout panel. Right-click for a menu.

Four layers, kept apart on purpose:

    Sprites    the baked atlas - frames, prop art, per-frame anchors. Dumb data.
    Creature   what he is doing and which frame that means. Knows no GTK.
    SessionStore  live Claude Code sessions on disk. Knows no GTK either.
    Deck       the window - compositing, input shape, interaction.

Adding an activity is a prop grid plus anchors in sprites.py and one line in
TOOL_COSTUME. Nothing in the draw path changes.
"""

import argparse
import json
import math
import os
import random
import signal
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

import cairo  # noqa: E402

# Pure data, no GTK. The tool-to-costume table lives with the art it names so that
# the daemon and preview.png can never disagree about who wears what.
from sprites import TOOL_COSTUME  # noqa: E402

from bubble import Bubble, Panel, place  # noqa: E402
from sessions import SessionStore, clock  # noqa: E402

try:  # GLib.unix_signal_add still works but warns on PyGObject 3.50+
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix

    unix_signal_add = GLibUnix.signal_add
except (ValueError, ImportError):  # pragma: no cover - older PyGObject
    unix_signal_add = GLib.unix_signal_add

HERE = Path(__file__).parent
POS = HERE / "pos.json"
PREFS = HERE / "prefs.json"
PIDFILE = HERE / "daemon.pid"
LOGFILE = HERE / "guy.log"
LOG_MAX = 256 * 1024

TICK_HZ = 30          # motion updates per second; sprite frames advance at their own fps
SLEEP_HZ = 5          # throttled tick rate once he is asleep
MODE_HZ = {           # per-posture tick rate; anything absent runs at TICK_HZ
    "idle": 8,        # idle is 4fps, blink 8fps - nothing moves across the screen
    "working": 12,    # costume props top out at 8fps
    "sleep": SLEEP_HZ,
}
PATROL_RANGE = 150    # px he will wander either side of home
PATROL_SPEED = 45     # px per second
GROUND_MARGIN = 6     # px between his feet and the bottom of the screen
EDGE_MARGIN = 4       # px of screen he will not walk into
PUFF_SECS = 0.36      # length of the burst that hides a costume change
LONG_TASK_SECS = 30   # after this long on one task he puts the chef hat on
STALE_SECS = 45       # no hook this long while "working" means the session is over
BUBBLE_GAP = 10       # px between the top of his head and the bubble's tail
BUBBLE_LINGER = 2.5   # secs the last step stays readable after it ends
BUBBLE_FADE = 0.6     # secs it takes to fade out after that



class Sprites:
    """Atlas frames, pre-scaled nearest-neighbour so the pixels stay hard.

    Both facings are baked at load time: flipping a pixbuf per frame would be
    pointless work every tick, and there are only a few dozen frames.
    """

    def __init__(self, scale):
        manifest = json.loads((HERE / "sprites.json").read_text())
        atlas = GdkPixbuf.Pixbuf.new_from_file(str(HERE / "sprites.png"))
        self.scale = scale
        self.grid_w, self.grid_h = manifest["grid"]

        def cut(rects):
            out = []
            for x, y, w, h in rects:
                sub = atlas.new_subpixbuf(x, y, w, h).scale_simple(
                    w * scale, h * scale, GdkPixbuf.InterpType.NEAREST
                )
                out.append((sub, sub.flip(True), w, h))
            return out

        self.anims = {
            name: {
                "frames": cut(spec["rects"]),
                "fps": spec["fps"],
                "loop": spec["loop"],
                "attach": spec.get("attach", {}),
            }
            for name, spec in manifest["anims"].items()
        }
        self.props = {
            name: {"frames": cut(spec["rects"]), "z": spec["z"]}
            for name, spec in manifest["props"].items()
        }
        first = self.anims["idle"]["frames"][0][0]
        self.w, self.h = first.get_width(), first.get_height()

    def __getitem__(self, name):
        return self.anims[name]

    def has(self, name):
        return name in self.anims

    def prop_frame(self, name, index, facing):
        frames = self.props[name]["frames"]
        pix, flipped, cw, ch = frames[index % len(frames)]
        return (flipped if facing < 0 else pix), cw, ch


class Creature:
    """Behaviour and animation state. Knows nothing about GTK.

    `mode` is the posture (idle / patrol / working / jump / sleep / wake) and
    `costume` is what he is dressed as while working. Keeping the two apart is
    what stops every new activity from adding a branch to the state machine.
    """

    def __init__(self, sprites, home_x, idle_secs):
        self.sp = sprites
        self.x = float(home_x)
        self.home_x = float(home_x)
        self.y_off = 0.0          # px above the ground, supplied by the jump arc
        self.idle_secs = idle_secs
        self.facing = 1
        self.min_x, self.max_x = 0.0, float("inf")   # replaced by Deck.set_limits

        self.mode = "idle"
        self.costume = None
        self.anim = "idle"
        self.frame = 0
        self._frame_t = 0.0
        self._next_blink = time.time() + random.uniform(3, 7)
        self._next_decision = time.time() + random.uniform(4, 8)
        self._dir = 1
        self._cue = None           # one-shot animation, then fall into _cue_into
        self._cue_into = None
        self.sparkle = False
        self.puff_until = 0.0
        self.last_activity = time.time()
        self.working_since = 0.0

    # ---------------------------------------------------------------- helpers
    def _play(self, anim):
        if self.anim != anim:
            self.anim = anim
            self.frame = 0
            self._frame_t = 0.0

    def _work_anim(self):
        """Costume animation if we have art for it, plain busy loop otherwise."""
        if self.costume and self.sp.has(self.costume):
            return self.costume
        return "working"

    def set_limits(self, min_x, max_x):
        """Fence him in. Everything that can move him has to pass through here.

        Held in his own coordinates (centre of the body), so callers hand over the
        screen edges once and drag, patrol, restore-from-disk and monitor changes all
        obey the same fence.
        """
        self.min_x, self.max_x = min_x, max(min_x, max_x)
        self.home_x = self.clamp(self.home_x)
        self.x = self.clamp(self.x)

    def clamp(self, x):
        return min(max(x, self.min_x), self.max_x)

    def bbox(self, ground_y):
        """Where his body is right now, as (x, y, w, h) in window coordinates."""
        return (
            int(self.x - self.sp.w / 2),
            int(ground_y - self.sp.h - self.y_off),
            self.sp.w,
            self.sp.h,
        )

    # ------------------------------------------------------------ transitions
    def set_costume(self, costume, now=None):
        """Swap outfits behind a puff of smoke, like the hat popping on in the video."""
        if costume == self.costume:
            return
        print(f"costume {self.costume} -> {costume}", flush=True)
        self.costume = costume
        self.puff_until = (now or time.time()) + PUFF_SECS
        if self.mode == "working":
            self._play(self._work_anim())

    def set_mode(self, mode, duration=0.0):
        """External trigger (hook or demo script)."""
        if mode == self.mode and mode != "jump":
            return
        if self.mode == "sleep" and mode != "sleep":
            self._wake_into(mode)
            return
        if self.mode == "wake" and mode != "sleep":
            self._cue_into = mode      # let the stretch finish, then land in the new mode
            return
        self._enter(mode, duration)

    def _enter(self, mode, duration=0.0):
        if mode != self.mode:
            print(f"mode {self.mode} -> {mode}"
                  + (f" (costume {self.costume})" if self.costume else ""), flush=True)
        self.mode = mode
        self._cue = None
        if mode != "jump":
            # Only a finished jump used to clear this, so a session that ended
            # mid-hop left him sparkling on an empty stage.
            self.sparkle = False
        if mode == "jump":
            self._start_jump(duration)
        elif mode == "working":
            self.working_since = time.time()
            self._play(self._work_anim())
        elif mode == "sleep":
            self._play("sleep")
        else:
            self.costume = None
            self._play("idle")

    def _wake_into(self, mode):
        self.mode = "wake"
        self._cue_into = mode
        self._play("wake")

    def cue(self, anim):
        """Play a one-shot over the top of whatever he is doing, then go back."""
        if self.mode in ("jump", "sleep", "wake"):
            return
        self._cue = self.mode
        self.mode = "cue"
        self._play(anim)

    def _start_jump(self, duration):
        """Hop count scales with how long the task took (PLAN.md 11.2)."""
        if duration > 30:
            heights, self.sparkle = [46, 30, 18], True
        elif duration > 5:
            heights, self.sparkle = [38, 22], False
        else:
            heights, self.sparkle = [30], False
        self._hops = heights
        self._hop_i = 0
        self._jump_t = 0.0
        self._jump_phase = "crouch"
        self._play("jump")

    # ------------------------------------------------------------------ tick
    def update(self, dt, now):
        if self.mode == "jump":
            self._update_jump(dt)
        elif self.mode in ("wake", "cue"):
            self._update_oneshot(dt)
        elif self.mode == "patrol":
            self._update_patrol(dt, now)
        elif self.mode == "idle":
            self._update_idle(dt, now)
        else:
            self._advance(dt)

        # Working is a state we only ever hear about second hand, so it needs a
        # timeout. Claude Code refreshes it on every PreToolUse; a long silence means
        # the session ended without a Stop we saw - crashed, killed, or the terminal
        # closed. Without this he keeps cooking at an empty stove.
        if self.mode == "working" and now - self.last_activity > STALE_SECS:
            print(f"working went stale after {now - self.last_activity:.0f}s", flush=True)
            self._enter("idle")

        # A task that drags on earns the chef hat, per the reference video.
        if (self.mode == "working" and self.costume in (None, "working")
                and now - self.working_since > LONG_TASK_SECS):
            self.set_costume("cook", now)

        # Fall asleep when nothing has poked him for a while.
        if self.mode in ("idle", "patrol") and now - self.last_activity > self.idle_secs:
            self.mode = "sleep"
            self.costume = None
            self._play("sleep")

    def _advance(self, dt):
        """Step the current animation; returns True when a one-shot finishes."""
        spec = self.sp[self.anim]
        self._frame_t += dt
        step = 1.0 / spec["fps"]
        done = False
        while self._frame_t >= step:
            self._frame_t -= step
            self.frame += 1
            if self.frame >= len(spec["frames"]):
                if spec["loop"]:
                    self.frame = 0
                else:
                    self.frame = len(spec["frames"]) - 1
                    done = True
        return done

    def _update_oneshot(self, dt):
        """`wake` and clicked-on cues both end by dropping into a normal mode."""
        if not self._advance(dt):
            return
        nxt, self._cue_into, self._cue = (
            self._cue_into or self._cue or "idle", None, None,
        )
        self._enter(nxt)

    def _update_idle(self, dt, now):
        if self.anim == "blink" and self._advance(dt):
            self._play("idle")
            return
        if self.anim != "blink":
            self._advance(dt)
        if now > self._next_blink and self.anim == "idle":
            self._play("blink")
            self._next_blink = now + random.uniform(3, 7)
        elif now > self._next_decision:
            self._next_decision = now + random.uniform(5, 10)
            if random.random() < 0.6:
                self.start_patrol(now, random.uniform(1.5, 4.0))

    def start_patrol(self, now, secs, direction=None):
        self.mode = "patrol"
        self._dir = direction or random.choice((-1, 1))
        self.facing = self._dir
        self._patrol_until = now + secs
        self._play("patrol")

    def _update_patrol(self, dt, now):
        self._advance(dt)
        # He turns at whichever comes first, the edge of his patch or the edge of
        # the screen, so a home position near a corner shortens the walk instead of
        # marching him off the display.
        left = max(self.home_x - PATROL_RANGE, self.min_x)
        right = min(self.home_x + PATROL_RANGE, self.max_x)
        self.x += self._dir * PATROL_SPEED * dt
        if self.x < left:
            self.x, self._dir = left, 1
        elif self.x > right:
            self.x, self._dir = right, -1
        self.facing = self._dir
        if now > self._patrol_until:
            self.mode = "idle"
            self.facing = 1
            self._next_decision = now + random.uniform(4, 9)
            self._play("idle")

    def _update_jump(self, dt):
        """Pose frames come from the atlas; the vertical travel is computed here."""
        self._jump_t += dt
        air = 0.40
        if self._jump_phase == "crouch":
            self.frame = 0
            if self._jump_t >= 0.14:
                self._jump_phase, self._jump_t = "air", 0.0
        elif self._jump_phase == "air":
            t = self._jump_t / air
            h = self._hops[self._hop_i]
            self.y_off = h * math.sin(math.pi * min(t, 1.0))
            self.frame = 1 if t < 0.25 else (2 if t < 0.6 else 3)
            if self._jump_t >= air:
                self.y_off = 0.0
                self._jump_phase, self._jump_t = "land", 0.0
        else:  # land
            self.frame = 4
            if self._jump_t >= 0.10:
                self._hop_i += 1
                self._jump_t = 0.0
                if self._hop_i >= len(self._hops):
                    self.sparkle = False
                    # Through _enter, not a bare assignment: landing has to take the
                    # costume off too, or the next task starts in last task's outfit.
                    self._enter("idle")
                else:
                    self._jump_phase = "air"

    # ------------------------------------------------------------------ frames
    @property
    def pixbuf(self):
        frames = self.sp[self.anim]["frames"]
        pix, flipped, _, _ = frames[min(self.frame, len(frames) - 1)]
        return flipped if self.facing < 0 else pix

    def attachments(self):
        """(pixbuf, cell_x, cell_y, z) for every prop pinned to the current frame.

        Anchors are authored facing right, so facing left mirrors them across the
        body grid - otherwise the pan would hang off the wrong side of him.
        """
        spec = self.sp[self.anim]
        out = []
        for name, info in spec["attach"].items():
            anchors = info["anchors"]
            anchor = anchors[min(self.frame, len(anchors) - 1)]
            if not anchor:
                continue
            pix, cw, _ = self.sp.prop_frame(name, self.frame, self.facing)
            ax, ay = anchor
            if self.facing < 0:
                ax = self.sp.grid_w - ax - cw
            out.append((pix, ax, ay, info["z"]))
        return out


class Deck(Gtk.Window):
    def __init__(self, args):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.args = args
        self.sp = Sprites(args.scale)

        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()
        self.win_w, self.win_h = geo.width, args.height
        self.ground_y = self.win_h - GROUND_MARGIN

        home = self._load_home(geo.width // 2)
        self.creature = Creature(self.sp, home, args.idle_secs)
        self._fence()

        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_default_size(self.win_w, self.win_h)
        self.move(geo.x, geo.y + geo.height - self.win_h)
        self.stick()

        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_press)
        self.connect("button-release-event", self.on_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("leave-notify-event", self.on_leave)
        self.connect("destroy", lambda *_: Gtk.main_quit())
        display.connect("monitor-added", lambda *_: self._refit())
        display.connect("monitor-removed", lambda *_: self._refit())

        self._drag = None
        self._dragged = False
        self._last_bbox = None
        self._last_readout = None
        self._last_t = time.time()
        self._demo_step = 0
        self._demo_next = time.time() + 3
        self._demo_t0 = self._demo_started = time.time()

        # The readout. Both sit above the tallest hop rather than above his head,
        # so a jump does not fling the bubble around or land him on top of it.
        self.bubble = Bubble(args.scale)
        self.panel = Panel(args.scale)
        self._bubble_until = 0.0
        self._hover = False
        # Turning the bubble off leaves the hover panel working, so the
        # information is still one hover away rather than gone.
        self.show_bubble = bool(self._load_prefs().get("bubble", True))

        # No polling: notify.sh writes a file per session and the store watches
        # the directory. --demo drives him from the script below instead.
        self.store = None
        if not args.demo:
            self.store = SessionStore(on_change=self.on_sessions).start()

        self.show_all()
        self._tick_id = GLib.timeout_add(1000 // TICK_HZ, self.tick)

    # ------------------------------------------------------------------ state
    def _fence(self):
        """Keep his whole body on screen, however wide the strip currently is."""
        half = self.sp.w / 2
        self.creature.set_limits(half + EDGE_MARGIN, self.win_w - half - EDGE_MARGIN)

    def _refit(self):
        """Screen layout changed - re-hug the bottom of the primary monitor."""
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()
        self.win_w = geo.width
        self.ground_y = self.win_h - GROUND_MARGIN
        self.resize(self.win_w, self.win_h)
        self.move(geo.x, geo.y + geo.height - self.win_h)
        self._fence()
        print(f"refit to {geo.width}x{geo.height}+{geo.x}+{geo.y}", flush=True)

    def _load_home(self, default):
        try:
            return json.loads(POS.read_text())["home_x"]
        except Exception:
            return default

    def _save_home(self):
        try:
            POS.write_text(json.dumps({"home_x": int(self.creature.home_x)}))
        except OSError:
            pass

    def _load_prefs(self):
        try:
            data = json.loads(PREFS.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_prefs(self):
        try:
            PREFS.write_text(json.dumps({"bubble": self.show_bubble}))
        except OSError:
            pass

    # ---------------------------------------------------------------- sessions
    def on_sessions(self, store):
        """A session file changed. Phase 1 mirrors the most recent one only.

        Called by the file monitor, not on a timer, so there is nothing to do
        here when Claude Code is quiet.
        """
        s = store.newest()
        if s is None:
            self.creature.set_mode("idle")
            return
        now = time.time()
        self.creature.last_activity = now
        if s.state == "jump":
            self.creature.set_mode("jump", duration=s.task_secs)
        elif s.state == "working":
            was = self.creature.mode
            self.creature.set_mode("working")
            if was != "working":
                self.creature.working_since = now
            self.creature.set_costume(TOOL_COSTUME.get(s.tool), now)
        elif s.state == "idle":
            self.creature.set_mode("idle")

    @property
    def readout_y(self):
        """Where the bubble's tail sits: just above his head, and it rides with him.

        The first version parked it above the tallest possible hop so a jump could
        never reach it. That bought a permanent 130px of empty sky and a bubble
        that read as an unrelated notification. Following his head costs a few
        more repaints during a jump and is what makes it look like he is talking.
        """
        return self.ground_y - self.sp.h - self.creature.y_off - BUBBLE_GAP

    def update_readout(self, now):
        """Text and fade for the bubble, and the rows behind the hover panel.

        The bubble is deliberately quiet: it appears while a step is running and
        fades a couple of seconds after it ends, so an idle desktop has nothing
        on it but him.
        """
        s = self.store.newest() if self.store else None
        if not self.show_bubble:
            self.bubble.set("")
            self._bubble_until = 0.0
        elif s and s.state == "working" and s.label:
            self.bubble.set(s.label, clock(s.elapsed(now)))
            self._bubble_until = now + BUBBLE_LINGER
        elif s and s.state == "jump":
            self.bubble.set("done")
            self._bubble_until = min(self._bubble_until, now + BUBBLE_LINGER)

        if now > self._bubble_until + BUBBLE_FADE:
            self.bubble.set("")

        if not self._hover:
            self.panel.set([])
        elif s:
            rows = [("project", s.project or "-"),
                    ("session", clock(s.runtime(now))),
                    ("step", s.label or "idle"),
                    ("elapsed", clock(s.elapsed(now)))]
            if s.permission_mode:
                rows.insert(1, ("mode", s.permission_mode))
            self.panel.set(rows)
        elif self.args.demo:
            self.panel.set([("project", "deck-guy"), ("mode", "demo"),
                            ("session", clock(now - self._demo_t0)),
                            ("step", self.bubble.text or "idle"),
                            ("elapsed", clock(now - self._demo_started))])
        else:
            self.panel.set([("project", "-"), ("step", "no session"),
                            ("elapsed", "-")])

    def readout_alpha(self, now):
        left = self._bubble_until - now
        return 1.0 if left >= 0 else max(0.0, 1.0 + left / BUBBLE_FADE)

    def readout_rect(self, now):
        """Screen rect of whatever readout is showing, or None. Drives both the
        damage rectangle and the input shape, so the two cannot disagree."""
        widget = self.panel if self.panel.visible else self.bubble
        if not widget.visible or (widget is self.bubble and self.readout_alpha(now) <= 0):
            return None
        w, h = widget.size()
        x, y = place(self.creature.x, self.readout_y, w, h, self.win_w)
        return (x, y, w, h)

    DEMO = [
        ("idle", 4.0), ("patrol", 6.0), ("wave", 2.0),
        ("read", 5.0), ("write", 5.0), ("build", 5.0), ("scan", 5.0), ("cook", 7.0),
        ("jump", 3.0), ("idle", 2.0), ("sleep", 4.0), ("idle", 3.0),
    ]
    DEMO_LABELS = {
        "read": "Read sessions.py", "write": "Edit src/auth.py",
        "build": "Bash pytest -q", "scan": "WebFetch docs.anthropic.com",
        "cook": "Task Explore the repo", "jump": "done",
    }

    def run_demo(self, now):
        if now < self._demo_next:
            return
        mode, hold = self.DEMO[self._demo_step % len(self.DEMO)]
        self._demo_step += 1
        self._demo_next = now + hold
        self.creature.last_activity = now
        self._demo_started = now
        label = self.DEMO_LABELS.get(mode, "")
        if label:
            self.bubble.set(label)
            self._bubble_until = now + hold
        if mode == "jump":
            # cycle the three celebration sizes so all of them get seen
            self.creature.set_mode("jump", duration=[2, 12, 45][self._demo_step % 3])
        elif mode == "patrol":
            self.creature.start_patrol(now, hold)
        elif mode == "wave":
            self.creature.cue("wave")
        elif self.sp.has(mode):        # a costume name
            self.creature.set_mode("working")
            self.creature.set_costume(mode, now)
        else:
            self.creature.set_mode(mode)
        print(f"[demo] {mode}", flush=True)

    # ------------------------------------------------------------------- tick
    def tick(self):
        now = time.time()
        dt = min(now - self._last_t, 0.2)
        self._last_t = now

        if self.args.demo:
            self.run_demo(now)
        self.creature.update(dt, now)
        self.update_readout(now)

        # Repaint on what is actually visible, not on movement alone: he breathes and
        # blinks standing perfectly still, and the zzz trail, sparkles and the costume
        # puff all animate off the clock rather than off a frame index. Keying on this
        # means a still idle creature repaints 4 times a second, not 30.
        c = self.creature
        bbox = c.bbox(self.ground_y)
        if c.mode == "sleep":
            phase = int(now * SLEEP_HZ)
        elif c.puff_until > now:
            phase = int(now * 20)
        elif c.sparkle:
            phase = int(now * 12)
        else:
            phase = 0
        # The readout carries a ticking timer, so it goes in the key too - otherwise
        # the elapsed count would only redraw when he happened to move.
        readout = (self.bubble.text, self.bubble.timer, self.panel.visible,
                   round(self.readout_alpha(now), 2))
        rect = self.readout_rect(now)
        key = (c.anim, c.frame, c.facing, bbox, phase, readout)
        if key != getattr(self, "_last_key", None):
            self._last_key = key
            pad = 90  # room for props, sparkles and the zzz trail
            for b in filter(None, (self._last_bbox, bbox)):
                self.queue_draw_area(b[0] - pad, b[1] - pad, b[2] + pad * 2, b[3] + pad * 2)
            for r in filter(None, (self._last_readout, rect)):
                self.queue_draw_area(r[0] - 2, r[1] - 2, r[2] + 4, r[3] + 4)
            if bbox != self._last_bbox or rect != self._last_readout:
                self._update_input_shape(bbox, rect)
            self._last_bbox = bbox
            self._last_readout = rect

        # Tick only as fast as the current posture needs. Motion wants 30Hz; standing
        # still only has to outrun the animation's own fps, and sleeping barely that.
        want = MODE_HZ.get(c.mode, TICK_HZ)
        if want != getattr(self, "_hz", TICK_HZ):
            self._hz = want
            GLib.timeout_add(1000 // want, self.tick)
            return False
        return True

    def _update_input_shape(self, bbox, readout=None):
        """Only the creature and his readout catch clicks; the rest of the strip
        stays click-through. Anything drawn that is not in here is unhoverable,
        so both rectangles come from the same place the draw does."""
        if self.args.no_shape:
            return
        win = self.get_window()
        if not win:
            return
        x, y, w, h = bbox
        region = cairo.Region(cairo.RectangleInt(x, y, w, h))
        if readout:
            rx, ry, rw, rh = readout
            region.union(cairo.RectangleInt(rx, ry, rw, rh))
        win.input_shape_combine_region(region, 0, 0)

    # ------------------------------------------------------------------- draw
    def _blit(self, cr, pix, x, y, alpha=1.0):
        """Nearest-neighbour blit - cairo's default filter would soften the pixels."""
        Gdk.cairo_set_source_pixbuf(cr, pix, x, y)
        cr.get_source().set_filter(cairo.Filter.NEAREST)
        if alpha >= 1.0:
            cr.paint()
        else:
            cr.paint_with_alpha(alpha)

    def on_draw(self, _widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        c = self.creature
        s = self.sp.scale
        x, y, w, h = c.bbox(self.ground_y)
        now = time.time()

        # Ground shadow: tightens as he rises, which is what sells the jump height.
        if c.mode != "sleep":
            tier = 0 if c.y_off < 6 else (1 if c.y_off < 26 else 2)
            shadow, sw, _ = self.sp.prop_frame("shadow", tier, 1)
            self._blit(cr, shadow, int(c.x - shadow.get_width() / 2),
                       self.ground_y - shadow.get_height())

        for pix, ax, ay, z in c.attachments():
            if z == "back":
                self._blit(cr, pix, x + ax * s, y + ay * s)
        self._blit(cr, c.pixbuf, x, y)
        for pix, ax, ay, z in c.attachments():
            if z != "back":
                self._blit(cr, pix, x + ax * s, y + ay * s)

        if c.sparkle and c.y_off > 8:
            spark, _, _ = self.sp.prop_frame("sparkle", int(now * 12), 1)
            for dx, dy in ((-spark.get_width() - 6, 6), (w + 6, 18)):
                self._blit(cr, spark, x + dx, y + dy)

        if c.puff_until > now:
            t = 1.0 - (c.puff_until - now) / PUFF_SECS
            puff, _, _ = self.sp.prop_frame("puff", int(t * 3), 1)
            self._blit(cr, puff, x + w // 2 - puff.get_width() // 2,
                       y - puff.get_height() // 2, alpha=1.0 - t)

        # Readout last, on top of everything. The panel replaces the bubble rather
        # than stacking under it: the bubble's line is already the panel's `step`
        # row, and stacking would need a strip half again as tall.
        if self.panel.visible:
            self.panel.draw(cr, c.x, self.readout_y, self.win_w)
        else:
            self.bubble.draw(cr, c.x, self.readout_y, self.win_w,
                             self.readout_alpha(now))

        if c.mode == "sleep":
            z, _, _ = self.sp.prop_frame("zzz", 0, 1)
            phase = now % 3.0
            for i in range(3):
                t = (phase - i * 0.9) / 2.4
                if 0 < t < 1:
                    cr.save()
                    cr.translate(x + w + 4 + t * 26, y + h * 0.2 - t * 46)
                    cr.scale(0.6 + t, 0.6 + t)
                    self._blit(cr, z, 0, 0, alpha=max(0.0, 1.0 - t))
                    cr.restore()
        return False

    # -------------------------------------------------------------- interaction
    def on_press(self, _w, ev):
        if ev.button == 1:
            self._drag = (ev.x_root, self.creature.x)
            self._dragged = False
            self.creature.last_activity = time.time()
            if self.creature.mode == "sleep":
                self.creature.set_mode("idle")
        elif ev.button == 3:
            self.menu(ev)
        return True

    def on_motion(self, _w, ev):
        if self._drag:
            start_root, start_x = self._drag
            if abs(ev.x_root - start_root) > 3:
                self._dragged = True
            self.creature.x = self.creature.clamp(start_x + (ev.x_root - start_root))
            self.creature.home_x = self.creature.x
            return True
        # Motion only reaches us inside the input shape, so being here at all means
        # the pointer is on him or on the readout.
        self._hover = True
        return True

    def on_leave(self, _w, _ev):
        self._hover = False
        return True

    def on_release(self, _w, ev):
        if self._drag:
            dragged, self._drag = self._dragged, None
            if dragged:
                self._save_home()
            else:
                self.creature.cue("wave")   # a click, not a drag
        return True

    def on_toggle_bubble(self, item):
        self.show_bubble = item.get_active()
        self._save_prefs()
        print(f"speech bubble {'on' if self.show_bubble else 'off'}", flush=True)

    def menu(self, ev):
        m = Gtk.Menu()
        toggle = Gtk.CheckMenuItem(label="Speech bubble")
        toggle.set_active(self.show_bubble)
        toggle.connect("toggled", self.on_toggle_bubble)
        m.append(toggle)
        m.append(Gtk.SeparatorMenuItem())

        items = [
            ("Wave", lambda *_: self.creature.cue("wave")),
            ("Jump!", lambda *_: self.creature.set_mode("jump", 45)),
            ("Nap now", lambda *_: self.creature.set_mode("sleep")),
        ]
        items += [
            (f"Costume: {name}", lambda _i, n=name: (
                self.creature.set_mode("working"), self.creature.set_costume(n)))
            for name in ("read", "write", "build", "scan", "cook")
        ]
        items.append(("Quit", lambda *_: Gtk.main_quit()))
        for label, fn in items:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", fn)
            m.append(item)
        m.show_all()
        m.popup_at_pointer(ev)


def start_log():
    """Send stdout/stderr to guy.log so a crashed daemon leaves evidence."""
    try:
        if LOGFILE.exists() and LOGFILE.stat().st_size > LOG_MAX:
            LOGFILE.unlink()
        fd = os.open(LOGFILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
        print(f"--- started {time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()}",
              flush=True)
    except OSError:
        pass


def single_instance():
    try:
        old = int(PIDFILE.read_text())
        os.kill(old, 0)
    except (OSError, ValueError):
        pass
    else:
        print(f"already running as pid {old}", file=sys.stderr)
        return False
    PIDFILE.write_text(str(os.getpid()))
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--demo", action="store_true", help="cycle every state, ignore hooks")
    p.add_argument("--scale", type=int, default=3, help="pixel size (default 3)")
    # 340 is the sum of the parts at scale 3: body (96), the tallest hop (46), the
    # gap above his head (10), and the hover panel that replaces the bubble (~160
    # at five rows), with room for the rows phase 2 adds. Everything above him is
    # transparent and click-through, so spare height costs nothing but window
    # size, and the panel clamps if it ever wants more than there is.
    p.add_argument("--height", type=int, default=340, help="strip height in px")
    p.add_argument("--idle-secs", type=int, default=300, help="seconds before he sleeps")
    p.add_argument("--no-shape", action="store_true", help="debug: skip click-through")
    p.add_argument("--no-log", action="store_true", help="keep output on the terminal")
    args = p.parse_args()

    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("no display, not starting", file=sys.stderr)
        return 1
    if not single_instance():
        return 1
    if not args.no_log and not args.demo:
        start_log()

    # GLib's own handlers, not signal.signal(): Python defers its handlers until it
    # regains control from Gtk.main(), which in practice never delivers them here.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda *_: (Gtk.main_quit(), False)[1])
    Deck(args)
    Gtk.main()
    PIDFILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
