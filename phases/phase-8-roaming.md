# Phase 8 — Roaming the screen border

**Status:** steps 1–5 built. Optional step 6 pending.
**Depends on:** the existing overlay, vector rig and session events.
**Delivers:** idle exploration of the primary monitor's usable border.

## What is built

1. A transparent overlay covering the monitor's usable area, with click-through
   outside the buddy, readout and other avatars. Roam Off restores a bottom strip.
   The overlay hides while the active X11 application is full-screen; held approval
   requests return to the terminal. Detection runs in a worker and needs xprop.
2. Along the bottom: long idle strolls wrap continuously between screen edges,
   with both copies drawn and clickable while crossing.
3. Whole border: distance around a rounded border determines the buddy's centre
   and angle. He walks up walls, across the ceiling and around corners in either
   direction. Corners account for the rotated body's bounds. Walking animation
   uses speed along the border even when screen x stays constant.
4. Dragging in both axes in Whole border mode, with projection onto the nearest
   border point on release. The edge and relative position are saved in pos.json
   and restored after restart, size changes and monitor refits. Old floor-only
   position files still work. Off and Along the bottom keep horizontal dragging.
   Readouts open inward at each edge, with upright text and directional bubble
   tails. Drawing and card clicks share the same placement.
5. Occasional idle tricks: peeking half out of a floor edge, looking inward and
   walking back; sliding down either wall and rolling through the bottom corner;
   and a ceiling drop with anticipation, landing squash and dust. The menu offers
   the appropriate trick on the current edge. Ceiling naps hang by the feet,
   sway gently and send upright zzz downward, inside the monitor.

Work, completion, errors and attention requests bring him back to the floor.
He falls and turns upright; working costumes appear after landing. Short idle
walks and longer strolls can resume from a paused wall or ceiling position.
Go for a stroll requests a full lap in Whole border mode. Normal activity can
interrupt it. Other session avatars and sidekicks stay on the floor near home.

Only deliberate dragging saves a position; automatic strolls and work interruptions
do not replace that saved anchor. Work events still update while he is held and
return him to the floor after release. Dragging a sleeping buddy wakes him on the
selected edge. Tiny pointer movements remain clicks. Hiding or refitting the window
cancels a held drag so he cannot be left stuck.

## Remaining build order

6. Optional: avatars and sidekicks follow along the border; multiple monitors.

Tricks use the existing idle state, so work, attention, sleep, clicking and dragging
can interrupt them. They do not save positions or change session data. Peeking never
creates a wrapped copy on the other edge, and an interrupted peek walks fully back
on screen. A new stroll cannot interrupt a fall. Window refits cancel active tricks.

## Validation

`python3 test_roam.py` covers floor wraps, both lap directions, corner continuity,
body containment at every supported size, falls from across the border, work
interruptions and wall stride. With a display, it also draws the actual overlay,
checks readouts and card targets at every edge, drives two-axis drags and restart
restoration, tests size changes and work during a drag, switches roam modes, and
verifies asynchronous full-screen hiding/restoration and held-prompt fallback.
It also checks complete tricks, mid-trick work/sleep/click interruptions, both
wall slides at multiple sizes, once-per-landing dust, bat-nap wakeup and zzz drift.

The existing session, transcript, alert, window, interaction, mod and costume
suites remain part of regression validation. Full-screen detection of native
Wayland clients depends on what their compositor exposes to XWayland.
