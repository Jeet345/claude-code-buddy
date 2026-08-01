#!/usr/bin/env python3
"""What is actually in a Claude Code transcript, on this machine, today.

The transcript format is undocumented and will change when it changes. Rather
than write `transcript.py` against a guess, run this over the real files and
write the parser against what it prints. Re-run it whenever the numbers in the
HUD start looking wrong - a format change shows up here first.

    python3 dump_transcript.py                    # every project, summary
    python3 dump_transcript.py path/to/x.jsonl    # one file
    python3 dump_transcript.py --sample assistant # print one whole line of a type

Findings get copied into `phases/phase-2-metrics.md`. Read-only, always.
"""

import collections
import json
import sys
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
USAGE_KEYS = ("input_tokens", "output_tokens",
              "cache_read_input_tokens", "cache_creation_input_tokens")


def lines(path):
    """Every parseable object, plus a count of the ones that were not."""
    bad = 0
    try:
        with open(path, errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        pass
    if bad:
        print(f"  ({bad} unparseable lines in {path.name})")


def key_paths(obj, prefix="", out=None, depth=0):
    """Flattened `a.b.c:type` paths, so a new field is obvious at a glance."""
    out = set() if out is None else out
    if depth > 4 or not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            key_paths(v, p, out, depth + 1)
        elif isinstance(v, list):
            out.add(f"{p}[]:{type(v[0]).__name__ if v else 'empty'}")
        else:
            out.add(f"{p}:{type(v).__name__}")
    return out


def survey(paths):
    types = collections.Counter()
    subtypes = collections.Counter()
    models = collections.Counter()
    usage_shapes = collections.Counter()
    assistant_paths = collections.Counter()
    ctx_max = collections.defaultdict(int)
    compactions = []
    sidechain = collections.Counter()
    total = 0

    for path in paths:
        for obj in lines(path):
            total += 1
            kind = obj.get("type")
            types[kind] += 1
            if kind == "system":
                subtypes[obj.get("subtype")] += 1
                meta = obj.get("compactMetadata")
                if isinstance(meta, dict):
                    compactions.append((meta.get("trigger"),
                                        meta.get("preTokens"),
                                        meta.get("postTokens")))
            if kind != "assistant":
                continue
            sidechain[bool(obj.get("isSidechain"))] += 1
            for p in key_paths(obj):
                assistant_paths[p.split(":")[0]] += 1
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            model = msg.get("model")
            models[model] += 1
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
            usage_shapes[tuple(sorted(usage))] += 1
            ctx = sum(usage.get(k) or 0 for k in USAGE_KEYS if k != "output_tokens")
            ctx_max[model] = max(ctx_max[model], ctx)

    print(f"\n{len(paths)} transcripts, {total} lines\n")
    show("line types", types)
    show("system subtypes", subtypes)
    show("models", models)
    show("assistant isSidechain", sidechain)
    show("assistant key paths", assistant_paths)

    print("\nmessage.usage key sets")
    for keys, n in usage_shapes.most_common():
        print(f"  {n:6d}  {', '.join(keys) or '(empty)'}")

    print("\nhighest observed context (input + cache_read + cache_creation)")
    for model, n in sorted(ctx_max.items(), key=lambda kv: -kv[1]):
        print(f"  {n:9,}  {model}")
    print("  ^ a floor for the real window, never the window itself")

    if compactions:
        print("\ncompact_boundary (ground truth for the context reading)")
        for trigger, pre, post in compactions:
            print(f"  {trigger:>10}  pre {pre:,} -> post {post:,}")


def show(title, counter):
    print(title)
    for key, n in counter.most_common():
        print(f"  {n:6d}  {key}")
    print()


def sample(paths, kind):
    for path in paths:
        for obj in lines(path):
            if obj.get("type") == kind:
                print(json.dumps(obj, indent=2)[:4000])
                return
    print(f"no {kind!r} line found")


def main(argv):
    argv = list(argv)
    kind = None
    if "--sample" in argv:
        i = argv.index("--sample")
        kind = argv[i + 1] if i + 1 < len(argv) else "assistant"
        del argv[i:i + 2]

    paths = [Path(a) for a in argv] or sorted(PROJECTS.glob("*/*.jsonl"))
    paths = [p for p in paths if p.is_file()]
    if not paths:
        print(f"no transcripts under {PROJECTS}")
        return 1
    sample(paths, kind) if kind else survey(paths)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
