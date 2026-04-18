#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-dotenv"]
# ///
"""
engram — self-learning loop for coding agents.
Mines session traces, detects personal patterns, auto-updates skills + memory.

Usage:
  uv run python -m engram.learn --hours 168 --save        # auto-saves to runs/<timestamp>_<h>h/
  uv run python -m engram.learn --hours 168 --show-candidates
Then in Claude Code: /engram-apply runs/latest/candidates.md
"""

import argparse
import json
import os
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

# Load .env from cwd, then project root
load_dotenv(pathlib.Path.cwd() / ".env")
load_dotenv(pathlib.Path(__file__).parent.parent / ".env", override=True)

import sys
import time

from engram import staso, compress, llm
from engram.prompts import PER_SESSION, CROSS_SESSION, COMMON_INSTRUCTIONS


_progress_starts: dict[str, float] = {}
_IS_TTY = sys.stdout.isatty()


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(raw: str) -> str:
    """Collapse unsafe chars (/, spaces, etc.) into underscores. Returns a single segment.

    Preserves meaningful tokens — slashes are flattened, not stripped.
    Blocks `..` traversal patterns.
    """
    if not raw:
        return "unknown.md"
    # Block directory traversal
    raw = raw.replace("..", "")
    # Flatten path separators into underscores (preserves info from "foo/bar" -> "foo_bar")
    raw = raw.replace("\\", "_").replace("/", "_")
    raw = _FILENAME_SAFE_RE.sub("_", raw.lower())
    # Collapse runs of underscores
    while "__" in raw:
        raw = raw.replace("__", "_")
    raw = raw.strip("._")
    if not raw:
        return "unknown.md"
    if not raw.endswith(".md"):
        raw += ".md"
    return raw


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def _progress(label: str, done: int, total: int, suffix: str = ""):
    """Inline progress bar (TTY only). Non-TTY prints a single line on completion.

    TTY format: ████████░░░░░░░░  42%  5/12  label  3s
    Non-TTY:    done: 12/12 label in 3s
    """
    if label not in _progress_starts:
        _progress_starts[label] = time.time()
    elapsed = time.time() - _progress_starts[label]

    if not _IS_TTY:
        # Pipe/log/cron: print only the completion line, once
        if done >= total:
            _progress_starts.pop(label, None)
            extra = f"  {suffix}" if suffix else ""
            print(f"  done: {done}/{total} {label} in {_fmt_elapsed(elapsed)}{extra}", flush=True)
        return

    w = 24
    pct = (done / total) if total else 1.0
    filled = int(round(w * pct))
    bar = "█" * filled + "░" * (w - filled)
    pct_int = int(round(pct * 100))
    time_str = _fmt_elapsed(elapsed)
    extra = f"  {suffix}" if suffix else ""

    count_width = len(str(total))
    counter = f"{done:>{count_width}}/{total}"

    line = f"  {bar}  {pct_int:>3d}%  {counter}  {label}  {time_str}{extra}"

    # \r + ANSI clear-to-end-of-line so the final frame cleanly overwrites prior renders
    clear = "\r\033[K"
    if done >= total:
        _progress_starts.pop(label, None)
        print(f"{clear}{line}", flush=True)
    else:
        print(f"{clear}{line}", end="", flush=True)

# ---------------------------------------------------------------------------
# Paths — auto-detect or override via env
# ---------------------------------------------------------------------------

def _find_skills_dir() -> pathlib.Path:
    env = os.environ.get("SKILLS_DIR")
    if env:
        return pathlib.Path(env).expanduser().resolve()
    # Default: .claude/skills relative to cwd
    return pathlib.Path.cwd() / ".claude" / "skills"


def _find_memory_dir() -> pathlib.Path:
    env = os.environ.get("MEMORY_DIR")
    if env:
        return pathlib.Path(env).expanduser().resolve()
    # Auto-detect: ~/.claude/projects/-<cwd-slug>/memory/
    cwd = pathlib.Path.cwd().resolve()
    slug = str(cwd).replace("/", "-")
    candidate = pathlib.Path.home() / ".claude" / "projects" / slug / "memory"
    if candidate.exists():
        return candidate
    # Fallback: check for any MEMORY.md nearby
    for p in (pathlib.Path.home() / ".claude" / "projects").iterdir() if (pathlib.Path.home() / ".claude" / "projects").exists() else []:
        mem = p / "memory" / "MEMORY.md"
        if mem.exists() and str(cwd.name) in p.name:
            return p / "memory"
    return candidate  # return the auto-detected path even if it doesn't exist yet


SKILLS_DIR = _find_skills_dir()
MEMORY_DIR = _find_memory_dir()
MEMORY_MD = MEMORY_DIR / "MEMORY.md"
RUNS_DIR = pathlib.Path(__file__).parent.parent / "runs"


def _make_run_dir(hours: int) -> pathlib.Path:
    """Create a timestamped run directory: runs/2026-04-17T04-26_168h/"""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M")
    name = f"{stamp}_{hours}h"
    run_dir = RUNS_DIR / name
    run_dir.mkdir(parents=True, exist_ok=True)
    latest = RUNS_DIR / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(name)
    except OSError:
        pass
    return run_dir


# Confidence cap for single-session behavioral candidates (used by per-session pass)
IMMEDIATE_CONFIDENCE_CAP = 0.65

# Session filters
MIN_SPANS = 10
MIN_DURATION_MS = 120_000

# ---------------------------------------------------------------------------
# Context loaders — feed existing local layout into the LLM prompt
# ---------------------------------------------------------------------------

def load_skills_index() -> str:
    if not SKILLS_DIR.exists():
        return "  (none found)"
    lines = []
    for skill_md in sorted(SKILLS_DIR.rglob("SKILL.md")):
        rel = skill_md.parent.relative_to(SKILLS_DIR)
        desc = ""
        try:
            for line in skill_md.read_text().splitlines():
                if line.startswith("description:"):
                    desc = line.replace("description:", "").strip().strip('"').strip("'")[:100]
                    break
        except Exception:
            pass
        lines.append(f"  {rel} -- {desc}" if desc else f"  {rel}")
    return "\n".join(lines) if lines else "  (none found)"


def load_memory_index() -> str:
    if not MEMORY_MD.exists():
        return "(none)"
    return MEMORY_MD.read_text().strip()

# ---------------------------------------------------------------------------
# Review file
# ---------------------------------------------------------------------------

def _write_review_md(path: pathlib.Path, skills: list, memories: list, insights: list,
                     session_summaries: list[tuple[int, dict]] | None = None):
    """Human-readable candidate review. Apply via Claude Code's /engram-apply skill."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# engram review — {stamp}", ""]

    def _render_candidate(idx: int, c: dict, kind: str):
        """kind: 'skill' or 'memory'. Writes a single candidate block."""
        action = c.get("action", "?").upper()
        target = c.get("path", "?") if kind == "skill" else c.get("filename", "?")
        conf = c.get("confidence", 0)
        sc = c.get("session_count", "?")
        lines.append(f"### [{idx}] {action} `{target}` — {conf:.2f}, {sc} sessions")
        lines.append(c.get("name", ""))
        if c.get("description") and c["description"] != c.get("name"):
            lines.append(c["description"])
        lines.append("")
        lines.append(f"Evidence: {c.get('evidence', '')}")
        lines.append("")
        content = c.get("content", "")
        if content:
            lines.append("```markdown")
            lines.append(content.rstrip())
            lines.append("```")
        lines.append("")

    if skills:
        lines.append(f"## Skills ({len(skills)})")
        lines.append("")
        for i, s in enumerate(skills, 1):
            _render_candidate(i, s, "skill")

    if memories:
        lines.append(f"## Memories ({len(memories)})")
        lines.append("")
        for i, m in enumerate(memories, 1):
            _render_candidate(i, m, "memory")

    if insights:
        lines.append(f"## Insights ({len(insights)})")
        lines.append("")
        for ins in insights:
            lines.append(f"- [{ins.get('type','?')}] {ins.get('confidence',0):.2f}, {ins.get('session_count','?')} sessions — {ins.get('description','')}")
            if ins.get("suggestion"):
                lines.append(f"  → {ins['suggestion']}")
        lines.append("")

    if session_summaries:
        lines.append("---")
        lines.append("")
        lines.append("## Session summaries (reference)")
        lines.append("")
        for idx, summary in session_summaries:
            lines.append(f"**Session {idx+1}** — {summary.get('goal', '?')} [{summary.get('domain', '?')}]")
            for c in summary.get("corrections", []):
                lines.append(f"- {c.get('rejected', '')} → {c.get('accepted', '')}")
                if c.get("quote"):
                    lines.append(f"  > {c['quote']}")
            for e in summary.get("errors", []):
                lines.append(f"- err: {e.get('error', '')} → {e.get('resolution', '')}")
            if summary.get("notable"):
                lines.append(f"- note: {summary['notable']}")
            lines.append("")

    path.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Self-learning loop for Claude Code")
    parser.add_argument("--hours", type=int, default=24, help="Look back N hours (default: 24)")
    parser.add_argument("--from", dest="from_dt", help="Start datetime (ISO)")
    parser.add_argument("--to", dest="to_dt", help="End datetime (ISO)")
    parser.add_argument("--dry-run", action="store_true", help="Analyze only (default — apply is via Claude Code)")
    parser.add_argument("--show-candidates", action="store_true", help="Print full LLM-generated content for each candidate")
    parser.add_argument("--session", help="Analyze a specific session_id only")
    parser.add_argument("--trace", help="Analyze a specific trace_id only")
    parser.add_argument("--limit", type=int, help="Analyze only top N sessions by span count")
    parser.add_argument("--save", nargs="?", const="auto", metavar="FILE_OR_DIR",
                        help="Save candidates. Bare --save auto-creates runs/<timestamp>_<hours>h/. Pass a path to override.")
    args = parser.parse_args()

    # --save always implies dry-run (apply is delegated to Claude Code via the engram skill)
    if args.save:
        args.dry_run = True

    now = datetime.now(timezone.utc)
    if args.from_dt:
        start = datetime.fromisoformat(args.from_dt.replace("Z", "+00:00"))
        end = datetime.fromisoformat(args.to_dt.replace("Z", "+00:00")) if args.to_dt else now
    else:
        end = now
        start = end - timedelta(hours=args.hours)

    print(f"[learn] time range: {start.isoformat()} -> {end.isoformat()}")
    print(f"[learn] skills: {SKILLS_DIR}")
    print(f"[learn] memory: {MEMORY_DIR}")
    print(f"[learn] dry-run: {args.dry_run}")

    # Phase 1: Collect
    session_data: list[tuple[dict, list[dict], list[dict]]] = []

    if args.trace:
        print(f"[learn] fetching trace {args.trace}")
        detail = staso.fetch_trace_detail(args.trace)
        trace = detail["trace"]
        spans = detail.get("spans", [])
        session = {
            "session_id": trace.get("session_id", args.trace),
            "trace_count": 1, "total_spans": len(spans),
            "error_count": 1 if trace.get("status") == "error" else 0,
            "total_tokens": trace.get("total_tokens", 0),
            "total_duration_ms": trace.get("duration_ms", 0),
        }
        session_data.append((session, [trace], spans))

    elif args.session:
        print(f"[learn] fetching session {args.session}")
        traces = staso.fetch_traces_for_session(args.session, start, end)
        all_spans = []
        for t in traces:
            all_spans.extend(staso.fetch_trace_detail(t["trace_id"]).get("spans", []))
        session = {
            "session_id": args.session, "trace_count": len(traces), "total_spans": len(all_spans),
            "error_count": sum(1 for t in traces if t.get("status") == "error"),
            "total_tokens": sum(t.get("total_tokens", 0) or 0 for t in traces),
            "total_duration_ms": sum(t.get("duration_ms", 0) or 0 for t in traces),
        }
        session_data.append((session, traces, all_spans))

    else:
        print("[learn] fetching conversations...")
        conversations = staso.fetch_conversations(start, end)
        print(f"[learn] found {len(conversations)} conversations")
        interesting = [
            c for c in conversations
            if c.get("total_spans", 0) >= MIN_SPANS
            or c.get("total_duration_ms", 0) >= MIN_DURATION_MS
            or c.get("error_count", 0) > 0
        ]
        interesting.sort(key=lambda c: c.get("total_spans", 0), reverse=True)
        if args.limit:
            interesting = interesting[:args.limit]
        print(f"[learn] fetching {len(interesting)} sessions (from {len(conversations)} total)")
        for ci, conv in enumerate(interesting):
            sid = conv["session_id"]
            _progress("fetched", ci, len(interesting))
            traces = staso.fetch_traces_for_session(sid, start, end)
            all_spans = []
            for t in traces:
                all_spans.extend(staso.fetch_trace_detail(t["trace_id"]).get("spans", []))
            session_data.append((conv, traces, all_spans))
        _progress("fetched", len(interesting), len(interesting))

    if not session_data:
        print("[learn] no sessions to analyze")
        return

    # Phase 2: Compress
    summaries = []
    total_spans = 0
    for i, (session, traces, spans) in enumerate(session_data):
        summary = compress.compress_session(session, traces, spans)
        summaries.append(f"=== Session {i+1} ===\n{summary}")
        total_spans += len(spans)
    combined = "\n\n".join(summaries)
    print(f"[learn] compressed {len(session_data)} sessions ({total_spans} spans)")

    # Phase 3: Analyze (per-session + cross-session)
    skills_index = load_skills_index()
    memory_index = load_memory_index()
    common = COMMON_INSTRUCTIONS

    from engram.llm import call as call_llm, _resolve_provider
    provider, api_key, base_url, model, extra = _resolve_provider()
    if provider == "none":
        print("[learn] no LLM configured -- printing compressed summaries")
        print("\n" + "=" * 60)
        print(combined)
        print("=" * 60)
        print("\nSet OPENROUTER_API_KEY, DEEPSEEK_API_KEY, or LLM_PROVIDER=claude-code")
        return

    # Per-session: cheap/fast model for structured extraction
    # Cross-session: stronger model for pattern detection
    per_session_model = os.environ.get("LLM_MODEL_PER_SESSION", "")
    cross_session_model = os.environ.get("LLM_MODEL_CROSS_SESSION", "")
    per_session_str = f"{provider} / {per_session_model or model}"
    cross_session_str = f"{provider} / {cross_session_model or model}"
    n_days = args.hours // 24 or 1

    # Pass 1: Per-session — extract structured summaries + immediate behavioral candidates
    def _summarize_session(session_text: str) -> dict | None:
        prompt = PER_SESSION.replace("{session_summaries}", session_text)
        try:
            return call_llm(prompt, model_override=per_session_model)
        except Exception as e:
            print(f"[learn] LLM error (per-session): {e}")
            return None

    print(f"[learn] per-session pass ({per_session_str})")
    session_llm_summaries = []
    immediate_candidates = []
    completed = 0
    with ThreadPoolExecutor(max_workers=min(len(summaries), 6)) as pool:
        futures = {pool.submit(_summarize_session, s): i for i, s in enumerate(summaries)}
        for f in as_completed(futures):
            completed += 1
            _progress("sessions", completed, len(summaries))
            idx = futures[f]
            result = f.result()
            if result:
                summary = result.get("session_summary", result)
                session_llm_summaries.append((idx, summary))
                imm = result.get("immediate_candidates", [])
                for c in imm:
                    c["session_count"] = 1
                    c["action"] = c.get("action", "create")
                    c.setdefault("confidence", 0.65)
                    c["confidence"] = min(c["confidence"], IMMEDIATE_CONFIDENCE_CAP)
                    if c.get("kind") == "skill" and not c.get("path"):
                        c["path"] = "common/" + c.get("name", "unknown").lower().replace(" ", "-")
                    if c.get("kind") != "skill":
                        c["filename"] = _safe_filename(c.get("filename") or c.get("name", "unknown"))
                    if not c.get("type") and c.get("kind") != "skill":
                        c["type"] = "feedback"
                immediate_candidates.extend(imm)

    session_llm_summaries.sort(key=lambda x: x[0])
    if not session_llm_summaries:
        print("[learn] no sessions summarized successfully")
        return

    # Drop trivial sessions
    def _is_trivial(summary: dict) -> bool:
        return (
            not summary.get("corrections")
            and not summary.get("content_produced")
            and not summary.get("notable")
            and len(summary.get("errors", [])) <= 1
        )

    before = len(session_llm_summaries)
    session_llm_summaries = [(idx, s) for idx, s in session_llm_summaries if not _is_trivial(s)]
    dropped = before - len(session_llm_summaries)
    total_corrections = sum(len(s.get("corrections", [])) for _, s in session_llm_summaries)
    print(f"[learn] per-session: {len(session_llm_summaries)} sessions ({dropped} trivial dropped), {total_corrections} corrections, {len(immediate_candidates)} immediate candidates")

    # Build structured input for cross-session pass
    structured_summaries = []
    for idx, summary in session_llm_summaries:
        structured_summaries.append(f"=== Session {idx+1} ===\n{json.dumps(summary, indent=2)}")
    cross_session_input = "\n\n".join(structured_summaries)

    # Pass 2: Cross-session — find patterns, generate candidates
    if len(session_llm_summaries) < 2:
        print("\n[learn] single session -- skipping cross-session (need 2+)")
        if immediate_candidates:
            print(f"[learn] {len(immediate_candidates)} per-session candidate(s) without gatekeeper review — recommend a wider --hours window")
        else:
            print("[learn] run with --hours 168 or more to analyze multiple sessions")
        return

    # Format immediate candidates for cross-session context
    if immediate_candidates:
        imm_lines = []
        for i, c in enumerate(immediate_candidates, 1):
            imm_lines.append(f"  [{i}] ({c.get('confidence', 0.65):.2f}) {c.get('name', '?')} -- {c.get('evidence', '')[:200]}")
        imm_context = "\n".join(imm_lines)
    else:
        imm_context = "  (none)"

    print(f"[learn] cross-session pass ({cross_session_str})")
    cross_prompt = (
        CROSS_SESSION
        .replace("{n_days}", str(n_days))
        .replace("{n_sessions}", str(len(session_llm_summaries)))
        .replace("{common_instructions}", common)
        .replace("{skills_index}", skills_index)
        .replace("{memory_index}", memory_index)
        .replace("{immediate_candidates}", imm_context)
        .replace("{session_summaries}", cross_session_input)
    )

    import threading
    _cross_start = time.time()
    _stop = threading.Event()
    _n_sess = len(session_llm_summaries)

    def _tick():
        while not _stop.is_set():
            el = _fmt_elapsed(time.time() - _cross_start)
            print(f"\r\033[K  analyzing  {_n_sess} sessions  {el}", end="", flush=True)
            _stop.wait(1.0)

    if _IS_TTY:
        _ticker = threading.Thread(target=_tick, daemon=True)
        _ticker.start()
    else:
        print(f"  analyzing {_n_sess} sessions...", flush=True)
        _ticker = None

    try:
        result = call_llm(cross_prompt, model_override=cross_session_model)
    except Exception as e:
        if _ticker:
            _stop.set()
            _ticker.join(timeout=0.2)
        print(f"\r\033[K  error: {e}", flush=True)
        return
    finally:
        if _ticker:
            _stop.set()
            _ticker.join(timeout=0.2)
    el = _fmt_elapsed(time.time() - _cross_start)
    if _IS_TTY:
        print(f"\r\033[K  analyzed   {_n_sess} sessions  {el}", flush=True)
    else:
        print(f"  analyzed {_n_sess} sessions in {el}", flush=True)

    # R1 is the gatekeeper — its output already includes filtered/promoted per-session candidates.
    # No more code-level merging or dedup; what R1 says is what ships.
    candidate_skills = result.get("candidate_skills", [])
    candidate_memories = result.get("candidate_memories", [])
    insights = result.get("insights", [])

    print(f"\n[learn] results: {len(candidate_skills)} skills, {len(candidate_memories)} memories, {len(insights)} insights")

    # --save: dump candidates to JSON + generate readable review markdown
    if args.save:
        save_arg = pathlib.Path(args.save)
        # If user passed a directory OR the default "auto" sentinel, use runs/<timestamp>/
        if str(save_arg) == "auto" or save_arg.is_dir() or save_arg.suffix == "":
            run_dir = _make_run_dir(args.hours)
            save_path = run_dir / "candidates.json"
            review_path = run_dir / "candidates.md"
        else:
            # User provided explicit filename — use it as-is
            save_path = save_arg
            review_path = save_arg.with_suffix(".md")
        save_data = {
            "candidate_skills": candidate_skills,
            "candidate_memories": candidate_memories,
            "insights": insights,
        }
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(json.dumps(save_data, indent=2))
        _write_review_md(review_path, candidate_skills, candidate_memories, insights, session_llm_summaries)
        print(f"[learn] saved to {save_path}")
        print(f"[learn] review: {review_path}")

    # Show candidates (compact terminal output)
    if args.show_candidates:
        sep = "-" * 60
        if candidate_skills:
            print(f"\n{sep}\nSKILLS ({len(candidate_skills)})\n{sep}")
            for i, s in enumerate(candidate_skills, 1):
                print(f"\n  [{i}] {s.get('action','?').upper()} {s.get('path','?')}  ({s.get('confidence',0):.2f}, {s.get('session_count','?')} sessions)")
                print(f"      {s.get('name','')}: {s.get('description','')}")
                print(f"      evidence: {s.get('evidence','')[:120]}")

        if candidate_memories:
            print(f"\n{sep}\nMEMORIES ({len(candidate_memories)})\n{sep}")
            for i, m in enumerate(candidate_memories, 1):
                print(f"\n  [{i}] {m.get('action','?').upper()} {m.get('filename','?')}  ({m.get('confidence',0):.2f}, {m.get('session_count','?')} sessions)")
                print(f"      {m.get('name','')}")
                print(f"      evidence: {m.get('evidence','')[:120]}")

        if insights:
            print(f"\n{sep}\nINSIGHTS ({len(insights)})\n{sep}")
            for i, ins in enumerate(insights, 1):
                print(f"\n  [{i}] [{ins.get('type','?')}] ({ins.get('confidence',0):.2f}, {ins.get('session_count','?')} sessions)")
                print(f"      {ins.get('description','')[:120]}")
        print(f"\n{sep}")

    # Apply is delegated to Claude Code via the engram skill — see .claude/skills/engram/SKILL.md
    print(f"\n[learn] done -- {len(candidate_skills)} skills, {len(candidate_memories)} memories, {len(insights)} insights")
    if args.save:
        print("[learn] next: open Claude Code in this repo and run /engram-apply runs/latest/candidates.md")


if __name__ == "__main__":
    main()
