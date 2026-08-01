#!/usr/bin/env python3
"""Who is waiting on you, for how long, and how loudly to say so.

No GTK, no drawing, no knowledge of the creature. It is handed the live sessions
on every tick and answers one question: *is there something you should look at,
and has it been ignored long enough to be rude about it.*

    alerts = Alerts(toast=True, sound=False)
    alerts.update(store.live(), now)
    a = alerts.current()          # None, or the one thing worth showing

Three rules, in the order they matter:

**Clearing beats raising.** The hook writes `alert` on an attention event and
leaves it empty on every other event, so answering a permission prompt in the
terminal clears the badge on the very next hook - no timeout, no polling, no
second signal to go wrong. An alarm that keeps ringing after you have dealt with
it is worse than no alarm.

**One alert per session, not one per event.** Entries are keyed on
`(session_id, kind)` and keep the time they were *first* seen, so ten
notifications in a row are one badge whose escalation clock does not restart.

**Failures do not toast.** A failed grep is a normal part of a session. It gets
the red badge for as long as it is the last thing that happened and nothing
more. Only the two kinds that actually mean "stopped" - waiting on you, and the
session itself failing - are allowed to reach for the desktop.
"""

import os
import shutil
import subprocess
import time

# Every notification_type Claude Code 2.1.220 can send, read out of the
# installed binary - see phases/phase-3-attention.md for how, and re-check it
# after an upgrade. The six that mean you are the blocker:
#
#   permission_prompt        a tool is waiting on your yes/no
#   idle_prompt              "Claude is waiting for your input"
#   elicitation_dialog       an MCP server is asking you something
#   elicitation_url_dialog
#   agent_needs_input
#   worker_permission_prompt
#
# They are not listed as a set on purpose. Only the two tables below change
# behaviour, and everything absent from both is treated as blocking: a type we
# do not recognise is far more likely to be a new way of saying "you are the
# blocker" than a new kind of shrug, and being wrong that way costs a badge you
# did not need rather than the silence you did.
FAILURES = {"tool_failed", "session_failed", "session_died"}   # red, not amber
QUIET = {"tool_failed",            # a failed grep is a normal part of a session
         "auth_success", "computer_use_enter", "computer_use_exit",
         "agent_completed", "elicitation_complete", "elicitation_response"}

# Escalation thresholds, in seconds. `Alerts(first=..., repeat=...)`, wired to
# prefs.json as `alert_secs` and `alert_repeat_secs`.
#
# 10s looks aggressive and is not: Claude Code has already sat on the
# notification for ~6s of you not touching the keyboard before it sends one at
# all, so the clock here starts from a point where you are provably not
# looking, and 10s on top means ~16s of a prompt going unanswered. The first
# pass shipped 60s and the honest result was that the toast never fired in
# normal use - every prompt was either answered or acknowledged first, which is
# an alarm that is only theoretically an alarm.
FIRST_TOAST = 10
REPEAT_TOAST = 300

LEVEL_BADGE, LEVEL_TOASTED, LEVEL_REPEATED = 0, 1, 2


def _secs(value, fallback):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


class Alert:
    """One thing wanting your attention. `since` is when it *first* appeared."""

    __slots__ = ("session_id", "kind", "message", "since", "level", "acked")

    def __init__(self, session_id, kind, message, since):
        self.session_id = session_id
        self.kind = kind
        self.message = message
        self.since = since
        self.level = LEVEL_BADGE
        self.acked = False

    @property
    def failed(self):
        return self.kind in FAILURES

    @property
    def loud(self):
        """Whether this kind is allowed to reach the desktop at all."""
        return self.kind not in QUIET

    @property
    def frame(self):
        """Which frame of the `alert` prop: amber for waiting, red for broken."""
        return 1 if self.failed else 0

    def waited(self, now):
        return max(0.0, now - self.since)

    def __repr__(self):
        return f"<Alert {self.kind} {self.message!r} level={self.level}>"


class Alerts:
    """The escalation state machine. Feed it sessions, ask it what to show."""

    def __init__(self, toast=True, sound=False, first=FIRST_TOAST,
                 repeat=REPEAT_TOAST, notifier=None):
        self.toast = bool(toast)
        self.sound = bool(sound)
        # These two come from a file people are told they can hand-edit, so a
        # typo in it must not take the daemon down with it. Fail quiet.
        self.first = max(1, _secs(first, FIRST_TOAST))
        self.repeat = max(self.first + 1, _secs(repeat, REPEAT_TOAST))
        # Injectable so the tests can watch what would have been sent without
        # putting a real toast on a real desktop.
        self.notifier = notifier or Notifier()
        self.live = {}            # (session_id, kind) -> Alert
        self._local = None        # an alert the daemon raised about itself

    # ------------------------------------------------------------------ input
    def update(self, sessions, now=None):
        """Reconcile against the sessions as they are right now, then escalate."""
        now = time.time() if now is None else now
        seen = set()
        for s in sessions or ():
            kind = getattr(s, "alert", "")
            # Type, not just emptiness. The key below goes into a set, so a
            # non-string here is not a wrong badge, it is an unhashable-type
            # error that takes the daemon's whole tick with it. `Session`
            # coerces already; this is the second lock on the same door,
            # because `update` accepts anything session-shaped.
            if not kind or not isinstance(kind, str):
                continue                      # this session is not asking for anything
            key = (getattr(s, "id", ""), kind)
            seen.add(key)
            existing = self.live.get(key)
            if existing is None:
                self.live[key] = Alert(key[0], kind,
                                       getattr(s, "alert_label", "") or kind, now)
            else:
                # Same alert, said again. Keep `since` - restarting it here is
                # how an alarm that fires every 30s never reaches its own
                # escalation and stays a badge forever.
                existing.message = getattr(s, "alert_label", "") or existing.message
        if self._local is not None:
            key = (self._local.session_id, self._local.kind)
            self.live.setdefault(key, self._local)
            seen.add(key)
        for gone in set(self.live) - seen:
            del self.live[gone]               # answered, or the session ended
        self._escalate(now)

    def local(self, kind, message, now=None):
        """An alert the daemon raised itself, about a session that stopped
        answering. There is no hook for a terminal that was closed mid-tool -
        the only evidence is silence, so the daemon has to speak for it."""
        now = time.time() if now is None else now
        if self._local is None or self._local.kind != kind:
            self._local = Alert("", kind, message, now)
            self.live[("", kind)] = self._local

    def clear_local(self):
        if self._local is not None:
            self.live.pop((self._local.session_id, self._local.kind), None)
            self._local = None

    # ----------------------------------------------------------- escalation
    def _escalate(self, now):
        """Cross the thresholds, then say everything that crossed *once*.

        Collected first rather than announced as we go, because with two
        sessions blocked they cross in the same pass and you got two popups
        fighting over the same corner of the screen. Two popups do not tell you
        twice as much as one that says "2 sessions".
        """
        firing = []
        for a in self.live.values():
            if a.acked or not a.loud:
                continue
            waited = a.waited(now)
            if a.level < LEVEL_TOASTED and waited >= self.first:
                a.level = LEVEL_TOASTED
                firing.append((a, False))
            elif a.level < LEVEL_REPEATED and waited >= self.repeat:
                a.level = LEVEL_REPEATED
                firing.append((a, True))
        if firing:
            self._speak(firing, now)

    def _speak(self, firing, now):
        """Say it out loud. Two independent channels, three levels of rudeness.

        The badge is passive and always there. A **toast** covers a corner of
        your screen, so it is for when you are looking at the screen and looking
        at something else. A **sound** reaches you when the screen is covered,
        or you are not at it - which is the case this whole phase exists for.

        They are separate flags because those are separate situations, and
        wanting the second without the first is an ordinary preference: no
        popup over my work, just tell me. Gating the beep behind the toast
        turned "Alert sound: on, Desktop notifications: off" into total
        silence, which is a checkbox that lies about what it does.

        `firing` is a list because several sessions can cross at the same
        instant. However many there are, this makes at most one toast and at
        most one beep.
        """
        failed = any(a.failed for a, _ in firing)
        again = any(rep for _, rep in firing)
        said = []
        if self.toast:
            if len(firing) == 1:
                alert, repeated = firing[0]
                title = "Claude Code failed" if failed else "Claude Code needs you"
                body = alert.message
                if repeated:
                    body += f"  ({int(alert.waited(now) // 60)} min)"
            else:
                title = f"{len(firing)} sessions need you"
                body = " · ".join(a.message for a, _ in firing[:3])
                if len(firing) > 3:
                    body += f" · and {len(firing) - 3} more"
            said.append("toast " + ("sent" if self.notifier.toast(
                title, body, urgent=failed or again) else "FAILED"))
        if self.sound:
            # With the toast, not held back for the 5-minute repeat: the one
            # signal that reaches you when the screen is covered should not be
            # the one you are least likely to ever get.
            said.append("beep " + ("sent" if self.notifier.beep() else "FAILED"))
        # Logged, because "why am I not getting a toast" is otherwise
        # unanswerable from outside the process: the badge, the toast and the
        # sound are three channels and any one of them can be the one that
        # failed.
        waited = max(a.waited(now) for a, _ in firing)
        kinds = ", ".join(sorted({a.kind for a, _ in firing}))
        print(f"{' + '.join(said) if said else 'badge only (toast and sound both off)'}"
              f" after {waited:.0f}s: {kinds}"
              f"{'' if len(firing) == 1 else f' (x{len(firing)})'}", flush=True)

    # ----------------------------------------------------------------- output
    def current(self):
        """The one alert worth drawing: unacked, failures first, then oldest.

        Phase 4 gives every session its own avatar and its own badge. Until
        then he has one shoulder, so this picks.
        """
        pick = [a for a in self.live.values() if not a.acked]
        if not pick:
            return None
        return sorted(pick, key=lambda a: (not a.failed, a.since))[0]

    def pending(self):
        return [a for a in self.live.values() if not a.acked]

    # ------------------------------------------------------------ user action
    def ack(self, now=None):
        """You clicked him. Everything currently showing goes quiet, and stays
        quiet until it clears on its own - a second permission prompt is a new
        alert and gets a new badge."""
        now = time.time() if now is None else now
        hit = False
        for a in self.live.values():
            if not a.acked:
                a.acked = hit = True
        return hit


class Notifier:
    """`notify-send` and `paplay`, both entirely optional.

    Neither is a dependency: a machine without them degrades to the badge on his
    shoulder, which is the part that actually works everywhere. Nothing here
    blocks, and nothing here raises.
    """

    # The freedesktop sound theme ships Ogg, and most of the obvious players
    # cannot read it. `aplay` in particular does not fail: it takes the .oga as
    # raw 8-bit 8kHz PCM and plays the file's bytes as noise, at exit code 0,
    # which is worse than silence. Order matters and the file does not go to
    # every player.
    #
    #   canberra-gtk-play   plays a theme *id*, no path - the correct one
    #   pw-play / paplay    handle Ogg, take a path
    #   aplay               ALSA, WAV only - so it gets a WAV or nothing
    PLAYERS = (
        ("canberra-gtk-play", ["-i", "message"]),
        ("pw-play", None),
        ("paplay", None),
        ("aplay", None),
    )
    SOUNDS = (
        "/usr/share/sounds/freedesktop/stereo/message.oga",
        "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga",
    )
    WAV_SOUNDS = ("/usr/share/sounds/alsa/Front_Center.wav",)

    def __init__(self):
        self.send = shutil.which("notify-send")
        self.sound_file = next((p for p in self.SOUNDS if os.path.exists(p)), None)
        self.play = None
        self.beep_argv = None
        for name, fixed in self.PLAYERS:
            path = shutil.which(name)
            if not path:
                continue
            if fixed is not None:
                self.play, self.beep_argv = path, [path] + fixed
                break
            wanted = self.WAV_SOUNDS if name == "aplay" else self.SOUNDS
            sound = next((p for p in wanted if os.path.exists(p)), None)
            if sound:
                self.play, self.beep_argv = path, [path, sound]
                break
        self.sent = []            # what we asked for, for the tests and the log

    def _run(self, argv):
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             start_new_session=True)
            return True
        except OSError:           # binary vanished between which() and now
            return False

    def toast(self, title, body, urgent=False):
        self.sent.append(("toast", title, body))
        if not self.send:
            return False
        return self._run([self.send, "-a", "deck guy",
                          "-u", "critical" if urgent else "normal",
                          "-h", "string:x-canonical-private-synchronous:deck-guy",
                          title, body])

    def beep(self):
        self.sent.append(("beep",))
        if not self.beep_argv:
            return False
        return self._run(self.beep_argv)


if __name__ == "__main__":       # a quick look at what this machine can do
    import sys

    n = Notifier()
    print(f"notify-send  {n.send or 'NOT INSTALLED - badge only, no toasts'}")
    print(f"sound        {' '.join(n.beep_argv) if n.beep_argv else 'NO USABLE PLAYER'}")
    print(f"thresholds   first toast {FIRST_TOAST}s unanswered, one repeat at "
          f"{REPEAT_TOAST}s; the sound goes with both")
    if "--quiet" not in sys.argv:
        print("\nsending one of each now...")
        print(f"  toast {'sent' if n.toast('deck guy', 'this is an alert') else 'FAILED'}")
        print(f"  beep  {'sent' if n.beep() else 'FAILED'}")
