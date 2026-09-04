"""Motion primitives for the vector renderer.

Springs with real overshoot, follow-through chains, blink timing taken from
human figures, breathing, and sustained oscillators for each animation's motion
signature.

Nothing here is keyed to wall-clock easing curves: every value is integrated, so
the motion stays correct at any frame rate and survives a dropped frame. That is
the difference between this and stepping through baked sprite frames -- the
sprite cadence was 2-12fps because each frame was hand-drawn; this runs at
whatever the tick gives it.
"""

import math
import random

_MAX_STEP = 1.0 / 240.0  # substep ceiling; keeps stiff springs stable
_MAX_FRAME = 0.1  # ignore hitches longer than this rather than exploding


class Spring:
    """Damped harmonic oscillator integrated with semi-implicit Euler.

    damping 1.0 is critical (no overshoot), below 1.0 overshoots and settles,
    which is what sells weight. k is stiffness: higher reaches target sooner.
    """

    __slots__ = ("x", "v", "target", "k", "c", "eps")

    def __init__(self, value=0.0, k=120.0, damping=1.0, eps=1e-4):
        self.x = float(value)
        self.v = 0.0
        self.target = float(value)
        self.k = float(k)
        self.c = 2.0 * math.sqrt(self.k) * float(damping)
        self.eps = eps

    def tune(self, k=None, damping=None):
        if k is not None:
            self.k = float(k)
        if damping is not None:
            self.c = 2.0 * math.sqrt(self.k) * float(damping)
        return self

    def step(self, dt):
        remaining = min(dt, _MAX_FRAME)
        while remaining > 1e-9:
            h = _MAX_STEP if remaining > _MAX_STEP else remaining
            a = -self.k * (self.x - self.target) - self.c * self.v
            self.v += a * h
            self.x += self.v * h
            remaining -= h
        return self.x

    def nudge(self, impulse):
        """Add velocity directly. Use for hops, recoils and anticipation --
        an impulse reads as physical in a way a target change does not."""
        self.v += impulse

    def snap(self, value):
        self.x = self.target = float(value)
        self.v = 0.0

    @property
    def settled(self):
        return abs(self.x - self.target) < self.eps and abs(self.v) < self.eps * 10.0


class Follow:
    """A value that chases a source with lag. Chain these for follow-through:
    body -> limb -> limb tip, each a little softer than the last."""

    __slots__ = ("spring",)

    def __init__(self, value=0.0, k=60.0, damping=0.55):
        self.spring = Spring(value, k=k, damping=damping)

    def step(self, source, dt):
        self.spring.target = source
        return self.spring.step(dt)

    @property
    def x(self):
        return self.spring.x

    @property
    def settled(self):
        return self.spring.settled


class Noise1D:
    """Smooth value noise. Cheap, deterministic per seed, and unlike a sine it
    never lands on a perceptible period -- which is the whole point for the
    micro-movement that keeps a face from reading as a mannequin."""

    __slots__ = ("_rand", "_t", "_a", "_b", "rate")

    def __init__(self, rate=1.0, seed=None):
        self._rand = random.Random(seed)
        self.rate = rate
        self._t = 0.0
        self._a = self._rand.uniform(-1.0, 1.0)
        self._b = self._rand.uniform(-1.0, 1.0)

    def step(self, dt):
        self._t += dt * self.rate
        while self._t >= 1.0:
            self._t -= 1.0
            self._a = self._b
            self._b = self._rand.uniform(-1.0, 1.0)
        t = self._t
        smooth = t * t * (3.0 - 2.0 * t)
        return self._a + (self._b - self._a) * smooth


class Breath:
    """Slow body oscillation. rate is cycles per second: ~0.25 at rest
    (15 breaths/min), rising when Clawd is working hard."""

    __slots__ = ("phase", "rate")

    def __init__(self, rate=0.25):
        self.phase = random.random()
        self.rate = rate

    def step(self, dt):
        self.phase = (self.phase + dt * self.rate) % 1.0
        return math.sin(self.phase * math.tau)


class Blink:
    """Lid closure 0..1 with human timing.

    Inter-blink intervals are roughly exponential around a 4s mean. The closing
    phase is faster than the opening phase -- get that backwards and it reads
    as sleepy rather than alert.
    """

    CLOSE = 0.065
    HOLD = 0.030
    OPEN = 0.115

    def __init__(self, mean_interval=4.0, seed=None):
        self._rand = random.Random(seed)
        self.mean_interval = mean_interval
        self._wait = self._next_interval()
        self._t = None
        self._queued = 0

    def _next_interval(self):
        return min(9.0, max(1.2, self._rand.expovariate(1.0 / self.mean_interval)))

    def force(self, double=False):
        self._t = 0.0
        self._queued = 1 if double else 0

    def step(self, dt):
        if self._t is None:
            self._wait -= dt
            if self._wait <= 0.0:
                self._t = 0.0
                # people often blink twice in quick succession
                self._queued = 1 if self._rand.random() < 0.15 else 0
            return 0.0

        self._t += dt
        total = self.CLOSE + self.HOLD + self.OPEN
        if self._t >= total:
            if self._queued:
                self._queued -= 1
                self._t = 0.0
                return 0.0
            self._t = None
            self._wait = self._next_interval()
            return 0.0

        if self._t < self.CLOSE:
            u = self._t / self.CLOSE
            return u * u  # accelerating close
        if self._t < self.CLOSE + self.HOLD:
            return 1.0
        u = (self._t - self.CLOSE - self.HOLD) / self.OPEN
        return 1.0 - (1.0 - (1.0 - u) * (1.0 - u))  # decelerating open


class Saccade:
    """Gaze target generator.

    Real eyes do not drift, they jump: a fast ballistic move, then a long
    fixation. Amplitude and duration are linked (the 'main sequence'), so a
    small glance is quicker than a wide one.
    """

    def __init__(self, box=(1.0, 0.6), seed=None):
        self._rand = random.Random(seed)
        self.box = box
        self.x = 0.0
        self.y = 0.0
        self._fixate = self._next_fixation()
        self.enabled = True

    def _next_fixation(self):
        return self._rand.uniform(0.25, 1.1)

    def look_at(self, x, y, hold=None):
        self.x, self.y = x, y
        self._fixate = hold if hold is not None else self._next_fixation()

    def step(self, dt, spread=1.0):
        if not self.enabled:
            return self.x, self.y
        self._fixate -= dt
        if self._fixate <= 0.0:
            bx, by = self.box
            self.x = self._rand.uniform(-bx, bx) * spread
            self.y = self._rand.uniform(-by, by) * spread
            self._fixate = self._next_fixation()
        return self.x, self.y


def squash(velocity, gain=0.0016, limit=0.16):
    """Volume-preserving squash and stretch from vertical velocity.

    Returns (scale_x, scale_y). Moving fast stretches along the direction of
    travel and pinches across it, so the silhouette conserves area.
    """
    s = max(-limit, min(limit, velocity * gain))
    scale_y = 1.0 + s
    scale_x = 1.0 / scale_y
    return scale_x, scale_y


def approach(current, target, rate, dt):
    """Frame-rate independent exponential approach, for values that should
    ease but never overshoot (opacity, colour, caption fade)."""
    return target + (current - target) * math.exp(-rate * dt)


class Oscillator:
    """Sustained periodic motion for a state's motion signature.

    Emotion in a face this low-bandwidth is carried more by *rhythm* than by
    amplitude: a slow nod and a fast bounce read as different feelings even
    when both move the same few pixels. Frequencies across the pose table are
    deliberately spread so no two states beat at the same rate.
    """

    __slots__ = ("param", "amplitude", "rate", "wave", "phase")

    def __init__(self, param, amplitude, rate, wave="sine", phase=0.0):
        self.param = param
        self.amplitude = amplitude
        self.rate = rate
        self.wave = wave
        self.phase = phase

    def value(self, t):
        u = (t * self.rate + self.phase) % 1.0
        if self.wave == "bounce":
            # always-negative: a bounce leaves the ground, it does not sink
            return -abs(math.sin(u * math.pi)) * self.amplitude
        if self.wave == "pulse":
            return (1.0 if u < 0.5 else 0.0) * self.amplitude
        if self.wave == "sway":
            # slow figure-of-eight; reads as unsteady rather than rhythmic
            return (math.sin(u * math.tau) * 0.7
                    + math.sin(u * math.tau * 2.0) * 0.3) * self.amplitude
        return math.sin(u * math.tau) * self.amplitude
