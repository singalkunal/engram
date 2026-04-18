# engram

Your coding agent forgets between sessions. engram makes it remember.

Mines session traces, finds personal patterns, writes them back as Claude Code skills and memory entries.

## How it works

```
traces -> compress -> per-session extract -> cross-session gatekeeper -> apply
```

1. **Fetch** recent sessions
2. **Compress** each into structured signal (prompts, corrections, errors, content produced)
3. **Per-session pass** (cheap model): extract candidate hints permissively
4. **Cross-session pass** (strong reasoning model): act as gatekeeper — filter feature-specific noise, promote recurring patterns, output the final candidate list
5. **Apply** based on confidence

Confidence scales with recurrence:

| Sessions | Cap |
|---|---|
| 1 | 0.65 |
| 2 | 0.70 |
| 3 | 0.85 |
| 4+ | 0.95 |

The bar: *would a fresh AI session get this wrong without this info?* If no, drop it.

## Quickstart

```bash
git clone https://github.com/AgenticLeash/engram.git
cd engram
cp .env.example .env  # fill in trace key + LLM key (or use Claude Code, see below)
uv run python -m engram.learn --hours 168 --save
```

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

> **No LLM API key?** Set `LLM_PROVIDER=claude-code` in `.env` and engram runs through your local Claude Code CLI. Uses your existing subscription, no extra cost, no third-party LLM keys.

## Usage

```bash
# generate candidates (engram's only job)
uv run python -m engram.learn --hours 168 --save

# scoped runs
uv run python -m engram.learn --session <id> --dry-run
uv run python -m engram.learn --trace <id> --dry-run
uv run python -m engram.learn --hours 168 --limit 3 --dry-run

# model override
LLM_MODEL=deepseek/deepseek-r1-0528 uv run python -m engram.learn --hours 168 --dry-run
```

Then in Claude Code (in the repo where you want to apply learnings):

```
/engram-apply runs/latest/candidates.md
```

The `/engram-apply` skill (shipped in `.claude/skills/engram-apply/`) reads your local skill/memory layout, places each candidate in the right subdirectory, updates parent index files and `MEMORY.md`, and skips anything that doesn't fit your conventions.

**Why the split?** Engram analyzes cross-session traces (Claude Code can't see those). Claude Code knows your local layout, conventions, and what's already there (engram doesn't). Each does what only it can do.

### Output

```
[learn] fetching 12 sessions (from 17 total)
  ████████████████████████  100%  12/12  fetched  8s
[learn] compressed 12 sessions (5415 spans)
[learn] per-session pass (openrouter / google/gemini-2.5-flash)
  ████████████████████████  100%  12/12  sessions  19s
[learn] cross-session pass (openrouter / deepseek/deepseek-r1-0528)
  analyzed   12 sessions  4m02s
[learn] results: 2 skills, 5 memories, 1 insights
[learn] saved to runs/2026-04-18T04-06_168h/candidates.json
[learn] review: runs/2026-04-18T04-06_168h/candidates.md
[learn] done -- 2 skills, 5 memories, 1 insights
[learn] next: open Claude Code in this repo and run /engram-apply runs/latest/candidates.md
```

### Run organization

Each `--save` creates a timestamped directory:

```
runs/
├── 2026-04-15T12-00_168h/
│   ├── candidates.json   # machine-readable, edit to drop entries
│   └── candidates.md     # human-readable review
└── latest -> 2026-04-15T12-00_168h
```

## Configuration

```bash
STASO_API_KEY=                                       # or your trace provider's key
LLM_PROVIDER=openrouter                              # openrouter | deepseek | claude-code
OPENROUTER_API_KEY=
LLM_MODEL=deepseek/deepseek-chat                     # default for both passes

# per-pass overrides (optional but recommended)
LLM_MODEL_PER_SESSION=google/gemini-2.5-flash        # cheap extraction
LLM_MODEL_CROSS_SESSION=deepseek/deepseek-r1-0528    # gatekeeper, needs reasoning
```

### Models

| Model | Cost | Use |
|---|---|---|
| `google/gemini-2.0-flash-001` | $0.10/M | Cheap per-session extraction |
| `google/gemini-2.5-flash` | $0.30/M | Better per-session extraction |
| `deepseek/deepseek-r1-0528` | $0.50/M | Cross-session gatekeeper (recommended) |
| `anthropic/claude-sonnet-4` | $3/M | Highest quality |
| `claude-code` | free | Uses your local Claude Code subscription, no API key needed |

## What it finds

Real examples (sanitized).

**Skill promoted from 3 sessions:**

```markdown
---
name: external-content-style
description: User-facing content — professional, outcome-oriented, non-spammy
---
# External Content Style

When creating UI text, marketing, changelogs, announcements:

1. Focus on outcomes ("X now available" beats "We added X")
2. Avoid fluff, hype words, overselling
3. Serious founder-focused voice, no casual slang, no emojis
4. Emphasize utility over promotion
5. Consistent branding, loading states for async ops
```

**Memory captured from 2 sessions:**

```markdown
---
name: Avoid Security Cliches
type: feedback
---
Never use stereotypical security imagery (shields, locks) in logos or branding.
Prioritize unique, abstract geometric designs that convey precision.
```

**What gets filtered out:** platform limits, tool errors, project-specific implementation choices, one-off product decisions. The gatekeeper rejects anything that wouldn't apply on a different project.

## Trace providers

Currently uses [Staso](https://staso.ai). Swap `staso.py` for other providers.

## Inspiration

- [Hermes Agent](https://hermes-agent.nousresearch.com) — in-session self-learning, cross-session recall via SQLite FTS5
- [OpenClaw](https://github.com/openclaw/openclaw) — personal AI assistant with memory and skill acquisition
- Karpathy's [autoresearch](https://github.com/karpathy/autoresearch) — modify, test, keep/discard with git as memory

## License

MIT
