# Phase 7 — Ambient signals and personality

**Status:** not started
**Depends on:** nothing structural. Each item here is independent and can be built any time
a small, satisfying job is wanted.
**Delivers:** context that is not about Claude, and charm that is not about information.

---

## Two unrelated halves

**Ambient** is information from outside Claude Code — git, CI, budget. It shares the panel
and the meter widget but nothing else.

**Personality** is deliberately not information. It is the reason a pet works where a status
bar does not. It is also the easiest thing to overdo.

---

## Ambient

### Git state of the watched repo

Branch, dirty file count, unpushed commit count, shown as a small line in the panel.

- Source: `git status --porcelain=v2 --branch` and `git rev-list --count @{u}..HEAD` in the
  session's `cwd`.
- Poll on a slow timer (15-30s) **and** after any `PostToolUse` for a writing tool, which is
  when it is most likely to have changed.
- A repo with no upstream, a detached HEAD, or no git at all must all render as "—", not as
  an error.

### CI / PR status dots

- Source: `gh pr status --json …` and `gh run list --json …`, if `gh` is installed and
  authenticated. Otherwise the row does not appear at all.
- Slow poll only (minutes). This is the one place that touches the network — it must be
  cancellable, must never block the daemon, and must back off on failure rather than
  retrying in a tight loop.

### Cost budget meter

- Reuses phase 2's numbers and phase 2's bar widget.
- A daily or per-session threshold, with a warning colour and one toast when crossed.
- One toast, not a per-message nag.

---

## Personality

### Mood tied to metrics

Tests green → happy idle. Tests red → glum idle. Long session → tired.

The rule must be legible: if you cannot explain in one sentence why he looks like that, it
reads as a bug rather than as charm. Keep it to three moods, driven by facts already on
screen.

### Skins and themes

The palette is already one dict, and the shading is derived from it, so a skin is a palette
plus optional prop overrides. This is the cheapest personality feature by a distance and the
one most likely to be wanted — a dark-mode-friendly variant, a monochrome variant.

### Caveman-mode buddy

If the caveman skill is active, the speech bubble from phase 1 speaks caveman: same content,
compressed. Rides entirely on the existing bubble; it is a text transform, not a feature.

### Levels / XP by sessions completed

A gimmick, and it knows it. Cheap, sticky, and harmless as long as it never occupies screen
space by default — a line in the panel, not a bar on the desktop.

---

## Tasks

Each is independent; pick any.

1. Git line: parse, slow poll, refresh on writing tools, degrade to "—".
2. CI/PR dots behind a `gh` presence check, with backoff.
3. Budget threshold and one-shot warning toast.
4. Three-mood rule, driven by existing facts.
5. Skin support: palette swap plus prop overrides; ship two skins.
6. Caveman text transform for the bubble.
7. XP counter in the panel.

---

## Acceptance criteria

- No git repo, detached HEAD and missing upstream all render without an error.
- `gh` absent or unauthenticated hides the CI row entirely rather than showing a broken one.
- Network failure in the CI poll backs off and never blocks a repaint.
- Switching skin changes every sprite consistently, including derived shading, with one
  rebuild.
- Mood changes are explainable in one sentence.
- Nothing in this phase increases idle CPU measurably.

---

## Risks

- **Network in the daemon.** The CI poll is the first outbound request in the project. Keep
  it isolated, cancellable, and off by default until it has been lived with.
- **Personality outstaying its welcome.** Every element here is optional and every one
  should be switchable off. The pet is charming because he is quiet.
