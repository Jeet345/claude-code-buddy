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
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, GObject, Gtk  # noqa: E402

import vector  # noqa: E402

import cairo  # noqa: E402

# Pure data, no GTK. The tool-to-costume table lives with the art it names so that
# the daemon and preview.png can never disagree about who wears what.
import costume  # noqa: E402
from sprites import COSTUMES, TOOL_COSTUME  # noqa: E402

import layout  # noqa: E402
import prices  # noqa: E402
import windows  # noqa: E402
from alerts import Alerts  # noqa: E402
from bridge import Approvals, Bridge  # noqa: E402
from bubble import (HIGH, WARN, BAR_HIGH, BAR_OK, Bubble, Card, Meter, Panel,  # noqa: E402
                    bar_colour, place)
from vector_motion import Spring  # noqa: E402
from sessions import ROOT as SESSIONS_ROOT, SessionStore, clock, describe, elide  # noqa: E402

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
SIZES = {"Small": 3, "Medium": 4, "Large": 5, "Huge": 6}   # right-click -> Size
DEFAULT_SCALE = 4
WALK_RATE = 2.2       # how quickly avatars and sidekicks close on their spot, 1/s
AVATAR_DIV = 2        # other sessions stand beside him at half his size
SIDEKICK_DIV = 3      # subagents trail behind him, smaller still
FOCUS_POLL = 1.0      # secs between "did you go back to the terminal?" checks
LISTEN_AFTER = 20.0   # secs of waiting on you before the headphones go on
CARD_ARM = 1.0        # secs a new prompt's buttons ignore clicks: it can pop up
                      # right where you were already clicking something else
CMD_LINE = 64         # chars per line of a held command on the approval card
CMD_LINES = 4         # lines of it shown before saying how much is left out
BADGE = {4: (0.80, 0.58, 0.18), 3: BAR_HIGH, 2: BAR_OK}   # avatar dot by priority


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

    @property
    def jump_phase(self):
        """"crouch" / "air" / "land" mid-jump, else "". The rig reads it for the
        anticipation squash, the landing dust and the confetti."""
        return getattr(self, "_jump_phase", "") if self.mode == "jump" else ""

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
        prefs0 = self._load_prefs()
        scale = args.scale or int(prefs0.get("scale", DEFAULT_SCALE))
        self.sp = vector.Sheet(scale if scale in SIZES.values() else DEFAULT_SCALE)

        geo = primary_geometry()
        self.win_w, self.win_h = geo.width, self._strip_height()
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
        self.bubble = Bubble(self.sp.scale)
        self.panel = Panel(self.sp.scale)
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

        # Phase 5: answering permission prompts from here. The card replaces the
        # bubble while a prompt is held; the bridge is the socket the deck-buddy
        # mod asks over. Off by default - it is the one feature that acts.
        self.card = Card(self.sp.scale)
        self._card_rect = None
        self._card_hot = ""
        self._press_btn = ""
        self._card_since = 0.0
        self._focus_check = 0.0
        self.approvals = Approvals(
            enabled=bool(prefs.get("approve", False)),
            focused=self._focused_threadsafe,
            on_change=lambda: GLib.idle_add(self._approvals_changed))
        self.bridge = None
        if not args.demo:
            self.bridge = Bridge(SESSIONS_ROOT / "buddy.sock", self.approvals,
                                 prefs=self._bridge_prefs, cue=self._bridge_cue).start()
        print(f"approve from buddy: {self.approvals.enabled}", flush=True)

        # Phase 4: every other live session stands beside him; the featured
        # session's subagents trail behind. One Rig each, keyed by id.
        self._avatars = {}
        self._sidekicks = {}
        self._avatar_rects = []       # [(session_id, rect)]
        self._sidekick_rects = []     # [(agent_id, rect)]
        self._more = []               # [((x, y), n)] "+n" labels
        self._hover_sid = None        # an avatar under the pointer
        self._pointer = None          # (x, y, when) of the last motion over him
        # Readouts pop in: a spring from slightly small to rest, with a touch
        # of overshoot. Keyed on which readout is up, so a new card pops and a
        # ticking timer does not.
        self._pop = Spring(1.0, k=260.0, damping=0.55)
        self._pop_key = None
        # Costume props spring onto him from nothing, with a wobble of overshoot.
        self._prop_pop = Spring(1.0, k=220.0, damping=0.42)
        self._last_costume = None
        self._last_others = []
        self._walk = {}               # id -> drawn x, easing toward its layout spot
        self._celebrated = None       # (session, ts) of the last turn he jumped for
        self._prev_featured = None
        self._leaving = []            # [rig, x, y, w, h, dir, alpha] walking off

        # No polling: notify.sh writes a file per session and the store watches
        # the directory. --demo drives him from the script below instead.
        self.store = None
        if not args.demo:
            self.store = SessionStore(on_change=self.on_sessions).start()

        self.show_all()
        self._tick_id = GLib.timeout_add(1000 // TICK_HZ, self.tick)

    # ------------------------------------------------------------------ state
    def _strip_height(self):
        """Tall enough for him, his highest hop, and the tallest readout."""
        return max(self.args.height or 0, self.sp.h + 340)

    def set_scale(self, scale):
        """Resize him live, from the Size menu. Remembered in prefs.json."""
        if scale == self.sp.scale:
            return
        self.sp = vector.Sheet(scale)
        self.creature.sp = self.sp
        self.bubble, self.panel, self.card = Bubble(scale), Panel(scale), Card(scale)
        self._avatars.clear()
        self._sidekicks.clear()
        self._walk.clear()
        geo = primary_geometry()
        self.win_h = self._strip_height()
        self.ground_y = self.win_h - GROUND_MARGIN
        self.resize(self.win_w, self.win_h)
        self.move(geo.x, geo.y + geo.height - self.win_h)
        self._fence()
        self.creature.x = self.creature.clamp(self.creature.x)
        self._last_key = None
        self.queue_draw()
        self._save_prefs()
        print(f"size -> scale {scale}", flush=True)

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
                          "click_focus": self.click_focus,
                          "approve": self.approvals.enabled,
                          "scale": self.sp.scale})
            PREFS.write_text(json.dumps(prefs, indent=2))
        except OSError:
            pass

    # ---------------------------------------------------------------- sessions
    def on_sessions(self, store):
        """A session file changed. Phase 1 mirrors the most recent one only.

        Called by the file monitor, not on a timer, so there is nothing to do
        here when Claude Code is quiet.
        """
        self._sync_approvals(store)
        s = store.featured()
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
            # Once per finished turn. The mod rewrites the session file every
            # 15s as a heartbeat and the state stays "jump" until the next
            # prompt, so reacting to the state alone celebrated on every beat.
            key = (s.id, s.ts)
            if key != self._celebrated and now - s.ts < 15:
                self._celebrated = key
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

    def _sync_approvals(self, store):
        """Release held prompts whose session no longer names them.

        Interrupting the turn in the terminal, or the session dying, leaves the
        mod's request abandoned; the buddy must stop offering a button for it.
        """
        for sess in store.sessions.values():
            self.approvals.sync(sess.id, (sess.pending or {}).get("id"))
        now = time.time()
        for req in self.approvals.held():
            if req.session_id not in store.sessions and now - req.since > 3.0:
                self.approvals.answer(req.id, "ask")

    # ---------------------------------------------------------------- approvals
    def _focused(self, session_id):
        s = self.store.sessions.get(session_id) if self.store else None
        if s is None:
            return None
        return windows.focused(s.pids, s.project)

    def _focused_threadsafe(self, session_id):
        """`_focused`, from the bridge's thread. GDK is main-loop only."""
        box, done = [], threading.Event()

        def probe():
            try:
                box.append(self._focused(session_id))
            except Exception:
                box.append(None)
            finally:
                done.set()
            return False

        GLib.idle_add(probe)
        return box[0] if done.wait(1.5) and box else None

    def _approvals_changed(self):
        req = self.approvals.current()
        key = req.id if req else None
        if key and key != getattr(self, "_approval_key", None):
            self._card_since = time.time()
            print(f"holding prompt {req!r}", flush=True)
            c = self.creature
            c.last_activity = time.time()
            if c.mode == "sleep":
                c.set_mode("idle")
            c.cue("wave")
        self._approval_key = key
        return False

    def _bridge_prefs(self, d):
        if isinstance(d.get("approve"), bool):
            self.approvals.enabled = d["approve"]
            if not self.approvals.enabled:
                self.approvals.release()
            GLib.idle_add(lambda: (self._save_prefs(), False)[1])
            print(f"approve from buddy: {self.approvals.enabled} (by /buddy)", flush=True)
        return {"approve": self.approvals.enabled}

    def _bridge_cue(self, d):
        cue = d.get("cue")
        if cue not in ("wave", "jump"):
            return {"ok": False}
        GLib.idle_add(lambda: (self.creature.cue("wave") if cue == "wave"
                               else self.creature.set_mode("jump", 12), False)[1])
        return {"ok": True}

    def answer_card(self, bid):
        req = self.approvals.current()
        if req is None:
            return
        sess = self.store.sessions.get(req.session_id) if self.store else None
        if bid == "remember":
            self.approvals.answer(req.id, "allow", remember=True)
            self.say("allowed - again for 10 min")
        elif bid in ("allow", "deny"):
            self.approvals.answer(req.id, bid)
            self.say("allowed" if bid == "allow" else "denied")
        else:
            self.approvals.answer(req.id, "ask")
            if sess is not None:
                self.focus_terminal(sess)
        print(f"answered {req!r}: {bid}", flush=True)
        self.creature.cue("wave")

    def card_content(self, req, now):
        sess = self.store.sessions.get(req.session_id) if self.store else None
        cwd = sess.cwd if sess else ""
        project = (sess.project if sess else "") or "session"
        who = f"{project} · subagent" if req.agent else project
        cmd = req.input.get("command", "")
        if cmd:
            # The exact command, not the shortened step label: this is what you
            # are approving, so every character of it is shown or accounted for.
            text = " ".join(cmd.split())
            lines = [text[i:i + CMD_LINE] for i in range(0, len(text), CMD_LINE)]
            shown = lines[:CMD_LINES]
            if len(lines) > CMD_LINES:
                left = len(text) - CMD_LINE * CMD_LINES
                shown.append(f"… {left} more chars - read it all In terminal")
            title = req.tool
            body = shown
        else:
            title = elide(f"{req.tool}  {describe(req.tool, req.input, cwd)}".strip(), 70)
            path = req.input.get("file_path", "")
            body = [elide(path, 70)] if path and path != describe(req.tool, req.input, cwd) else []
        reason = req.reason or "needs your permission"
        lines = body + [elide(f"{who} · {reason}", 70)]
        waiting = len(self.approvals.held()) - 1
        if waiting > 0:
            lines.append(f"+{waiting} more waiting")
        buttons = [("allow", "Allow"), ("remember", "Allow 10 min"),
                   ("deny", "Deny"), ("ask", "In terminal")]
        if req.risk:
            # Risky: say what is risky first, and do not offer a standing pass.
            title = f"⚠ {req.risk}  ·  {title}"
            buttons = [b for b in buttons if b[0] != "remember"]
        return title, lines, buttons

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
        req = self.approvals.current()
        if req is not None:
            title, lines, buttons = self.card_content(req, now)
            self.card.set(title, lines, buttons, timer=clock(now - req.since),
                          hot=self._card_hot, danger=bool(req.risk))
        else:
            self.card.set("")

        featured = self.store.featured() if self.store else None
        hovered = self.store.sessions.get(self._hover_sid) if (self.store and self._hover_sid) else None
        s = hovered or featured
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
        elif featured and featured.state == "working" and featured.label:
            self.bubble.set(featured.label, clock(featured.elapsed(now)))
            self._bubble_until = now + BUBBLE_LINGER
        elif featured and featured.state == "jump":
            self.bubble.set("done")
            self._bubble_until = min(self._bubble_until, now + BUBBLE_LINGER)

        if now > self._bubble_until + BUBBLE_FADE:
            self.bubble.set("")

        if not (self._hover or hovered):
            self.panel.set([])
        elif s:
            rows = [("project", s.project or "-"),
                    ("session", clock(s.runtime(now))),
                    ("step", s.label or "idle"),
                    ("elapsed", clock(s.elapsed(now)))]
            if s.permission_mode:
                rows.insert(1, ("mode", s.permission_mode))
            if s.agents:
                first = s.agents[0]
                what = first.get("description") or first.get("type") or "subagent"
                rows.append(("subagents", elide(f"{len(s.agents)} · {what}", 40)))
            if len(self.store.sessions) > 1:
                rows.append(("sessions", f"{len(self.store.sessions)} live"))
            self.panel.set(rows + self.alert_rows(alert if s is featured else None, now)
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
        if getattr(m, "live", False):
            # Measured by the engine: exact window, Claude Code's own cost.
            rows = [("context", f"{pct}  {prices.tokens(m.context)} / "
                                f"{approx}{prices.tokens(m.window)}", m.window_approx),
                    Meter(m.fill, dim=m.window_approx),
                    ("cost", prices.money(m.cost)),
                    ("model", f"{m.model or 'unknown'} {m.effort}".strip(), not m.model)]
            return rows
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
        s = self.store.featured() if self.store else None
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
        px = self.sp.scale
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
        widget = self.readout_widget()
        if not widget.visible or (widget is self.bubble and self.readout_alpha(now) <= 0):
            return None
        w, h = widget.size()
        x, y = place(self.readout_cx, self.readout_y, w, h, self.win_w)
        return (x, y, w, h)

    def readout_widget(self):
        """Card over panel over bubble: a question for you outranks a hover."""
        if self.card.visible:
            return self.card
        return self.panel if self.panel.visible else self.bubble

    @property
    def readout_cx(self):
        """Above whichever body the readout is about: an avatar when hovering one."""
        if self._hover_sid and not self.card.visible:
            for sid, (x, _y, w, _h) in self._avatar_rects:
                if sid == self._hover_sid:
                    return x + w / 2
        return self.creature.x

    DEMO = [
        ("idle", 4.0), ("patrol", 6.0), ("wave", 2.0),
        ("read", 5.0), ("write", 5.0), ("build", 5.0), ("scan", 5.0), ("cook", 7.0),
        ("listen", 6.0), ("jump", 3.0), ("alert", 5.0), ("error", 5.0),
        ("idle", 2.0), ("sleep", 4.0), ("idle", 3.0),
    ]
    DEMO_LABELS = {
        "read": "Read sessions.py", "write": "Edit src/auth.py",
        "build": "Bash pytest -q", "scan": "WebFetch docs.anthropic.com",
        "cook": "Task Explore the repo", "jump": "done",
    }

    def put_on_headphones(self, now):
        """Skip the wait: idle, as if nobody had needed him for LISTEN_AFTER.
        Anything that really happens next takes them off as usual."""
        self.creature.set_costume(None, now)
        self.creature.set_mode("idle")
        self.creature.last_activity = now - LISTEN_AFTER - 0.1

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
        elif mode == "listen":
            self.put_on_headphones(now)
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
        self.update_face(now)
        self.rig.update(dt, self.creature)
        self.update_others(dt)
        self.poll_focus(now)
        self.update_pop(dt)
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
        readout = (self.bubble.text, self.bubble.timer, self.panel.key(), self.card.key(),
                   round(self.readout_alpha(now), 2), self.context_level(),
                   round(self._pop.x, 3), round(self._prop_pop.x, 3))
        rect = self.readout_rect(now)
        # The sprite body only changed when `frame` did, so the frame index was
        # a sound repaint key. A spring-driven body changes every tick, so the
        # tick counter goes in instead - except while asleep, where the rig is
        # effectively still and the old economy is worth keeping.
        self._seq = getattr(self, "_seq", 0) + 1
        moving = 0 if c.mode == "sleep" else self._seq
        others = self.other_rects()
        key = (c.anim, c.frame, c.facing, bbox, phase, readout, moving, tuple(others))
        if key != getattr(self, "_last_key", None):
            self._last_key = key
            pad = 90  # room for props, sparkles and the zzz trail
            for b in filter(None, (self._last_bbox, bbox)):
                self.queue_draw_area(b[0] - pad, b[1] - pad, b[2] + pad * 2, b[3] + pad * 2)
            for r in filter(None, (self._last_readout, rect)):
                # padded for the pop's overshoot, which draws a little outside
                padx, pady = int(r[2] * 0.06) + 4, int(r[3] * 0.12) + 4
                self.queue_draw_area(r[0] - padx, r[1] - pady, r[2] + padx * 2, r[3] + pady + 4)
            for r in set(self._last_others) | set(others):
                self.queue_draw_area(r[0] - 30, r[1] - 16, r[2] + 60, r[3] + 20)
            if (bbox != self._last_bbox or rect != self._last_readout
                    or others != self._last_others):
                self._update_input_shape(bbox, rect, others)
            self._last_bbox = bbox
            self._last_readout = rect
            self._last_others = others

        # Tick only as fast as the current posture needs. Motion wants 30Hz; standing
        # still only has to outrun the animation's own fps, and sleeping barely that.
        want = MODE_HZ.get(c.mode, TICK_HZ)
        if want != getattr(self, "_hz", TICK_HZ):
            self._hz = want
            GLib.timeout_add(1000 // want, self.tick)
            return False
        return True

    def update_pop(self, dt):
        costume = self.creature.costume if self.creature.mode == "working" else None
        if costume != self._last_costume:
            if costume:
                self._prop_pop.snap(0.15)
                self._prop_pop.target = 1.0
            self._last_costume = costume
        self._prop_pop.step(dt)
        widget = self.readout_widget()
        key = None
        if widget.visible:
            key = (type(widget).__name__,
                   self.approvals.current().id if widget is self.card and self.approvals.current() else
                   (self._hover_sid if widget is self.panel else bool(self.bubble.text)))
        if key != self._pop_key:
            if key is not None and (self._pop_key is None or key[0] != self._pop_key[0]
                                    or widget is self.card):
                self._pop.snap(0.86)
                self._pop.target = 1.0
            self._pop_key = key
        self._pop.step(dt)

    # ------------------------------------------------------------------ face
    def update_face(self, now):
        """What his face says beyond the posture: the session's phase, a
        question waiting on you, and where your pointer is.

        Only while he is not mid-gesture: a jump, a wave or a slump already
        has a face of its own and is over in a second or two.
        """
        c = self.creature
        expression = None
        s = self.store.featured() if self.store else None
        if c.anim in ("idle", "working", "patrol") or c.anim in COSTUMES:
            if self.approvals.current() is not None or (s and s.priority == 4):
                expression = "worried"
            elif s and s.state == "working" and s.phase == "thinking":
                expression = "thinking"
            elif s and s.state == "working" and s.phase == "responding":
                expression = "writing"
        # Nothing to do and nobody asking: headphones on until there is.
        # Sleep, a session starting work, or a prompt all take them off.
        listening = (expression is None and c.mode in ("idle", "patrol")
                     and now - c.last_activity > LISTEN_AFTER
                     and not (s and s.state == "working"))
        self.rig.listening = listening
        if listening and c.anim in ("idle", "blink", "patrol"):
            expression = "vibing"
        c.expression = expression

        face = self.rig.face
        if self.card.visible:
            face.look(0.0, -1.0, hold=0.3)          # up at the question
        elif self._pointer and now - self._pointer[2] < 1.5:
            px, py, _ = self._pointer
            bx, by, bw, bh = c.bbox(self.ground_y)
            dx = (px - (bx + bw / 2)) / max(1.0, bw * 1.5) * c.facing
            dy = (py - (by + bh * 0.45)) / max(1.0, bh * 1.5)
            face.look(dx, dy, hold=0.3)

    # ------------------------------------------------------------- the others
    @staticmethod
    def _anim_for(s):
        if s.state == "working":
            return TOOL_COSTUME.get(s.tool) or "idle"
        if s.state == "error":
            return "error"
        return "idle"

    def update_others(self, dt):
        """Lay out and step the avatars and sidekicks. Cheap: a few rects."""
        if not self.store:
            return
        c = self.creature
        featured = self.store.featured()
        others = self.store.others(featured)
        stepped_out = self._prev_featured if (featured and self._prev_featured
                                               and self._prev_featured != featured.id) else None
        self._prev_featured = featured.id if featured else None
        aw, ah = self.sp.w // AVATAR_DIV, self.sp.h // AVATAR_DIV
        row = layout.avatar_row(c.x, self.sp.w, self.ground_y, (aw, ah),
                                len(others), self.win_w)
        ids = [s.id for s in others[:len(row.rects)]]
        if row.side < 0:
            ids.reverse()
        self._avatar_rects = list(zip(ids, row.rects))
        self._more = [(row.more_at, row.more)] if row.more else []

        agents = featured.agents if featured else []
        kw, kh = self.sp.w // SIDEKICK_DIV, self.sp.h // SIDEKICK_DIV
        krow = layout.sidekick_row(c.x, self.sp.w, self.ground_y, (kw, kh),
                                   len(agents), self.win_w, avoid=row.side if others else 0)
        kids = [str(a.get("id", i)) for i, a in enumerate(agents[:len(krow.rects)])]
        if krow.side < 0:
            kids.reverse()
        self._sidekick_rects = list(zip(kids, krow.rects))
        if krow.more:
            self._more.append((krow.more_at, krow.more))

        live_ids = {sid for sid, _ in self._avatar_rects}
        for sid in list(self._avatars):
            if sid not in live_ids:
                # a session that ended walks off the edge rather than vanishing;
                # one that became the featured session just merges into him
                rig = self._avatars.pop(sid)
                x = self._walk.pop(sid, None)
                if x is not None and not (featured and sid == featured.id):
                    _y, w, h = self.ground_y - self.sp.h // AVATAR_DIV, self.sp.w // AVATAR_DIV, self.sp.h // AVATAR_DIV
                    away = 1 if x > c.x else -1
                    self._leaving.append([rig, x, _y, w, h, away, 1.0])
        for sid, (tx, _y, w, _h) in self._avatar_rects:
            rig = self._avatars.get(sid)
            if rig is None:
                rig = self._avatars[sid] = vector.Rig()
                if sid == stepped_out:
                    # he was showing this one a moment ago: it steps out of him
                    self._walk[sid] = c.x - w / 2
                else:
                    # a new session walks in from beyond the far end of the row
                    side = 1 if tx > c.x else -1
                    self._walk[sid] = tx + side * (w * 2.5)
            s = self.store.sessions.get(sid)
            moving = self._ease_walk(sid, tx, dt)
            anim = "patrol" if moving else (self._anim_for(s) if s else "idle")
            expression = ("worried" if s and s.priority == 4 else
                          "thinking" if s and s.state == "working" and s.phase == "thinking" else None)
            rig.update(dt, SimpleNamespace(anim=anim, y_off=0.0, expression=expression,
                                           x=self._walk[sid], facing=1))
        kid_ids = {k for k, _ in self._sidekick_rects}
        for k in list(self._sidekicks):
            if k not in kid_ids:
                self._sidekicks.pop(k)
                self._walk.pop("k:" + k, None)
        for k, (tx, _y, w, _h) in self._sidekick_rects:
            rig = self._sidekicks.get(k)
            if rig is None:
                rig = self._sidekicks[k] = vector.Rig()
                self._walk["k:" + k] = c.x            # hatch from him, then trot off
            # sidekicks lag: ducklings, not a formation
            moving = self._ease_walk("k:" + k, tx, dt, rate=WALK_RATE * 0.55)
            rig.update(dt, SimpleNamespace(anim="patrol" if moving else "read", y_off=0.0,
                                           x=self._walk["k:" + k], facing=1))
        alive = []
        for item in self._leaving:
            rig, x, y, w, h, away, alpha = item
            item[1] = x + away * 90 * dt
            item[6] = alpha - dt * 1.4
            if item[6] > 0 and -w < item[1] < self.win_w:
                rig.update(dt, SimpleNamespace(anim="patrol", y_off=0.0, x=item[1], facing=away))
                alive.append(item)
        self._leaving = alive
        if self._hover_sid and self._hover_sid not in live_ids:
            self._hover_sid = None

    def _ease_walk(self, key, target, dt, rate=WALK_RATE):
        """Move a drawn x toward its spot; True while it is still on its way."""
        x = self._walk.get(key, target)
        x += (target - x) * min(1.0, dt * rate)
        self._walk[key] = x
        return abs(target - x) > 2.0

    def drawn_rect(self, key, rect):
        x = self._walk.get(key)
        return rect if x is None else (int(x), rect[1], rect[2], rect[3])

    def other_rects(self):
        rects = [self.drawn_rect(sid, r) for sid, r in self._avatar_rects]
        rects += [self.drawn_rect("k:" + k, r) for k, r in self._sidekick_rects]
        rects += [(int(i[1]), i[2], i[3], i[4]) for i in self._leaving]
        return rects

    def poll_focus(self, now):
        """Went back to the terminal while a prompt was held? Hand it over."""
        req = self.approvals.current()
        if req is None or now - self._focus_check < FOCUS_POLL:
            return
        self._focus_check = now
        if self._focused(req.session_id) is True:
            n = self.approvals.release(req.session_id)
            if n:
                print(f"released {n} prompt(s): you went back to the terminal", flush=True)

    def draw_others(self, cr, now):
        if not self.store:
            return
        c = self.creature
        for rig, x, y, w, h, away, alpha in self._leaving:
            cr.push_group()
            rig.draw(cr, x, y, w, h, away)
            cr.pop_group_to_source()
            cr.paint_with_alpha(max(0.0, alpha))
        for sid, rect in self._avatar_rects:
            x, y, w, h = self.drawn_rect(sid, rect)
            rig = self._avatars.get(sid)
            s = self.store.sessions.get(sid)
            if rig is None or s is None:
                continue
            if abs(x - rect[0]) > 2:                  # walking: face the way he goes
                facing = 1 if rect[0] > x else -1
            else:
                facing = -1 if x + w / 2 < c.x else 1  # standing: look at him
            rig.draw(cr, x, y, w, h, facing)
            if sid == self._hover_sid:
                cr.set_source_rgba(0.494, 0.227, 0.141, 0.8)
                cr.rectangle(x + 4, y + h + 2, w - 8, 2)
                cr.fill()
        for _k, rect in self._sidekick_rects:
            x, y, w, h = self.drawn_rect("k:" + _k, rect)
            rig = self._sidekicks.get(_k)
            if rig is not None:
                if abs(x - rect[0]) > 2:
                    facing = 1 if rect[0] > x else -1
                else:
                    facing = 1 if x < c.x else -1
                rig.draw(cr, x, y, w, h, facing)
        cr.select_font_face("DejaVu Sans Mono")
        cr.set_font_size(11)
        for at, n in self._more:
            if at:
                cr.set_source_rgba(0.16, 0.11, 0.09, 0.85)
                cr.move_to(*at)
                cr.show_text(f"+{n}")

    def _update_input_shape(self, bbox, readout=None, others=()):
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
        for ox, oy, ow, oh in others:
            region.union(cairo.RectangleInt(ox, oy, ow, oh))
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

        # Costumes the rig draws itself ride his body; only anything left in
        # the old keyframed table is still pinned on from out here.
        specs = [] if c.anim in costume.NAMES else c.attachment_specs()
        pop = max(0.0, self._prop_pop.x)

        def prop(key, ax, ay, rot):
            if pop >= 0.999:
                vector.draw_prop(cr, key, x + ax * s, y + ay * s, s, rotate=rot)
                return
            pw, ph = vector.prop_size(key)
            cx, cy = x + (ax + pw / 2) * s, y + (ay + ph / 2) * s
            cr.save()
            cr.translate(cx, cy)
            cr.scale(pop, pop)
            cr.translate(-cx, -cy)
            vector.draw_prop(cr, key, x + ax * s, y + ay * s, s, rotate=rot)
            cr.restore()

        for key, ax, ay, z, rot in specs:
            if z == "back":
                prop(key, ax, ay, rot)
        self.rig.draw(cr, x, y, w, h, c.facing)
        for key, ax, ay, z, rot in specs:
            if z != "back":
                prop(key, ax, ay, rot)

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
        self.draw_others(cr, now)

        # Readout last, on top of everything. The panel replaces the bubble rather
        # than stacking under it: the bubble's line is already the panel's `step`
        # row, and stacking would need a strip half again as tall.
        pop = self._pop.x
        if self.card.visible:
            self._card_rect = self.card.draw(cr, c.x, self.readout_y, self.win_w, scale=pop)
        elif self.panel.visible:
            self.panel.draw(cr, self.readout_cx, self.readout_y, self.win_w, scale=pop)
        else:
            self.bubble.draw(cr, c.x, self.readout_y, self.win_w,
                             self.readout_alpha(now), scale=pop)

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
        s = session or (self.store.featured() if self.store else None)
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

    def _card_button(self, x, y):
        if not self.card.visible:
            return ""
        w, h = self.card.size()
        origin = place(self.creature.x, self.readout_y, w, h, self.win_w)
        for bid, (bx, by, bw, bh) in self.card.buttons_at(origin).items():
            if bx <= x < bx + bw and by <= y < by + bh:
                return bid
        return ""

    def _avatar_at(self, x, y):
        i = layout.hit([self.drawn_rect(sid, r) for sid, r in self._avatar_rects], x, y)
        return self._avatar_rects[i][0] if i >= 0 else None

    def on_press(self, _w, ev):
        if ev.button == 1 and self._card_button(ev.x, ev.y):
            if time.time() - self._card_since < CARD_ARM:
                print(f"ignored a click on the card at ({ev.x:.0f},{ev.y:.0f}): "
                      f"it appeared under the pointer {time.time() - self._card_since:.2f}s ago",
                      flush=True)
                return True
            self._press_btn = self._card_button(ev.x, ev.y)
            print(f"card press {self._press_btn} at ({ev.x:.0f},{ev.y:.0f})", flush=True)
            return True
        if ev.button == 1 and self._avatar_at(ev.x, ev.y):
            self._press_btn = "avatar:" + self._avatar_at(ev.x, ev.y)
            return True
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
        # the pointer is on him, the readout, or another session's avatar.
        self._pointer = (ev.x, ev.y, time.time())
        self._card_hot = self._card_button(ev.x, ev.y)
        self._hover_sid = self._avatar_at(ev.x, ev.y)
        self._hover = self._hover_sid is None
        return True

    def on_leave(self, _w, _ev):
        self._hover = False
        self._hover_sid = None
        self._card_hot = ""
        return True

    def on_release(self, _w, ev):
        pressed, self._press_btn = self._press_btn, ""
        if pressed.startswith("avatar:"):
            sid = pressed.split(":", 1)[1]
            if self._avatar_at(ev.x, ev.y) == sid and self.store:
                s = self.store.sessions.get(sid)
                if s is not None:
                    self.focus_terminal(s)
            return True
        if pressed:
            if self._card_button(ev.x, ev.y) == pressed:
                self.answer_card(pressed)
            return True
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

    def on_toggle_approve(self, item):
        self.approvals.enabled = item.get_active()
        if not self.approvals.enabled:
            self.approvals.release()
        self._save_prefs()
        print(f"approve from buddy: {self.approvals.enabled}", flush=True)

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
        approve = Gtk.CheckMenuItem(label="Answer prompts from buddy")
        approve.set_active(self.approvals.enabled)
        approve.set_sensitive(self.bridge is not None)
        approve.connect("toggled", self.on_toggle_approve)
        m.append(approve)

        size_item = Gtk.MenuItem(label="Size")
        sizes = Gtk.Menu()
        group = None
        for label, scale in SIZES.items():
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
            group = item
            item.set_active(scale == self.sp.scale)
            item.connect("toggled", lambda it, sc=scale: it.get_active() and self.set_scale(sc))
            sizes.append(item)
        size_item.set_submenu(sizes)
        m.append(size_item)
        m.append(Gtk.SeparatorMenuItem())

        items = [
            # Here as well as on the click, so the gesture is discoverable and
            # still reachable when the click has been turned off.
            ("Focus terminal", lambda *_: self.focus_terminal()),
            ("Wave", lambda *_: self.creature.cue("wave")),
            ("Jump!", lambda *_: self.creature.set_mode("jump", 45)),
            ("Nap now", lambda *_: self.creature.set_mode("sleep")),
            ("Headphones on", lambda *_: self.put_on_headphones(time.time())),
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
    # 4 since the face got expressions: at 3 a mouth is under two pixels thick
    # and every mood reads as the same blank stare.
    p.add_argument("--scale", type=int, default=None,
                   help="pixel size (default: the Size menu's choice, else 4)")
    # 400 is the sum of the parts at scale 3: body (96), the tallest hop (46), the
    # gap above his head (10), and the hover panel that replaces the bubble (198
    # at the ten rows phase 2 brought), plus slack. Everything above him is
    # transparent and click-through, so spare height costs nothing but window
    # size, and the panel clamps if it ever wants more than there is.
    p.add_argument("--height", type=int, default=None,
                   help="strip height in px (default: fits his size)")
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
    deck = Deck(args)
    Gtk.main()
    if deck.bridge is not None:
        deck.bridge.stop()             # releases held prompts to their terminals
    PIDFILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
