"""Measure prompt-cache waste from paced waits, and model a cache-aware release.

Read-only over four sources; writes nothing anywhere:

- Claude Code session logs   ~/.claude/projects/<slug>/*.jsonl
- Codex session logs         ~/.codex/sessions/**/*.jsonl
- the pacer's own JSONL log  ~/.local/state/kandev-pacer/pacer.jsonl
- kandev's SQLite database   ~/.kandev/data/kandev.db  (opened with mode=ro)

Part A measures what happened: every inter-request gap longer than the cache TTL, what
the request after it wrote back to the cache, and which of those gaps the pacer caused.
Part B replays the pacer-caused ones under a cache-aware release rule and reports what
each subscription window would have kept. `docs/cache-aware-pacing.md` explains the
method and carries the numbers this printed on 2026-09-06.

    uv run python scripts/cache_waste.py            # everything
    uv run python scripts/cache_waste.py --quick    # skip the token-per-percent fit
"""

from __future__ import annotations

import glob
import json
import sqlite3
import statistics
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from kandev_pacer.quota import resume_at

HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
PACER_LOG = HOME / ".local" / "state" / "kandev-pacer" / "pacer.jsonl"
KANDEV_DB = HOME / ".kandev" / "data" / "kandev.db"
TASKS_ROOT = "/.kandev/tasks/"

# --- the cache rules (sources in docs/cache-aware-pacing.md) ---
TTL_1H = 3600.0
TTL_5M = 300.0
TTL_CODEX = 300.0          # "around 5 to 10 minutes of inactivity"; 300 is the safe bound
WRITE_1H = 2.0             # 1-hour cache write, x base input
WRITE_5M = 1.25            # 5-minute cache write
READ = 0.1                 # cache read, most models
READ_FABLE = 0.025         # cache read, Fable 5.1 / Mythos 5.1
OUTPUT_WEIGHT = 5.0        # output price / input price, every current Claude tier
CODEX_READ = 0.1           # cached input on GPT-5.6+, "0.1x that rate"
CODEX_OUTPUT_WEIGHT = 8.0  # gpt-5.x output/input price ratio

GAP_BUCKETS = [(300, "5m-1h"), (3600, "1-2h"), (7200, "2-4h"), (14400, ">4h")]
MARGINS = [300, 600, 1200]          # PACER_CACHE_MARGIN candidates, seconds
READ_WEIGHTS = [READ, 0.5, 1.0]     # how a cache read might weigh inside a subscription


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def bucket(gap):
    for limit, name in reversed(GAP_BUCKETS):
        if gap >= limit:
            return name
    return "<5m"


def slug(path):
    """Claude Code's project directory name for a working directory."""
    return path.replace("/", "-").replace(".", "-").replace("_", "-")


def is_fable(model):
    return "fable" in (model or "").lower() or "mythos" in (model or "").lower()


# --- Claude Code logs ---


def claude_requests(path):
    """Distinct API requests of one session file: (t, usage, model), deduplicated.

    Claude Code writes one `assistant` record per streamed content block, all carrying
    the same `message.id`, `requestId` and `usage`; without this dedup the cache-write
    totals roughly double.
    """
    seen = set()
    out = []
    with open(path) as fh:
        for line in fh:
            if '"assistant"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "assistant" or not rec.get("timestamp"):
                continue
            msg = rec.get("message") or {}
            key = (msg.get("id"), rec.get("requestId"))
            if key in seen:
                continue
            seen.add(key)
            usage = msg.get("usage") or {}
            if not usage:
                continue
            out.append((ts(rec["timestamp"]), usage, msg.get("model")))
    out.sort(key=lambda r: r[0])
    return out


def claude_ttl(usage):
    cc = usage.get("cache_creation") or {}
    return TTL_5M if cc.get("ephemeral_5m_input_tokens") and not cc.get(
        "ephemeral_1h_input_tokens"
    ) else TTL_1H


def claude_weighted(usage, model, read_weight=None):
    """Input-token equivalents a request spent, at API price ratios."""
    read = read_weight if read_weight is not None else (
        READ_FABLE if is_fable(model) else READ
    )
    cc = usage.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens", 0)
    w5m = cc.get("ephemeral_5m_input_tokens", 0)
    if not (w1h or w5m):
        w1h = usage.get("cache_creation_input_tokens", 0)
    return (
        usage.get("input_tokens", 0)
        + w1h * WRITE_1H
        + w5m * WRITE_5M
        + usage.get("cache_read_input_tokens", 0) * read
        + usage.get("output_tokens", 0) * OUTPUT_WEIGHT
    )


def claude_miss_extra(usage, model, read_weight=None):
    """What a request after an expired prefix paid beyond a warm hit, in input-token
    equivalents: the whole rewrite at the write rate, minus the read it would have been.
    """
    read = read_weight if read_weight is not None else (
        READ_FABLE if is_fable(model) else READ
    )
    cc = usage.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens", 0)
    w5m = cc.get("ephemeral_5m_input_tokens", 0)
    if not (w1h or w5m):
        w1h = usage.get("cache_creation_input_tokens", 0)
    return w1h * (WRITE_1H - read) + w5m * (WRITE_5M - read)


def scan_claude(dirs):
    """Per project dir: list of sessions, each a list of requests; plus gap stats."""
    sessions = defaultdict(list)
    stats = {
        "sessions": 0, "requests": 0, "gaps": Counter(), "stale": 0,
        "stale_tokens": 0, "stale_extra": 0.0, "model_switch": 0,
    }
    for d in dirs:
        for f in glob.glob(str(d / "*.jsonl")):
            reqs = claude_requests(f)
            if not reqs:
                continue
            stats["sessions"] += 1
            stats["requests"] += len(reqs)
            sessions[d.name].append(reqs)
            for (t0, u0, m0), (t1, u1, m1) in zip(reqs, reqs[1:], strict=False):
                gap = t1 - t0
                stats["gaps"][bucket(gap)] += 1
                if gap <= claude_ttl(u0):
                    continue
                if m0 != m1:
                    stats["model_switch"] += 1
                    continue
                stats["stale"] += 1
                stats["stale_tokens"] += u1.get("cache_creation_input_tokens", 0)
                stats["stale_extra"] += claude_miss_extra(u1, m1)
    return sessions, stats


# --- Codex logs ---


def codex_session(path):
    """(cwd, [(t, last_usage)]) for one rollout file."""
    cwd = None
    out = []
    with open(path) as fh:
        for line in fh:
            if "token_count" not in line and "session_meta" not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = rec.get("payload") or {}
            if rec.get("type") == "session_meta":
                cwd = payload.get("cwd")
            elif rec.get("type") == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                last = info.get("last_token_usage") or {}
                if last and rec.get("timestamp"):
                    out.append((ts(rec["timestamp"]), last))
    return cwd, out


def codex_weighted(u, read_weight=CODEX_READ):
    cached = u.get("cached_input_tokens", 0)
    return (
        (u.get("input_tokens", 0) - cached)
        + cached * read_weight
        + u.get("output_tokens", 0) * CODEX_OUTPUT_WEIGHT
    )


def scan_codex():
    sessions = defaultdict(list)
    stats = {
        "sessions": 0, "requests": 0, "gaps": Counter(), "stale": 0,
        "stale_uncached": 0, "stale_extra": 0.0,
    }
    for f in glob.glob(str(CODEX_SESSIONS / "**" / "*.jsonl"), recursive=True):
        cwd, reqs = codex_session(f)
        if not cwd or TASKS_ROOT not in cwd or len(reqs) < 2:
            continue
        stats["sessions"] += 1
        stats["requests"] += len(reqs)
        sessions[cwd].append(reqs)
        warm_uncached = [
            u1.get("input_tokens", 0) - u1.get("cached_input_tokens", 0)
            for (t0, _), (t1, u1) in zip(reqs, reqs[1:], strict=False)
            if t1 - t0 <= TTL_CODEX
        ]
        baseline = statistics.median(warm_uncached) if warm_uncached else 0
        for (t0, _), (t1, u1) in zip(reqs, reqs[1:], strict=False):
            gap = t1 - t0
            stats["gaps"][bucket(gap)] += 1
            if gap <= TTL_CODEX:
                continue
            uncached = u1.get("input_tokens", 0) - u1.get("cached_input_tokens", 0)
            excess = max(0, uncached - baseline)
            stats["stale"] += 1
            stats["stale_uncached"] += uncached
            stats["stale_extra"] += excess * (1 - CODEX_READ)
    return sessions, stats


# --- the pacer log and kandev ---


def pacer_launches():
    """Every launch the pacer made, with the tick's windows, plus the withheld ticks."""
    launches = []
    withheld = defaultdict(list)   # provider -> [(t, windows)]
    ticks = defaultdict(list)      # provider -> [(t, windows)] sampled every ~60 s
    last_sample = {}
    with open(PACER_LOG) as fh:
        for line in fh:
            if '"kind":"tick"' not in line:
                continue
            rec = json.loads(line)
            provider = rec.get("provider") or "anthropic"
            windows = [
                {
                    "label": w["label"], "consumed_frac": w["c"],
                    "duration_s": w["d"], "resets_at": w["resets_at"],
                }
                for w in rec.get("windows") or []
            ]
            t = rec["t"]
            if rec.get("action") == "withheld":
                withheld[provider].append((t, windows))
            if t - last_sample.get(provider, 0) >= 60:
                ticks[provider].append((t, windows))
                last_sample[provider] = t
            launched = rec.get("launched") or {}
            if launched.get("task") and launched.get("session"):
                launches.append((t, provider, launched["task"], launched["session"], windows))
    return launches, withheld, ticks


def kandev_sessions():
    db = sqlite3.connect(f"file:{KANDEV_DB}?mode=ro", uri=True)
    turns = defaultdict(list)
    for sid, started, completed in db.execute(
        "select task_session_id, started_at, completed_at from task_session_turns "
        "where completed_at is not null"
    ):
        turns[sid].append((ts(started) if started else None, ts(completed)))
    paths = dict(db.execute("select id, workspace_path from task_sessions"))
    models = {}
    for sid, snap in db.execute("select id, agent_profile_snapshot from task_sessions"):
        try:
            models[sid] = (json.loads(snap or "{}") or {}).get("model", "")
        except json.JSONDecodeError:
            models[sid] = ""
    return turns, paths, models


def first_after(reqs_by_dir, key, t, horizon=1800):
    """The first request any session under `key` made within `horizon` s after `t`."""
    best = None
    for reqs in reqs_by_dir.get(key, ()):
        for r in reqs:
            if t <= r[0] <= t + horizon and (best is None or r[0] < best[0]):
                best = r
                break
    return best


def nearest_tick(ticks, t):
    lo, hi = 0, len(ticks)
    while lo < hi:
        mid = (lo + hi) // 2
        if ticks[mid][0] < t:
            lo = mid + 1
        else:
            hi = mid
    return ticks[lo] if lo < len(ticks) else None


def attribute(launches, withheld, ticks, turns, paths, models, claude, codex,
              *, measurements_only=False):
    """Every pacer launch that continued an existing session, with its wait, whether the
    pacer withheld during it, the prefix the next request rewrote, and how far past
    the cache deadline the gate would have reopened on its own."""
    rows = []
    for t, provider, task, sid, _windows in launches:
        prior = [c for _s, c in turns.get(sid, ()) if c < t]
        if not prior:
            continue  # fresh session: no conversation prefix to lose
        prev_end = max(prior)
        wait = t - prev_end
        path = paths.get(sid) or ""
        model = models.get(sid, "")
        held = [w for w in withheld.get(provider, ()) if prev_end <= w[0] <= t]
        if provider == "openai":
            nxt = first_after(codex, path, t)
            ttl = TTL_CODEX
            extra = None
            if nxt and not measurements_only:
                u = nxt[1]
                extra = (u.get("input_tokens", 0) - u.get("cached_input_tokens", 0)) \
                    * (1 - CODEX_READ)
            prefix = (nxt[1].get("input_tokens", 0)) if nxt else None
        else:
            nxt = first_after(claude, slug(path), t)
            ttl = claude_ttl(nxt[1]) if nxt else TTL_1H
            extra = (claude_miss_extra(nxt[1], nxt[2])
                     if nxt and not measurements_only else None)
            prefix = (
                nxt[1].get("cache_creation_input_tokens", 0)
                + nxt[1].get("cache_read_input_tokens", 0)
            ) if nxt else None
        deadline = prev_end + ttl
        overshoot = None
        if held and wait > ttl and not measurements_only:
            at = nearest_tick(ticks.get(provider, []), deadline)
            if at and at[1]:
                healed = resume_at(at[1], deadline)
                overshoot = max(0.0, (healed or deadline) - deadline)
        rows.append({
            "t": t, "provider": provider, "task": task, "session": sid, "wait": wait,
            "held": bool(held), "ttl": ttl, "matched": nxt is not None,
            "extra": extra, "prefix": prefix, "overshoot": overshoot,
            "fable": is_fable(model), "stale": wait > ttl,
        })
    return rows


# --- token <-> percent ---


def tokens_per_percent(ticks, claude_all, codex_all):
    """Weighted input-token equivalents spent per percent of each window, fitted over
    the whole log: every 1 % the window's `c` rose, against everything every session on
    this machine spent in the same interval. A range, because sessions elsewhere (claude.ai,
    other devices) are invisible here and would push the true figure down."""
    out = {}
    for provider, series in ticks.items():
        spend = claude_all if provider != "openai" else codex_all
        times = [t for t, _ in spend]
        cum = [0.0]
        for _, v in spend:
            cum.append(cum[-1] + v)

        def spent_between(t0, t1, times=times, cum=cum):
            return cum[bisect_right(times, t1)] - cum[bisect_right(times, t0)]

        # The provider read is cached for five minutes, so `c` moves in steps: count
        # everything spent since the previous step, not just since the previous tick.
        by_label = defaultdict(lambda: [0.0, 0.0])
        last = {}
        for t, windows in series:
            for w in windows:
                label = w["label"]
                prev = last.get(label)
                if prev is None:
                    last[label] = (t, w["consumed_frac"])
                    continue
                t_prev, c_prev = prev
                dc = w["consumed_frac"] - c_prev
                if dc == 0:
                    continue
                if dc > 0:
                    by_label[label][0] += dc * 100
                    by_label[label][1] += spent_between(t_prev, t)
                last[label] = (t, w["consumed_frac"])
        out[provider] = {
            label: (tok / pct if pct else None) for label, (pct, tok) in by_label.items()
        }
    return out


def all_spend_claude():
    """(t, weighted tokens) for every Claude Code request on this machine."""
    out = []
    for f in glob.glob(str(CLAUDE_PROJECTS / "*" / "*.jsonl")):
        for t, u, m in claude_requests(f):
            out.append((t, claude_weighted(u, m)))
    out.sort()
    return out


def all_spend_codex():
    out = []
    for f in glob.glob(str(CODEX_SESSIONS / "**" / "*.jsonl"), recursive=True):
        _cwd, reqs = codex_session(f)
        out.extend((t, codex_weighted(u)) for t, u in reqs)
    out.sort()
    return out


# --- reporting ---


def fmt(n):
    if n is None:
        return "-"
    if isinstance(n, float) and abs(n) < 100:
        return f"{n:.2f}"
    return f"{int(round(n)):,}"


def table(headers, rows):
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    line = "| " + " | ".join(str(h).ljust(w) for h, w in zip(headers, widths, strict=False)) + " |"
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    body = [
        "| " + " | ".join(str(c).ljust(w) for c, w in zip(r, widths, strict=False)) + " |"
        for r in rows
    ]
    return "\n".join([line, sep, *body])


def main(argv):
    quick = "--quick" in argv
    task_dirs = [
        d for d in CLAUDE_PROJECTS.iterdir()
        if d.is_dir() and "-kandev-tasks-" in d.name
    ]
    claude, cstats = scan_claude(task_dirs)
    codex, xstats = scan_codex()
    launches, withheld, ticks = pacer_launches()
    turns, paths, models = kandev_sessions()
    rows = attribute(launches, withheld, ticks, turns, paths, models, claude, codex)

    print("## A1. Inter-request gaps in kandev task sessions\n")
    print(table(
        ["source", "sessions", "requests", *[b for _, b in GAP_BUCKETS],
         "stale (>TTL)", "tokens after stale", "extra paid (input-eq)"],
        [
            ["Claude Code", cstats["sessions"], cstats["requests"],
             *[cstats["gaps"][b] for _, b in GAP_BUCKETS], cstats["stale"],
             fmt(cstats["stale_tokens"]), fmt(cstats["stale_extra"])],
            ["Codex", xstats["sessions"], xstats["requests"],
             *[xstats["gaps"][b] for _, b in GAP_BUCKETS], xstats["stale"],
             fmt(xstats["stale_uncached"]), fmt(xstats["stale_extra"])],
        ],
    ))
    print(f"\nClaude gaps excluded for a model switch across the gap: "
          f"{cstats['model_switch']}\n")

    print("## A2. Pacer launches that continued an existing session\n")
    by = defaultdict(list)
    for r in rows:
        by[r["provider"]].append(r)
    out = []
    for provider, rs in sorted(by.items()):
        stale = [r for r in rs if r["stale"]]
        held = [r for r in stale if r["held"]]
        matched = [r for r in held if r["matched"] and r["extra"] is not None]
        waits = [r["wait"] for r in rs]
        out.append([
            provider, len(rs), fmt(statistics.median(waits) / 60) if waits else "-",
            len(stale), len(held), len(matched),
            fmt(sum(r["extra"] for r in matched)),
            fmt(statistics.median([r["prefix"] for r in matched if r["prefix"]]))
            if matched else "-",
        ])
    print(table(
        ["provider", "launches", "median wait (min)", "stale", "stale & pacer withheld",
         "…with a matched next request", "extra paid (input-eq)", "median prefix"],
        out,
    ))
    print()

    print("## A3. How far past the cache deadline the gate reopened (pacer-caused stale)\n")
    out = []
    for provider, rs in sorted(by.items()):
        ov = [r["overshoot"] for r in rs if r["held"] and r["stale"]
              and r["overshoot"] is not None]
        if not ov:
            continue
        out.append([
            provider, len(ov),
            *[sum(1 for o in ov if o <= m) for m in MARGINS],
            fmt(statistics.median(ov) / 60), fmt(max(ov) / 3600),
        ])
    print(table(
        ["provider", "n", *[f"<= {m // 60} min" for m in MARGINS],
         "median (min)", "max (h)"],
        out,
    ))
    print()

    tpp = None
    span = [t for series in ticks.values() for t, _ in series]
    weeks = (max(span) - min(span)) / 604800 if span else 0
    if not quick:
        print("## A4. Weighted tokens per percent of window (fit over the whole log)\n")
        tpp = tokens_per_percent(ticks, all_spend_claude(), all_spend_codex())
        out = [
            [p, label, fmt(v)] for p, labels in sorted(tpp.items())
            for label, v in sorted(labels.items())
        ]
        print(table(["provider", "window", "input-eq tokens per 1 %"], out))
        print()

    print("## B. Cache-aware release: what each margin would have kept\n")
    for provider, rs in sorted(by.items()):
        cand = [r for r in rs if r["held"] and r["stale"] and r["matched"]
                and r["extra"] is not None and r["overshoot"] is not None]
        if not cand:
            continue
        labels = sorted((tpp or {}).get(provider, {}))
        out = []
        for m in MARGINS:
            kept = [r for r in cand if r["overshoot"] <= m]
            saved = sum(r["extra"] for r in kept)
            fable_saved = sum(r["extra"] for r in kept if r["fable"])
            # overshoot 0: the gate was open at the deadline and the launch still came
            # later -- queue order or the cap, not the burn line. Those need the
            # ordering rule, not the early release.
            by_order = [r for r in kept if r["overshoot"] == 0]
            row = [
                f"{m // 60} min", len(kept), len(by_order), fmt(saved),
                fmt(sum(r["extra"] for r in by_order)), fmt(fable_saved),
            ]
            for label in labels:
                per = (tpp or {})[provider][label]
                pool = fable_saved if "Fable" in label else saved
                row.append(f"{pool / per / 100:.2f}" if per else "-")
            out.append(row)
        print(f"### {provider} (log spans {weeks:.1f} weeks)\n")
        print(table(
            ["margin", "kept warm", "…by order alone", "saved (input-eq)",
             "…by order alone", "…Fable",
             *[f"{label} windows saved" for label in labels]],
            out,
        ))
        print()

    print("## B2. Sensitivity: cache-read weight inside the subscription (anthropic)\n")
    out = []
    for provider, rs in sorted(by.items()):
        if provider == "openai":
            continue
        cand = [r for r in rs if r["held"] and r["stale"] and r["matched"]
                and r["overshoot"] is not None and r["overshoot"] <= MARGINS[1]]
        if not cand:
            continue
        for rw in READ_WEIGHTS:
            saved = 0.0
            for r in cand:
                nxt = first_after(claude, slug(paths.get(r["session"]) or ""), r["t"])
                if nxt:
                    saved += claude_miss_extra(nxt[1], nxt[2], read_weight=rw)
            out.append([provider, rw, fmt(saved)])
    print(table(["provider", "read weight", "saved at 10 min margin (input-eq)"], out))
    print()

    print("## B3. Extra requests the savings buy (10 min margin)\n")
    out = []
    for provider, rs in sorted(by.items()):
        per_request = []
        if provider == "openai":
            for sessions in codex.values():
                for reqs in sessions:
                    per_request.extend(codex_weighted(u) for _t, u in reqs)
        else:
            for sessions in claude.values():
                for reqs in sessions:
                    per_request.extend(claude_weighted(u, m) for _t, u, m in reqs)
        cand = [r for r in rs if r["held"] and r["stale"] and r["matched"]
                and r["extra"] is not None and r["overshoot"] is not None
                and r["overshoot"] <= MARGINS[1]]
        if not per_request or not cand:
            continue
        median_req = statistics.median(per_request)
        saved = sum(r["extra"] for r in cand)
        out.append([provider, fmt(median_req), fmt(saved / median_req)])
    print(table(["provider", "median input-eq per request", "extra requests bought"], out))
    print()
    print(f"sources: {len(task_dirs)} Claude project dirs, "
          f"{sum(len(v) for v in codex.values())} Codex task sessions, "
          f"{len(launches)} pacer launches, {len(rows)} continued a session")


if __name__ == "__main__":
    main(sys.argv[1:])
