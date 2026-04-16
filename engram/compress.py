"""Compress trace spans into structured summaries for LLM analysis."""

import json
from collections import Counter


def _extract_text(raw: str, max_len: int = 500) -> str:
    """Pull readable text out of a span input/output JSON blob."""
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw[:max_len]

    if isinstance(obj, dict):
        for key in ("prompt", "message", "text", "query", "question"):
            if key in obj and isinstance(obj[key], str):
                return obj[key][:max_len]
        if "tool_input" in obj and isinstance(obj["tool_input"], dict):
            ti = obj["tool_input"]
            if "content" in ti:
                return f"[file: {ti.get('file_path', '?')}] {ti['content'][:max_len]}"
            if "new_string" in ti:
                return f"[edit: {ti.get('file_path', '?')}] {ti.get('old_string', '')[:100]} -> {ti['new_string'][:200]}"
            if "command" in ti:
                return ti["command"][:max_len]
            return json.dumps(ti)[:max_len]
        if "content" in obj and isinstance(obj["content"], list):
            texts = [c.get("text", "") for c in obj["content"] if c.get("type") == "text" and c.get("text")]
            if texts:
                return " ".join(texts)[:max_len]
        if "tool_response" in obj and isinstance(obj["tool_response"], dict):
            tr = obj["tool_response"]
            if "stdout" in tr:
                return tr["stdout"][:max_len]
            return json.dumps(tr)[:max_len]
        if "response" in obj and isinstance(obj["response"], str):
            return obj["response"][:max_len]
    return raw[:max_len]


def compress_session(session: dict, traces: list[dict], all_spans: list[dict]) -> str:
    """Compress a session into a structured summary with personal signals."""
    session_id = session.get("session_id", "unknown")
    total_cost = sum(t.get("estimated_cost_usd", 0) or 0 for t in traces)
    models = list({t.get("model", "") for t in traces if t.get("model")})
    error_count = session.get("error_count", 0)
    total_tokens = session.get("total_tokens", 0)
    duration_ms = session.get("total_duration_ms", 0) or 0

    first_trace = traces[0] if traces else {}
    vcs_branch = first_trace.get("vcs_branch", "")
    vcs_sha = (first_trace.get("vcs_commit_sha") or "")[:8]

    lines = [
        f"Session: {session_id}",
        f"Traces: {len(traces)} | Spans: {len(all_spans)} | Duration: {duration_ms/1000:.0f}s",
        f"Models: {', '.join(models)} | Cost: ${total_cost:.3f} | Tokens: {total_tokens}",
        f"Status: {'error' if error_count else 'ok'} ({error_count} errors)",
        f"VCS: {vcs_branch}@{vcs_sha}",
    ]

    # 1. User prompts
    user_prompts = []
    for t in traces:
        prompt = t.get("root_span_name") or ""
        if not prompt:
            preview = t.get("input_preview") or ""
            prompt = _extract_text(preview, 300)
        if prompt and len(prompt) > 20:
            user_prompts.append(prompt[:300])

    if user_prompts:
        lines.append("")
        lines.append("User prompts (what user asked, each turn):")
        for i, p in enumerate(user_prompts[:15], 1):
            lines.append(f"  [{i}] {p}")

    # 2. Conversation flow
    lines.append("")
    lines.append("Conversation flow (key exchanges):")
    for s in all_spans:
        if s.get("kind") == "agent" and not s.get("parent_span_id"):
            inp = _extract_text(s.get("input", ""), 400)
            out = _extract_text(s.get("output", ""), 400)
            if inp:
                lines.append(f"  USER: {inp[:400]}")
            if out:
                lines.append(f"  CLAUDE: {out[:400]}")
            lines.append("")

    # 3. Content produced
    writes, edits = [], []
    for s in all_spans:
        name = s.get("name") or ""
        inp = s.get("input") or ""
        if "Write" in name and inp:
            writes.append(_extract_text(inp, 600))
        elif "Edit" in name and inp:
            edits.append(_extract_text(inp, 400))

    if writes or edits:
        lines.append("")
        lines.append("Content produced (files written/edited):")
        for w in writes[:5]:
            lines.append(f"  WRITE: {w}")
        for e in edits[:8]:
            lines.append(f"  EDIT: {e}")

    # 4. Corrections
    correction_keywords = [
        "no ", "not ", "don't", "dont", "stop", "wrong", "instead",
        "rewrite", "redo", "change", "fix", "actually", "I meant",
        "that's not", "thats not", "try again", "too ", "more ", "less ",
    ]
    corrections = [p[:300] for p in user_prompts if any(kw in p.lower() for kw in correction_keywords)]
    if corrections:
        lines.append("")
        lines.append("Corrections / redirections (user changed course):")
        for c in corrections:
            lines.append(f"  > {c}")

    # 5. Errors
    error_spans = [s for s in all_spans if s.get("status") == "error" or s.get("error_message")]
    if error_spans:
        lines.append("")
        lines.append("Errors:")
        for s in error_spans[:5]:
            name = (s.get("name") or "").replace("tool:", "")
            err = (s.get("error_message") or "")[:200]
            inp = _extract_text(s.get("input", ""), 200)
            lines.append(f"  [{name}] {err}")
            if inp:
                lines.append(f"    input: {inp}")

    # 6. Tool usage
    tool_counts: Counter = Counter()
    for s in all_spans:
        if s.get("kind") == "tool":
            tool_counts[(s.get("name") or "").replace("tool:", "")] += 1
    lines.append("")
    lines.append("Tool usage:")
    for tool, count in tool_counts.most_common(10):
        lines.append(f"  {tool}: {count}")

    # 7. Subagents
    subagents = [s for s in all_spans if s.get("kind") == "agent" and s.get("parent_span_id")]
    if subagents:
        lines.append("")
        lines.append("Subagents:")
        for s in subagents:
            name = (s.get("name") or "").replace("subagent:", "")
            dur = (s.get("duration_ms") or 0) / 1000
            task = _extract_text(s.get("input", ""), 200)
            lines.append(f"  {name} ({dur:.0f}s): {task}")

    return "\n".join(lines)
