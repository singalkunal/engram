# engram

  Your coding agent forgets everything between sessions. This script helps agent remember them by mutating its skills and memories.

Mines session traces, detects personal patterns via LLM analysis, writes results back as Claude Code skills and memory entries.

## How it works

```
traces --> compress --> per-session summarize --> cross-session patterns --> apply
```

1. **Collect** -- Fetch recent sessions from trace provider (Staso)
2. **Compress** -- Extract signal: prompts, corrections, errors, tool usage, content produced
3. **Per-session** -- Cheap/fast LLM: structured extraction + immediate behavioral candidates
4. **Cross-session** -- Stronger LLM: find patterns across 2+ sessions a fresh session would get wrong
5. **Apply** -- Write skill/memory files based on confidence and recurrence

### Two types of learnings

**Cross-session patterns** (need 2+ sessions): the same correction repeated across sessions is a rule, not noise. Confidence scales with recurrence.

| Sessions | Confidence cap |
|---|---|
| 2 | 0.70 |
| 3 | 0.85 |
| 4+ | 0.95 |

**Immediate behavioral candidates** (single session, 0.65 cap): explicit user directives about agent behavior -- "don't ever mock databases", "no emojis", "use uv not pip". The test: would this apply on a completely different project? If yes, it's behavioral and gets extracted immediately. Feature-specific corrections ("use this endpoint") stay in summaries for cross-session analysis only.

## Quickstart

```bash
git clone https://github.com/AgenticLeash/engram.git
cd engram
cp .env.example .env
# fill in trace provider API key + LLM key
uv run python -m engram.learn --hours 168 --show-candidates
```

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

## Usage

```bash
# last 7 days, dry run
uv run python -m engram.learn --hours 168 --show-candidates

# last 24 hours, apply high-confidence results
uv run python -m engram.learn --hours 24

# top 3 most active sessions only
uv run python -m engram.learn --hours 168 --limit 3 --dry-run

# specific session or trace
uv run python -m engram.learn --session <id> --dry-run
uv run python -m engram.learn --trace <id> --dry-run

# cherry-pick: save candidates, edit, then apply
uv run python -m engram.learn --hours 168 --save candidates.json
# edit candidates.json to keep only what you want
uv run python -m engram.learn --apply-from candidates.json

# override model at runtime
LLM_MODEL=deepseek/deepseek-r1-0528 uv run python -m engram.learn --hours 168 --dry-run
```

## Configuration

See [`.env.example`](.env.example).

```bash
STASO_API_KEY=                        # required
STASO_API_URL=https://api.staso.ai

LLM_PROVIDER=openrouter               # openrouter | deepseek | claude-code
OPENROUTER_API_KEY=
LLM_MODEL=deepseek/deepseek-chat      # fallback for both passes

# per-pass model overrides (optional)
LLM_MODEL_PER_SESSION=google/gemini-2.0-flash-001   # cheap, fast extraction
LLM_MODEL_CROSS_SESSION=google/gemini-2.5-flash      # stronger pattern detection
```

### Models

| Model | Cost | Use |
|---|---|---|
| `google/gemini-2.0-flash-001` | $0.10/M | Per-session extraction |
| `deepseek/deepseek-chat` | $0.14/M | Good default for both |
| `google/gemini-2.5-flash` | $0.30/M | Cross-session analysis |
| `deepseek/deepseek-r1-0528` | $0.50/M | Deep pattern detection |
| `anthropic/claude-sonnet-4` | $3/M | Highest quality |

No API key? Set `LLM_PROVIDER=claude-code` to use your local Claude Code CLI instead (uses your existing subscription, no extra cost).

## What it finds

Real output from 19 sessions over 8 days:

```
(0.85, 4 sessions) [correction_pattern]  Always use create_ch_client(), never get_ch_client()
(0.85, 3 sessions) [correction_pattern]  Build-time injection over runtime fetches for slow-moving config
(0.80, 3 sessions) [workflow_habit]       Creates versioned design docs before implementation
(0.70, 2 sessions) [correction_pattern]   Inline loaders where content changes, never page-level spinners
```

The bar: would a fresh AI session get this wrong without this info? If yes, it's a real pattern. If the agent would naturally do the right thing, it's noise.

## Trace providers

Currently uses [Staso](https://staso.ai). Swap `staso.py` for other providers.

## Inspiration

- [Hermes Agent](https://hermes-agent.nousresearch.com) -- in-session self-learning with skills, memory, and cross-session recall via SQLite FTS5
- [OpenClaw](https://github.com/openclaw/openclaw) -- personal AI assistant with persistent memory and skill acquisition
- Karpathy's [autoresearch](https://github.com/karpathy/autoresearch) -- modify, test, keep/discard loop with git as memory

## License

MIT
