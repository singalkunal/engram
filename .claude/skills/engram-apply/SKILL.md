---
name: engram-apply
description: Apply engram-generated learnings (skills + memories) into the user's local Claude Code layout. Use when the user says /engram-apply, "apply engram candidates", "process engram review", or points at a candidates.md or candidates.json file produced by engram.
---

# Engram Apply

You are integrating a batch of LLM-generated candidate skills/memories from `engram` into the user's actual local Claude Code layout. Engram only generates candidates — placement, integration, and index updates are YOUR job because you have full local context.

## What you receive

A path to either:
- `runs/<timestamp>/candidates.md` — the human-readable review
- `runs/<timestamp>/candidates.json` — machine-readable form (use this for parsing)

If only the `.md` is provided, derive the `.json` sibling automatically (same dir, same basename).

## Step-by-step procedure

### 1. Understand the user's local layout (mandatory before any writes)

Read these to know where things go and what already exists:

- `CLAUDE.md` — top-level routing rules and conventions
- `.claude/skills/` (or whichever `SKILLS_DIR` is configured) — skill folder structure. Identify the subdirectories the user has chosen and any parent index `SKILL.md` files (the ones at the root of a category — they list child skills in a table)
- The memory directory (`~/.claude/projects/-<cwd-slug>/memory/MEMORY.md` or as configured via `MEMORY_DIR`)

You need this context to:
- Pick the right subdirectory for each new skill
- Update parent index files when adding skills
- Update `MEMORY.md` index when adding memories
- Avoid overwriting category index files (they typically sit at the root of a subdirectory, often have `MUST_LOAD` or similar markers in their description, and contain a table listing child skills — they are NOT regular skills)

### 2. Load candidates

Parse `candidates.json`. Each candidate has:
- `kind`/structure indicating skill or memory
- `action`: `create` or `update`
- `name`, `description`, `content` (full file body), `evidence`, `confidence`, `session_count`
- For skills: `path` (engram's GUESS at directory)
- For memories: `filename` (engram's GUESS at filename), `type`

**Treat `path` and `filename` as suggestions, not commands.** You decide the actual placement based on the local layout.

### 3. Present to the user (chat summary)

For each candidate, in order skills → memories → insights:
- One-line summary: `[CONFIDENCE] action — name (sessions: N)` 
- Show evidence in 1-2 lines
- Mark anything that looks suspect (overlaps with existing skill/memory, project-specific despite cross-session evidence, weird filename)

Then ask the user: which to apply? Options:
- `all` — apply everything
- Comma-separated indices: `1,3,5`
- `none` / `skip` — abort
- Inline edits: user can say "apply 2 but rename to X" or "apply 3 as a memory not a skill"

### 4. Apply each approved candidate

For each one:

**SKILL:**
- Decide the right subdirectory based on the principle's scope (NOT engram's suggested path):
  - Broadly applicable → the user's broadest/shared subdirectory
  - Domain-specific → the matching narrower subdirectory
  - Match the existing organization in `SKILLS_DIR`. If the user has a flat layout, use the root.
- Pick a slug-style directory name (kebab-case, descriptive, no project-specific feature names if the principle is general)
- Write `<subdir>/<slug>/SKILL.md` with the candidate's `content`
- **Update the parent index file** if one exists for that subdir (e.g., a parent `SKILL.md` with an "Available Skills" table — add a new row for the new skill)
- **Update CLAUDE.md routing** if the skill needs auto-loading rules (only if the existing CLAUDE.md has similar routing entries — match the pattern)

**MEMORY:**
- Pick a `snake_case_filename.md` matching the user's existing naming convention (look at `MEMORY.md` for the pattern — typically `feedback_*`, `project_*`, `user_*`, `reference_*`)
- Write to `<MEMORY_DIR>/<filename>.md` with the candidate's `content`
- **Append to `MEMORY.md`**: a new line `- [Name](filename.md) — one-line description`. Match the existing format exactly.
- For `update` actions: read the existing memory, merge intelligently (preserve existing `Why:`/`How to apply:` lines if present, broaden where the new evidence justifies, keep concise)

### 5. Skip rules (don't apply)

- A "skill" candidate with `path` matching a category index location (e.g., a top-level subdirectory with no nested slug) — **do not apply**. The LLM was confused. Suggest as a memory or skip entirely.
- A skill whose name/principle is clearly project-specific despite the gatekeeper letting it through — surface to the user, default to skip.
- A duplicate of an existing skill/memory — propose UPDATE to that file instead of creating a new one.

### 6. Report what changed

After applying, summarize:
- Files created (with paths)
- Files updated (with what changed in 1 line each)
- Index files touched (parent `SKILL.md` files, `MEMORY.md`, `CLAUDE.md`)
- What was skipped and why

### 7. Optionally commit

Ask user: "commit these changes?" If yes:
- Stage only the files you modified
- Commit with a message like: `engram: apply N learnings from <run-name>`
- Keep author info from the user's git config

## Important conventions

- Match the user's existing file naming and frontmatter style EXACTLY. Don't introduce new patterns.
- Memory bodies stay concise (under ~100 words ideally). If a candidate body is longer, summarize during apply.
- Skill bodies are full guidelines (100+ words). Keep examples and structure from the candidate content.
- NEVER overwrite a parent category SKILL.md (the index file). Always add to its table, never replace it.
- If the user has `update-skill` or similar conventions defined in their CLAUDE.md, respect them.

## Failure modes to avoid (learned from previous engram bugs)

- Writing to a parent category `SKILL.md` thinking it's a skill — it's the index. Always check the file's role first.
- Using a candidate's `path` literally when it points to a top-level subdirectory only — that's a directory, not a skill location. Pick a nested slug.
- Sanitize filenames: no `/`, no spaces, no `..`. If `filename` field has these, fix it.
- YAML frontmatter must have `---` on its own line. If the candidate's `content` has `---name:` merged, fix it before writing.
- Don't dedupe by filename alone — check by name and description for semantic overlap.
