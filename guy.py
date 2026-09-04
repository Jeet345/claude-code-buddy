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
from gi.repository import Gdk, GLib, GObject, Gtk  # noqa: E402

import vector  # noqa: E402

import cairo  # noqa: E402

# Pure data, no GTK. The tool-to-costume table lives with the art it names so that
# the daemon and preview.png can never disagree about who wears what.
from sprites import COSTUMES, TOOL_COSTUME  # noqa: E402

import prices  # noqa: E402
import windows  # noqa: E402
from alerts import Alerts  # noqa: E402
from bubble import HIGH, WARN, Bubble, Meter, Panel, bar_colour, place  # noqa: E402
from sessions import ROOT as SESSIONS_ROOT, SessionStore, clock  # noqa: E402

def _unix_signal_add():
    """Whichever of the three spellings this PyGObject actually has.

        3.50+   GLibUnix.signal_add
        3.48    GLibUnix typelib is present but only carries signal_add_full
        older   no GLibUnix at all; GLib.unix_signal_add is the one that exists
                (it still works everywhere, it only warns as deprecated on 3.50+)

    Every failure mode here is an exception at *import* time, and an import that
    raises is the worst kind of failure this daemon has: notify.sh starts him
    detached, and start_log() has not redirected anything yet, so the traceback
    goes nowhere and he simply never appears. Hence: no attribute is assumed to
    exist, and the catch-all names AttributeError as well.
    """
    try:
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
    except (ValueError, ImportError):  # pragma: no cover - depends on the box
        return GLib.unix_signal_add
    for name in ("signal_add", "signal_add_full"):
        fn = getattr(GLibUnix, name, None)
        if fn is not None:
            return fn
    return GLib.unix_signal_add  # pragma: no cover - typelib with neither


unix_signal_add = _unix_signal_add()

HERE = Path(__file__).parent
POS = HERE / "pos.json"
PREFS = HERE / "prefs.json"
PIDFILE = HERE / "daemon.pid"
LOGFILE = HERE / "guy.log"
LOG_MAX = 256 * 1024

TICK_HZ = 60          # motion updates per second
SLEEP_HZ = 10         # throttled tick rate once he is asleep
# Per-posture tick rate; anything absent runs at TICK_HZ.
#
# The body is drawn from springs now rather than stepped through baked frames,
# so there is no longer an animation fps to merely outrun - motion is continuous
# and 60Hz is what makes it look it. What has not changed is that a creature who
# is only breathing does not need 60Hz to do it, so the quiet postures stay
# throttled. That is the same reasoning the sprite version used, applied to a
# different set of numbers.
MODE_HZ = {
    "idle": 30,       # breathing and the occasional blink
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





class Creature:
    """Behaviour and animation state. Knows nothing about GTK.

    `mode` is the posture (idle / patrol / working / jump / sleep / wake) and
    `costume` is what he is dressed as while working. Keeping the two apart is
    what stops every new activity from adding a branch to the state machine.
    """

    def __init__(self, sprites, home_x, idle_secs, on_stale=None):
        self.sp = sprites
        self.on_stale = on_stale  # a working session that stopped answering
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
        elif mode == "error":
            # Costume off: the outfit says what he is doing, and he is not doing
            # it any more. The badge is what names the failure.
            self.costume = None
            self._play("error")
        else:
            self.costume = None
            self._play("idle")

    def _wake_into(self, mode):
        self.mode = "wake"
        self._cue_into = mode
        self._play("wake")

    @property
    def slumped(self):
        """Is he in the error posture, *or* about to fall back into it.

        A failure makes him wave and slump at once, so for the length of the
        wave the slump is parked in `_cue` and `self.mode` says `cue`. Anything
        asking "is he showing a failure" has to ask this and not `mode`, or it
        gets the wrong answer for exactly the half-second after a failure.
        """
        return "error" in (self.mode, self._cue, self._cue_into)

    def leave_error(self):
        """Stop being slumped, including a slump queued behind a one-shot.

        A failure makes him wave *and* slump: the wave is a cue, so `error` is
        parked in `_cue` until the wave finishes. Clicking during those few
        frames used to clear the badge and then drop him back into the slump
        with nothing left to clear it.
        """
        if self._cue == "error":
            self._cue = None
        if self._cue_into == "error":
            self._cue_into = None
        if self.mode == "error":
            self._enter("idle")

    def cue(self, anim):
        """Play a one-shot over the top of whatever he is doing, then go back."""
        if self.mode in ("jump", "sleep", "wake"):
            return
        if self.mode != "cue":
            # A cue raised during a cue must not park "cue" as the mode to
            # return to. It did, and `_enter("cue")` is a mode nothing drives:
            # he kept the idle frames but stopped blinking, stopped patrolling
            # and never fell asleep again. Two clicks in a row was enough.
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
            # Phase 3: this is a crash, not a finish. There is no hook for a
            # terminal that was closed mid-tool - the only evidence is silence,
            # so he slumps and lets the daemon raise the alert for it.
            self._enter("error")
            if self.on_stale:
                self.on_stale()

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
            if self.frame >= spec["count"]:
                if spec["loop"]:
                    self.frame = 0
                else:
                    self.frame = spec["count"] - 1
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
    def frame_phase(self):
        """Where we are in the animation loop, in frames, with the fraction.

        `frame` on its own is a flipbook index, which is all a sprite needed.
        Props are interpolated between their keyframes now, so they also need
        how far through the current frame we are.
        """
        spec = self.sp[self.anim]
        return self.frame + min(1.0, self._frame_t * spec["fps"])

    def attachment_specs(self):
        """(prop_key, cell_x, cell_y, z, rotation) for the vector renderer.

        The anchors in COSTUMES are keyframes, not stops: they are sampled
        along a spline at the continuous phase so a prop travels between them
        instead of teleporting, and each prop leans into its own motion.
        """
        spec = self.sp[self.anim]
        phase = self.frame_phase
        out = []
        for name, info in spec["attach"].items():
            anchors = info["anchors"]
            pos = vector.sample_anchors(anchors, phase)
            if pos is None:
                continue
            key = vector.prop_key(name, self.frame)
            if key is None:
                continue
            size = vector.prop_size(key)
            ax, ay = pos
            rot = vector.prop_rotation(name, anchors, phase)
            if self.facing < 0:
                ax = self.sp.grid_w - ax - (size[0] if size else 0)
                rot = -rot
            out.append((key, ax, ay, info["z"], rot))
        return out



def primary_geometry():
    """Rect of the monitor he lives on, across GTK3's three monitor APIs.

    get_primary_monitor() on Gdk.Display is 3.22+; before that the same two
    questions were asked of Gdk.Screen. A multi-head box with no primary set
    answers None to the first one, which is why get_monitor(0) is a fallback and
    not an else-branch.
    """
    display = Gdk.Display.get_default()
    monitor = None
    if display is not None:
        get_primary = getattr(display, "get_primary_monitor", None)
        if get_primary is not None:
            monitor = get_primary()
        if monitor is None and hasattr(display, "get_monitor"):
            monitor = display.get_monitor(0)
    if monitor is not None:
        return monitor.get_geometry()

    screen = display.get_default_screen() if display else Gdk.Screen.get_default()
    idx = 0
    if hasattr(screen, "get_primary_monitor"):
        idx = max(screen.get_primary_monitor(), 0)
    return screen.get_monitor_geometry(idx)


class Deck(Gtk.Window):
    def __init__(self, args):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.args = args
        self.sp = vector.Sheet(args.scale)

        geo = primary_geometry()
        self.win_w, self.win_h = geo.width, args.height
        self.ground_y = self.win_h - GROUND_MARGIN

        home = self._load_home(geo.width // 2)
        self.creature = Creature(self.sp, home, args.idle_secs, on_stale=self.on_stale)
        self.rig = vector.Rig()
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
        # monitor-added/removed arrived with the monitor API in 3.22. Where they
        # do not exist, Gdk.Screen's size-changed is the older equivalent and
        # covers the case that actually matters: the strip is the wrong width
        # after a resolution change.
        display = Gdk.Display.get_default()
        if display is not None and GObject.signal_lookup("monitor-added", type(display)):
            display.connect("monitor-added", lambda *_: self._refit())
            display.connect("monitor-removed", lambda *_: self._refit())
        else:  # pragma: no cover - GTK < 3.22
            self.get_screen().connect("size-changed", lambda *_: self._refit())

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
        prefs = self._load_prefs()
        # Turning the bubble off leaves the hover panel working, so the
        # information is still one hover away rather than gone.
        self.show_bubble = bool(prefs.get("bubble", True))
        # Phase 4. Clicking him jumps to the terminal that session is running in.
        # On by default because the click already had no other job - it waved -
        # and because the alert badge exists to make you go there. It is a
        # preference and not a constant because a click that moves your focus is
        # exactly the kind of helpfulness some people want turned off.
        self.click_focus = bool(prefs.get("click_focus", True))
        # A short-lived line that outranks the bubble's usual contents. Only ever
        # set in answer to a deliberate gesture, so it is allowed to interrupt.
        self._say = ("", 0.0)
        # Sound ships off. It is the fastest way to turn a charming thing into
        # an annoying one, and unlike the badge you cannot ignore it.
        # No default thresholds repeated here. They were, and changing the ones
        # in alerts.py then did nothing at all to the running daemon - it kept
        # toasting at the old 60s while every test and every document said 10.
        # `Alerts` falls back on its own constants when handed None.
        self.alerts = Alerts(toast=prefs.get("toast", True),
                             sound=prefs.get("sound", False),
                             first=prefs.get("alert_secs"),
                             repeat=prefs.get("alert_repeat_secs"))
        self._alert_key = None
        print(f"alerts: toast={self.alerts.toast} sound={self.alerts.sound} "
              f"first={self.alerts.first}s repeat={self.alerts.repeat}s", flush=True)

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
        geo = primary_geometry()
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
            prefs = self._load_prefs()
            prefs.update({"bubble": self.show_bubble,
                          "toast": self.alerts.toast, "sound": self.alerts.sound,
                          "click_focus": self.click_focus})
            PREFS.write_text(json.dumps(prefs, indent=2))
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
            # A slump he has not acknowledged outlives the session that caused
            # it. Resetting him to idle here would erase the only evidence that
            # anything went wrong the moment the dead session got reaped - and
            # `slumped` rather than `mode == "error"` because the reap usually
            # lands during the wave, when the slump is still parked behind it.
            if not (self.creature.slumped and self.alerts.pending()):
                self.creature.set_mode("idle")
            return
        now = time.time()
        self.creature.last_activity = now
        self.alerts.clear_local()          # this session is alive after all
        if s.state == "jump":
            self.creature.set_mode("jump", duration=s.task_secs)
        elif s.state == "working":
            was = self.creature.mode
            self.creature.set_mode("working")
            if was != "working":
                self.creature.working_since = now
            self.creature.set_costume(TOOL_COSTUME.get(s.tool), now)
        elif s.state == "error":
            self.creature.set_mode("error")
        elif s.state == "idle":
            self.creature.set_mode("idle")

    def on_stale(self):
        """A working session stopped answering. Say so rather than shrugging."""
        self.alerts.local("session_died", "the session stopped answering")

    def on_alerts(self, now):
        """React to the alert changing. Everything else here only reads it.

        The wave is the cue, not a mode: he keeps whatever costume he had on, so
        a permission prompt in the middle of a Bash call still looks like a Bash
        call that is stuck, which is what it is.
        """
        live = self.store.live() if self.store else []
        self.alerts.update(live, now)
        a = self.alerts.current()
        key = (a.session_id, a.kind, a.since) if a else None
        if key == self._alert_key:
            return
        self._alert_key = key
        if a is None:
            return
        print(f"alert {a.kind}: {a.message}", flush=True)
        c = self.creature
        c.last_activity = now
        if c.mode == "sleep":
            c.set_mode("idle")     # being asked a question is a reason to wake up
        c.cue("wave")

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
        alert = self.alerts.current()
        say, say_until = self._say
        if not self.show_bubble:
            self.bubble.set("")
            self._bubble_until = 0.0
        elif say and now < say_until:
            # Above the alert, below the preference. You just clicked, and what
            # your click did outranks a message you have already read - but an
            # alert is more urgent than this and is *still* suppressed when the
            # bubble is off, so nothing here gets to overrule that switch.
            self.bubble.set(say)
            self._bubble_until = say_until
        elif alert:
            # An alert holds the bubble open for as long as it lasts. This is
            # the one thing here that is not allowed to fade out politely: the
            # whole feature is that you notice it from the other side of the
            # room while you are doing something else.
            self.bubble.set(alert.message, clock(alert.waited(now)))
            self._bubble_until = now + BUBBLE_LINGER
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
            self.panel.set(rows + self.alert_rows(alert, now)
                           + self.metric_rows(s.metrics))
        elif self.args.demo:
            self.panel.set([("project", "deck-guy"), ("mode", "demo"),
                            ("session", clock(now - self._demo_t0)),
                            ("step", self.bubble.text or "idle"),
                            ("elapsed", clock(now - self._demo_started))]
                           + self.alert_rows(alert, now)
                           + self.demo_metric_rows(now))
        else:
            self.panel.set([("project", "-"), ("step", "no session"),
                            ("elapsed", "-")])

    def alert_rows(self, alert, now):
        """One row, and only when there is something to say. A permanent
        `attention: none` row would be four more pixels of nothing on every
        hover, and would train you to stop reading the panel."""
        rows = []
        if alert:
            label = "failed" if alert.failed else "waiting"
            rows.append((label, f"{alert.message}  ({clock(alert.waited(now))})"))
        return rows

    @staticmethod
    def metric_rows(m):
        """The phase 2 half of the panel, or a single honest line saying why not.

        Every number here is greyed out the moment we are not sure of it: an
        approximate context window, a price table older than 90 days. A figure
        drawn in the same black as a known one is a figure someone will quote.
        """
        if m is None:
            return [("context", "-")]
        if not m.ok:
            return [("context", "reading transcript…", True)]

        pct = f"{m.fill * 100:.0f}%"
        approx = "~" if m.window_approx else ""
        burn = f"{prices.tokens(m.input)} in · {prices.tokens(m.output)} out"
        rows = [("context", f"{pct}  {prices.tokens(m.context)} / "
                            f"{approx}{prices.tokens(m.window)}", m.window_approx),
                Meter(m.fill, dim=m.window_approx),
                ("tokens", burn + (" (partial)" if m.partial else "")),
                ("cost", prices.money(m.cost), prices.stale() or not prices.known(m.model))]
        model = m.model or "unknown"
        rows.append(("model", f"{model} {m.effort}".strip()
                     + ("  fast" if m.speed == "fast" else ""), not m.model))
        return rows

    def context_level(self):
        """`0` fine, `1` amber, `2` red - the one number allowed out of the panel.

        Everything else in phase 2 stays behind a hover. This does not, because
        by the time you think to check, a compaction has already happened.
        """
        s = self.store.newest() if self.store else None
        m = getattr(s, "metrics", None) if s else None
        if m is None or not m.ok or m.window_approx:
            return 0            # never alarm on a number we are guessing at
        return 2 if m.fill >= HIGH else 1 if m.fill >= WARN else 0



    def draw_context_pip(self, cr, c):
        """A small badge at his shoulder. Deliberately not a costume: a costume
        says what he is doing, and this is a warning about the session."""
        level = self.context_level()
        if not level:
            return
        px = self.args.scale
        # His sheet cell is 120x96 but the body inside it is 90x63, and `bbox`
        # returns the cell. Anchoring to the cell put the badge 33px above his
        # head in empty sky, looking like an unrelated notification - the same
        # mistake the bubble made in phase 1. Find the actual pixels instead.
        bx, by, bw, _ = c.bbox(self.ground_y)
        pad_top, _, pad_right = vector.body_inset(self.sp.scale)
        x = int(bx + bw - pad_right - px * 2)
        y = int(by + pad_top)
        cr.set_source_rgb(0.16, 0.11, 0.09)
        cr.rectangle(x - px, y - px, px * 4, px * 4)
        cr.fill()
        cr.set_source_rgb(*bar_colour(1.0 if level == 2 else WARN))
        cr.rectangle(x, y, px * 2, px * 2)
        cr.fill()

    def draw_alert_badge(self, cr, c, now):
        """The `!` on his left shoulder. The context pip has the right one.

        Two badges, two meanings, two corners: context is a slow number you
        watch, an alert is a thing that just happened. Sharing a corner would
        make them look like two states of one indicator.
        """
        a = self.alerts.current()
        if a is None:
            return
        key = vector.prop_key("alert", a.frame)
        aw, ah = vector.prop_size(key)
        s = self.sp.scale
        bx, by, _, _ = c.bbox(self.ground_y)
        pad_top, pad_left, _ = vector.body_inset(self.sp.scale)
        x = int(bx + pad_left - aw * s * 0.45)
        y = int(by + pad_top - ah * s * 0.3)
        # A slow pulse while it is unanswered. Not a flash: this can sit there
        # for twenty minutes, and anything faster than this is unliveable.
        alpha = 0.72 + 0.28 * (0.5 + 0.5 * math.sin(now * 2.4))
        vector.draw_prop(cr, key, x, y, s, alpha=alpha)

    def demo_metric_rows(self, now):
        """--demo has no transcript, so fake a bar that sweeps the thresholds.

        Without this there is no way to eyeball the amber and red states short
        of burning a real session down to its last tokens.
        """
        fill = (now / 12.0) % 1.0
        return [("context", f"{fill * 100:.0f}%  "
                            f"{prices.tokens(int(fill * 1_000_000))} / 1.00M"),
                Meter(fill),
                ("tokens", "1.24M in · 38k out"),
                ("cost", prices.money(3.41)),
                ("model", "claude-opus-5 high")]

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
        ("jump", 3.0), ("alert", 5.0), ("error", 5.0),
        ("idle", 2.0), ("sleep", 4.0), ("idle", 3.0),
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
        if mode not in ("alert", "error"):
            self.alerts.clear_local()
        if mode == "alert":
            # A fake alert, because the real one needs a real permission prompt
            # and there is no way to stage one of those on demand.
            self.alerts.local("permission_prompt",
                              "Claude needs your permission to use Bash", now)
        elif mode == "error":
            self.alerts.local("session_failed", "Bash failed: exit 2", now)
            self.creature.set_mode("error")
        elif mode == "jump":
            # cycle the three celebration sizes so all of them get seen
            self.creature.set_mode("jump", duration=[2, 12, 45][self._demo_step % 3])
        elif mode == "patrol":
            self.creature.start_patrol(now, hold)
        elif mode == "wave":
            self.creature.cue("wave")
        elif mode in COSTUMES:
            # `COSTUMES`, not `sp.has(mode)`: every costume is an animation but
            # not every animation is a costume, so the loose test caught `idle`
            # and `sleep` too and dressed him up as them. It looked close enough
            # to be missed - except that `sleep` never entered the sleep *mode*,
            # so the zzz trail never appeared in the demo at all.
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
        self.rig.update(dt, self.creature)
        # Escalation is a clock, not an event, so it has to be looked at every
        # tick. There is no new timer: this is the one that was already running.
        self.on_alerts(now)
        self.update_readout(now)

        # Repaint on what is actually visible, not on movement alone: he breathes and
        # blinks standing perfectly still, and the zzz trail, sparkles and the costume
        # puff all animate off the clock rather than off a frame index. Keying on this
        # means a still idle creature repaints 4 times a second, not 30.
        c = self.creature
        bbox = c.bbox(self.ground_y)
        if self._alert_key is not None:
            phase = int(now * 12)      # the badge pulses, so it needs its own clock
        elif c.mode == "sleep":
            phase = int(now * SLEEP_HZ)
        elif c.puff_until > now:
            phase = int(now * 20)
        elif c.sparkle:
            phase = int(now * 12)
        else:
            phase = 0
        # The readout carries a ticking timer, so it goes in the key too - otherwise
        # the elapsed count would only redraw when he happened to move.
        readout = (self.bubble.text, self.bubble.timer, self.panel.key(),
                   round(self.readout_alpha(now), 2), self.context_level())
        rect = self.readout_rect(now)
        # The sprite body only changed when `frame` did, so the frame index was
        # a sound repaint key. A spring-driven body changes every tick, so the
        # tick counter goes in instead - except while asleep, where the rig is
        # effectively still and the old economy is worth keeping.
        self._seq = getattr(self, "_seq", 0) + 1
        moving = 0 if c.mode == "sleep" else self._seq
        key = (c.anim, c.frame, c.facing, bbox, phase, readout, moving)
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
            key = vector.prop_key("shadow", tier)
            sw, sh = vector.prop_size(key)
            vector.draw_prop(cr, key, c.x - sw * s / 2,
                             self.ground_y - sh * s, s)

        specs = c.attachment_specs()
        for key, ax, ay, z, rot in specs:
            if z == "back":
                vector.draw_prop(cr, key, x + ax * s, y + ay * s, s, rotate=rot)
        self.rig.draw(cr, x, y, w, h, c.facing)
        for key, ax, ay, z, rot in specs:
            if z != "back":
                vector.draw_prop(cr, key, x + ax * s, y + ay * s, s, rotate=rot)

        if c.sparkle and c.y_off > 8:
            key = vector.prop_key("sparkle", int(now * 12))
            spw = vector.prop_size(key)[0] * s
            for dx, dy in ((-spw - 6, 6), (w + 6, 18)):
                vector.draw_prop(cr, key, x + dx, y + dy, s)

        if c.puff_until > now:
            t = 1.0 - (c.puff_until - now) / PUFF_SECS
            key = vector.prop_key("puff", int(t * 3))
            pw, ph = vector.prop_size(key)
            vector.draw_prop(cr, key, x + w / 2 - pw * s / 2,
                             y - ph * s / 2, s, alpha=1.0 - t)

        self.draw_context_pip(cr, c)
        self.draw_alert_badge(cr, c, now)

        # Readout last, on top of everything. The panel replaces the bubble rather
        # than stacking under it: the bubble's line is already the panel's `step`
        # row, and stacking would need a strip half again as tall.
        if self.panel.visible:
            self.panel.draw(cr, c.x, self.readout_y, self.win_w)
        else:
            self.bubble.draw(cr, c.x, self.readout_y, self.win_w,
                             self.readout_alpha(now))

        if c.mode == "sleep":
            phase = now % 3.0
            for i in range(3):
                t = (phase - i * 0.9) / 2.4
                if 0 < t < 1:
                    cr.save()
                    cr.translate(x + w + 4 + t * 26, y + h * 0.2 - t * 46)
                    cr.scale(0.6 + t, 0.6 + t)
                    vector.draw_prop(cr, "zzz", 0, 0, s,
                                     alpha=max(0.0, 1.0 - t))
                    cr.restore()
        return False

    # -------------------------------------------------------------- interaction
    def say(self, text, secs=3.0):
        """Put one line in the bubble now, whatever else it was showing."""
        self._say = (text, time.time() + secs)

    def focus_terminal(self, session=None):
        """Jump to the terminal this session is running in.

        Returns the `windows.Result` so the menu item and the click can share
        one implementation and still report differently.

        When it cannot be done the cwd goes to the clipboard instead and he says
        so. That is the honest degradation the phase asks for: on a native
        Wayland terminal no client can raise another's window, and pretending
        otherwise gives you a gesture that works four times and silently does
        nothing the fifth, which is worse than one that never worked.
        """
        s = session or (self.store.newest() if self.store else None)
        if s is None:
            self.say("no session to jump to")
            return windows.Result(False, "no session")

        r = windows.focus(s.pids, project=s.project)
        if r.ok:
            print(f"focused terminal {r.xid:#x} {r.name!r} "
                  f"for {s.id[:8]} via pids {s.pids}", flush=True)
            return r

        # Could not raise it. Hand over the one thing that is still useful.
        copied = False
        if s.cwd:
            try:
                clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                clip.set_text(s.cwd, -1)
                clip.store()
                copied = True
            except Exception as exc:      # no clipboard owner, no display
                print(f"clipboard failed: {exc}", flush=True)
        self.say("cwd copied - " + r.reason if copied else r.reason, 4.0)
        print(f"focus failed for {s.id[:8]}: {r.reason} "
              f"(pids {s.pids}, cwd copied: {copied})", flush=True)
        if getattr(r, "hint", ""):
            print(f"  {r.hint}", flush=True)
        return r

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
                # Order matters. Jumping happens first because it is the reason
                # you clicked; acknowledging is a side effect of having arrived,
                # not a competing gesture. Both then wave, because a click with
                # nothing visible behind it feels broken.
                if self.click_focus:
                    self.focus_terminal()
                if self.alerts.ack():
                    print("alert acknowledged", flush=True)
                    self.creature.leave_error()
                self.creature.cue("wave")
        return True

    def on_toggle_bubble(self, item):
        self.show_bubble = item.get_active()
        self._save_prefs()
        print(f"speech bubble {'on' if self.show_bubble else 'off'}", flush=True)

    def on_toggle_sound(self, item):
        self.alerts.sound = item.get_active()
        self._save_prefs()
        print(f"alert sound {'on' if self.alerts.sound else 'off'}", flush=True)

    def on_toggle_click_focus(self, item):
        self.click_focus = item.get_active()
        self._save_prefs()
        print(f"click jumps to terminal: {self.click_focus}", flush=True)

    def on_toggle_toast(self, item):
        self.alerts.toast = item.get_active()
        self._save_prefs()
        print(f"desktop toasts {'on' if self.alerts.toast else 'off'}", flush=True)

    def on_quit(self, _item):
        """Quit, and stay quit for this sitting.

        `notify.sh --ensure` now runs on every prompt so a daemon that died
        comes back by itself, which would otherwise make this menu item mean
        "go away for thirty seconds". The marker says a person asked. Running
        `./notify.sh --ensure idle` by hand removes it, and so does opening
        Claude Code again - a fresh launch is a person asking too, where a
        prompt in the session you quit from is not.
        """
        try:
            SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)
            (SESSIONS_ROOT / "off").touch()
        except OSError:
            pass
        print("quit by menu; --ensure will leave him alone until asked", flush=True)
        Gtk.main_quit()

    def menu(self, ev):
        m = Gtk.Menu()
        toggle = Gtk.CheckMenuItem(label="Speech bubble")
        toggle.set_active(self.show_bubble)
        toggle.connect("toggled", self.on_toggle_bubble)
        m.append(toggle)
        toast = Gtk.CheckMenuItem(label="Desktop notifications")
        toast.set_active(self.alerts.toast)
        toast.connect("toggled", self.on_toggle_toast)
        m.append(toast)
        sound = Gtk.CheckMenuItem(label="Alert sound")
        sound.set_active(self.alerts.sound)
        sound.connect("toggled", self.on_toggle_sound)
        m.append(sound)
        jump = Gtk.CheckMenuItem(label="Click jumps to terminal")
        jump.set_active(self.click_focus)
        jump.connect("toggled", self.on_toggle_click_focus)
        m.append(jump)
        m.append(Gtk.SeparatorMenuItem())

        items = [
            # Here as well as on the click, so the gesture is discoverable and
            # still reachable when the click has been turned off.
            ("Focus terminal", lambda *_: self.focus_terminal()),
            ("Wave", lambda *_: self.creature.cue("wave")),
            ("Jump!", lambda *_: self.creature.set_mode("jump", 45)),
            ("Nap now", lambda *_: self.creature.set_mode("sleep")),
        ]
        items += [
            (f"Costume: {name}", lambda _i, n=name: (
                self.creature.set_mode("working"), self.creature.set_costume(n)))
            for name in ("read", "write", "build", "scan", "cook")
        ]
        items.append(("Quit", self.on_quit))
        for label, fn in items:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", fn)
            m.append(item)
        m.show_all()
        # popup_at_pointer is 3.22+; the deprecated popup() is what older GTK3 has.
        if hasattr(m, "popup_at_pointer"):
            m.popup_at_pointer(ev)
        else:  # pragma: no cover - GTK < 3.22
            m.popup(None, None, None, None, ev.button, ev.time)


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


def install_quit_signals():
    """Quit cleanly on INT/TERM/HUP, on whichever binding this machine has.

    GLib's own handlers, not signal.signal(): Python defers its handlers until it
    regains control from Gtk.main(), which in practice never delivers them here -
    so the plain-signal route is the last resort, not the first choice.

    The three-argument form is what every version documents, but signal_add_full
    is the C function with the user_data/destroy pair still on it, and some
    bindings expose that arity, so a TypeError here means "right function, wrong
    shape" and is worth one retry rather than a dead daemon.
    """
    quit_cb = lambda *_: (Gtk.main_quit(), False)[1]  # noqa: E731
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        for attempt in (
            lambda s: unix_signal_add(GLib.PRIORITY_HIGH, s, quit_cb),
            lambda s: unix_signal_add(GLib.PRIORITY_HIGH, s, quit_cb, None),
            lambda s: GLib.unix_signal_add(GLib.PRIORITY_HIGH, s, quit_cb),
        ):
            try:
                attempt(sig)
                break
            except (TypeError, AttributeError):
                continue
        else:
            signal.signal(sig, lambda *_: Gtk.main_quit())
            print(f"warn: GLib signal handler unavailable, using signal.signal for {sig}",
                  flush=True)


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
    # 400 is the sum of the parts at scale 3: body (96), the tallest hop (46), the
    # gap above his head (10), and the hover panel that replaces the bubble (198
    # at the ten rows phase 2 brought), plus slack. Everything above him is
    # transparent and click-through, so spare height costs nothing but window
    # size, and the panel clamps if it ever wants more than there is.
    p.add_argument("--height", type=int, default=400, help="strip height in px")
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

    install_quit_signals()
    Deck(args)
    Gtk.main()
    PIDFILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
