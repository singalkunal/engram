#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-dotenv"]
# ///
"""
engram — self-learning loop for coding agents.
Mines session traces, detects personal patterns, auto-updates skills + memory.

Usage:
  uv run python -m engram.learn --hours 168 --dry-run --show-candidates
  uv run python -m engram.learn --hours 24
  uv run python -m engram.learn --hours 168 --save candidates.json
  uv run python -m engram.learn --apply-from candidates.json
"""

import argparse
import json
import os
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

# Load .env from cwd, then project root
load_dotenv(pathlib.Path.cwd() / ".env")
load_dotenv(pathlib.Path(__file__).parent.parent / ".env", override=True)

from engram import staso, compress, llm
from engram.prompts import PER_SESSION, CROSS_SESSION, COMMON_INSTRUCTIONS


def _progress(label: str, done: int, total: int, suffix: str = ""):
    """Print an inline progress bar that overwrites itself."""
    w = 20
    filled = int(w * done / total) if total else w
    bar = "=" * filled + "-" * (w - filled)
    end = "\n" if done >= total else "\r"
    extra = f" {suffix}" if suffix else ""
    print(f"  [{bar}] {done}/{total} {label}{extra}", end=end, flush=True)

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
EVOLUTION_LOG = pathlib.Path(__file__).parent.parent / "evolution.tsv"

# Thresholds
AUTO_APPLY = 0.90
FLAG_REVIEW = 0.70
MIN_LOG = 0.50
IMMEDIATE_CONFIDENCE_CAP = 0.65  # single-session behavioral candidates capped here

# Session filters
MIN_SPANS = 10
MIN_DURATION_MS = 120_000

# ---------------------------------------------------------------------------
# Context loaders
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
# Apply candidates
# ---------------------------------------------------------------------------

def apply_skill(candidate: dict, dry_run: bool) -> str:
    path = SKILLS_DIR / candidate["path"] / "SKILL.md"
    action = candidate["action"]
    if dry_run:
        return f"[dry-run] would {action} skill at {path}"
    content = candidate.get("content", "")
    if action == "create":
        path.parent.mkdir(parents=True, exist_ok=True)
        if content:
            path.write_text(content)
        else:
            path.write_text(f"---\nname: {candidate['name']}\ndescription: {candidate['description']}\n---\n\n# {candidate['name']}\n\n{candidate.get('evidence', '')}\n")
        return f"created {path}"
    elif action == "update" and path.exists():
        if content:
            path.write_text(content)
        else:
            existing = path.read_text()
            note = f"\n\n## Update ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})\n{candidate.get('evidence', '')}"
            path.write_text(existing + note)
        return f"updated {path}"
    return f"skipped {action} {path}"


def apply_memory(candidate: dict, dry_run: bool) -> str:
    filename = candidate["filename"]
    mem_path = MEMORY_DIR / filename
    action = candidate["action"]
    if dry_run:
        return f"[dry-run] would {action} memory at {mem_path}"
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    content = candidate.get("content", "")
    if not content:
        content = f"""---
name: {candidate['name']}
description: {candidate['description']}
type: {candidate.get('type', 'feedback')}
---

{candidate.get('evidence', '')}
"""
    if action == "create" or (action == "update" and not mem_path.exists()):
        mem_path.write_text(content)
        if MEMORY_MD.exists():
            index = MEMORY_MD.read_text()
            entry = f"- [{candidate['name']}]({filename}) -- {candidate['description']}"
            if filename not in index:
                MEMORY_MD.write_text(index.rstrip() + "\n" + entry + "\n")
        return f"created {mem_path}"
    elif action == "update" and mem_path.exists():
        mem_path.write_text(content)
        return f"updated {mem_path}"
    return f"skipped {action} {mem_path}"

# ---------------------------------------------------------------------------
# Evolution log
# ---------------------------------------------------------------------------

def log_evolution(entries: list[dict], dry_run: bool):
    write_header = not EVOLUTION_LOG.exists()
    mode = "dry-run" if dry_run else "live"
    staso_url = os.environ.get("STASO_API_URL", "")
    with open(EVOLUTION_LOG, "a") as f:
        if write_header:
            f.write("date\tmode\tstaso_url\taction\ttype\tpath\tconfidence\tapplied\tevidence\n")
        for e in entries:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            f.write(
                f"{date}\t{mode}\t{staso_url}\t{e['action']}\t{e['type']}\t{e.get('path', '-')}\t"
                f"{e['confidence']:.2f}\t{e['applied']}\t{e.get('evidence', '')[:100]}\n"
            )

# ---------------------------------------------------------------------------
# Review file
# ---------------------------------------------------------------------------

def _write_review_md(path: pathlib.Path, skills: list, memories: list, insights: list,
                     session_summaries: list[tuple[int, dict]] | None = None):
    """Generate a human-readable markdown review of candidates."""
    lines = [f"# engram review -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n"]
    lines.append(f"Delete unwanted entries from the companion `.json` file, then run `--apply-from`.\n")

    if skills:
        lines.append(f"## Skills ({len(skills)})\n")
        for i, s in enumerate(skills, 1):
            lines.append(f"### [{i}] {s.get('action','?').upper()} `{s.get('path','?')}` -- confidence {s.get('confidence',0):.2f} ({s.get('session_count','?')} sessions)\n")
            lines.append(f"**{s.get('name','')}**: {s.get('description','')}\n")
            lines.append(f"**Evidence**: {s.get('evidence','')}\n")
            content = s.get("content", "")
            if content:
                lines.append(f"**Content that would be written:**\n")
                lines.append(f"```markdown\n{content}\n```\n")

    if memories:
        lines.append(f"## Memories ({len(memories)})\n")
        for i, m in enumerate(memories, 1):
            lines.append(f"### [{i}] {m.get('action','?').upper()} `{m.get('filename','?')}` -- confidence {m.get('confidence',0):.2f} ({m.get('session_count','?')} sessions)\n")
            lines.append(f"**{m.get('name','')}** ({m.get('type','')})\n")
            lines.append(f"**Evidence**: {m.get('evidence','')}\n")
            content = m.get("content", "")
            if content:
                lines.append(f"**Content that would be written:**\n")
                lines.append(f"```markdown\n{content}\n```\n")

    if insights:
        lines.append(f"## Insights ({len(insights)})\n")
        for i, ins in enumerate(insights, 1):
            lines.append(f"- **[{ins.get('type','?')}]** ({ins.get('confidence',0):.2f}, {ins.get('session_count','?')} sessions): {ins.get('description','')}")
            if ins.get("suggestion"):
                lines.append(f"  - Suggestion: {ins['suggestion']}")
            lines.append("")

    # Append per-session summaries for reference
    if session_summaries:
        lines.append(f"\n---\n\n## Per-Session Summaries (reference)\n")
        for idx, summary in session_summaries:
            goal = summary.get('goal', '?')
            lines.append(f"### Session {idx+1}: {goal} [{summary.get('domain', '?')}]\n")
            for c in summary.get("corrections", []):
                lines.append(f"- **correction**: {c.get('rejected', '')} -> {c.get('accepted', '')}")
                if c.get("quote"):
                    lines.append(f"  > \"{c['quote']}\"")
            for e in summary.get("errors", []):
                lines.append(f"- **error**: {e.get('error', '')} -> {e.get('resolution', '')}")
            if summary.get("notable"):
                lines.append(f"- **notable**: {summary['notable']}")
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
    parser.add_argument("--dry-run", action="store_true", help="Analyze only, don't write files")
    parser.add_argument("--show-candidates", action="store_true", help="Print full LLM-generated content for each candidate")
    parser.add_argument("--min-confidence", type=float, default=MIN_LOG, help="Minimum confidence to log")
    parser.add_argument("--session", help="Analyze a specific session_id only")
    parser.add_argument("--trace", help="Analyze a specific trace_id only")
    parser.add_argument("--limit", type=int, help="Analyze only top N sessions by span count")
    parser.add_argument("--save", metavar="FILE", help="Save candidates to JSON file after analysis")
    parser.add_argument("--apply-from", metavar="FILE", dest="apply_from", help="Apply candidates from a previously saved JSON file (skip collect/compress/analyze)")
    args = parser.parse_args()

    # --save implies dry-run (candidates are saved for later, not applied now)
    if args.save:
        args.dry_run = True

    # --apply-from: skip everything, just apply from saved JSON
    if args.apply_from:
        p = pathlib.Path(args.apply_from)
        if not p.exists():
            print(f"[learn] file not found: {p}")
            sys.exit(1)
        data = json.loads(p.read_text())
        candidate_skills = data.get("candidate_skills", [])
        candidate_memories = data.get("candidate_memories", [])
        insights = data.get("insights", [])
        print(f"[learn] loaded {len(candidate_skills)} skills, {len(candidate_memories)} memories, {len(insights)} insights from {p}")
        print(f"[learn] skills: {SKILLS_DIR}")
        print(f"[learn] memory: {MEMORY_DIR}")
        print(f"[learn] dry-run: {args.dry_run}")
        # User already cherry-picked, so apply everything (override min-confidence and thresholds)
        args.min_confidence = 0
        _apply_and_log(candidate_skills, candidate_memories, insights, args, force_apply=True)
        return

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
                    if c.get("kind") != "skill" and not c.get("filename"):
                        c["filename"] = c.get("name", "unknown").lower().replace(" ", "_").replace("-", "_") + ".md"
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
            print(f"[learn] processing {len(immediate_candidates)} behavioral candidate(s) from per-session")
            _apply_and_log([], immediate_candidates, [], args, force_apply=False)
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

    print(f"  [analyzing {len(session_llm_summaries)} sessions...]", end="", flush=True)
    try:
        result = call_llm(cross_prompt, model_override=cross_session_model)
        print(" done")
    except Exception as e:
        print(f" error: {e}")
        return

    candidate_skills = result.get("candidate_skills", [])
    candidate_memories = result.get("candidate_memories", [])
    insights = result.get("insights", [])

    # Merge immediate behavioral candidates from per-session pass
    # Dedup: fuzzy match — normalize names to keywords and skip if cross-session covers it
    def _name_keys(candidate: dict) -> set[str]:
        raw = (candidate.get("filename", "") + " " + candidate.get("name", "") + " " + candidate.get("path", "")).lower()
        for prefix in ("feedback_", "memory_", "user_", "project_", "reference_", "common/", "backend/", "frontend/"):
            raw = raw.replace(prefix, " ")
        raw = raw.replace(".md", "").replace("_", " ").replace("-", " ").replace("/", " ")
        return {w for w in raw.split() if len(w) > 2}

    # Dedup immediate candidates against both cross-session memories AND skills
    all_cross = candidate_memories + [{"name": s.get("name", ""), "filename": "", "path": s.get("path", "")} for s in candidate_skills]
    cross_keysets = [_name_keys(m) for m in all_cross]
    merged_immediate = 0
    for imm in immediate_candidates:
        imm_keys = _name_keys(imm)
        is_dup = any(
            len(imm_keys & ck) >= max(2, len(imm_keys) * 0.5)
            for ck in cross_keysets
        )
        if not is_dup:
            # Route based on kind: skill candidates go to skills, everything else to memories
            if imm.get("kind") == "skill" and imm.get("path"):
                candidate_skills.append(imm)
            else:
                candidate_memories.append(imm)
            cross_keysets.append(imm_keys)
            merged_immediate += 1

    cross_count = len(candidate_memories) + len(candidate_skills) - merged_immediate
    print(f"\n[learn] results: {len(candidate_skills)} skills, {len(candidate_memories)} memories ({merged_immediate} from per-session), {len(insights)} insights")

    # --save: dump candidates to JSON + generate readable review markdown
    if args.save:
        save_path = pathlib.Path(args.save)
        save_data = {
            "candidate_skills": candidate_skills,
            "candidate_memories": candidate_memories,
            "insights": insights,
        }
        save_path.write_text(json.dumps(save_data, indent=2))
        # Generate companion review file
        review_path = save_path.with_suffix(".md")
        _write_review_md(review_path, candidate_skills, candidate_memories, insights, session_llm_summaries)
        print(f"[learn] saved to {save_path}")
        print(f"[learn] review: {review_path}")
        print(f"[learn] edit {save_path.name} to remove unwanted candidates, then:")
        print(f"  uv run python -m engram.learn --apply-from {save_path}")

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

    _apply_and_log(candidate_skills, candidate_memories, insights, args,
                    force_apply=False, quiet=args.show_candidates)


def _apply_and_log(candidate_skills, candidate_memories, insights, args,
                    force_apply=False, quiet=False):
    """Apply candidates and log evolution. Used by both normal flow and --apply-from.
    When force_apply=True (from --apply-from), all candidates are applied regardless of confidence.
    When quiet=True, only print auto-applied entries (skip flag/log lines already shown by --show-candidates)."""
    log_entries = []

    if not quiet:
        print("\n[learn] skills:")
    for s in candidate_skills:
        conf = s.get("confidence", 0)
        path = s.get("path", "?")
        action = s.get("action", "?")
        evidence = s.get("evidence", "")
        if conf < args.min_confidence:
            continue
        status = "log-only"
        if force_apply or conf >= AUTO_APPLY:
            msg = apply_skill(s, dry_run=args.dry_run)
            status = "auto-applied" if not args.dry_run else "dry-run"
            print(f"  apply ({conf:.2f}) [{action}] {path} -- {msg}")
        elif conf >= FLAG_REVIEW:
            if not quiet:
                print(f"  flag  ({conf:.2f}) [{action}] {path} -- {evidence}")
            status = "flagged"
        else:
            if not quiet:
                print(f"  log   ({conf:.2f}) [{action}] {path}")
        log_entries.append({"action": action, "type": "skill", "path": path,
                            "confidence": conf, "applied": status, "evidence": evidence})

    if not quiet:
        print("\n[learn] memories:")
    for m in candidate_memories:
        conf = m.get("confidence", 0)
        filename = m.get("filename", "?")
        action = m.get("action", "?")
        evidence = m.get("evidence", "")
        if conf < args.min_confidence:
            continue
        status = "log-only"
        if force_apply or conf >= AUTO_APPLY:
            msg = apply_memory(m, dry_run=args.dry_run)
            status = "auto-applied" if not args.dry_run else "dry-run"
            print(f"  apply ({conf:.2f}) [{action}] {filename} -- {msg}")
        elif conf >= FLAG_REVIEW:
            if not quiet:
                print(f"  flag  ({conf:.2f}) [{action}] {filename} -- {evidence}")
            status = "flagged"
        else:
            if not quiet:
                print(f"  log   ({conf:.2f}) [{action}] {filename}")
        log_entries.append({"action": action, "type": "memory", "path": filename,
                            "confidence": conf, "applied": status, "evidence": evidence})

    if not quiet:
        print("\n[learn] insights:")
    for ins in insights:
        conf = ins.get("confidence", 0)
        if conf >= args.min_confidence:
            if not quiet:
                print(f"  ({conf:.2f}) [{ins.get('type','?')}] {ins.get('description','')}")
                if ins.get("suggestion"):
                    print(f"          -> {ins.get('suggestion','')}")
            log_entries.append({"action": "insight", "type": ins.get("type", "?"), "path": "-",
                                "confidence": conf, "applied": "logged", "evidence": ins.get("description", "")[:100]})

    if log_entries:
        log_evolution(log_entries, dry_run=args.dry_run)

    applied = sum(1 for e in log_entries if e["applied"] == "auto-applied")
    flagged = sum(1 for e in log_entries if e["applied"] == "flagged")
    print(f"\n[learn] done -- {len(log_entries)} logged, {applied} applied, {flagged} flagged")


if __name__ == "__main__":
    main()
