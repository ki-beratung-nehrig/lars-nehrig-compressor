# Claude Code specifics

Concrete tool mappings for the generic levers in SKILL.md.

## Contents

- Narrow retrieval
- Browser work
- Batching turns
- Subagents
- Instruction-file and tool hygiene
- Where the transcripts live

## Narrow retrieval

- `Grep`: use `head_limit`, `output_mode: "files_with_matches"` for existence
  checks, and `-C` context lines instead of reading whole files afterwards.
- `Read`: pass `offset`/`limit` when you already know the region.
- Shell: `git diff --stat` before `git diff`; `git log --oneline`; pipe long output
  through `head` or `wc -l` first, then fetch only what matters.
- After `Edit`/`Write` succeed, don't re-read the whole file just to confirm the
  write — the tools fail loudly. Do verify proportionally to risk: a targeted
  diff, a test run, a formatter, or re-reading the changed region.

## Browser work

- When the question is content, structure, or success, prefer `read_page` /
  `get_page_text` over `screenshot`: structured text is more reliable to act on
  than a bitmap, and it avoids the confirm-by-screenshot loop.
- `read_page` returns `ref_N` element references — click and fill via `ref`, not
  pixel `coordinate`. Coordinates require a screenshot before (to find the pixel)
  and after (to confirm); each extra image adds both tokens and a turn.
- Focus large pages with `read_page`'s `ref_id`/`depth` parameters instead of
  dumping the full tree.
- Check results via console messages, network requests, or the DOM before
  reaching for a confirmation screenshot.
- Screenshots are right when the visual itself is the question (layout, styling,
  proof for the user) — take them deliberately, not as a reflex after every
  action.
- Only start browser integration (e.g. Chrome via `--chrome`) when the task needs
  it; an attached browser adds context on its own.

## Batching turns

- Send independent tool calls in a single message; they execute in parallel and
  save one full turn each. Never add calls you don't need just to batch.
- MCP tools: use the exact tool names your session exposes (Claude Code names
  them `mcp__<server>__<tool>`; check `/mcp` when unsure) so lookups don't fail
  and retry.
- Claude Code's tool search loads full MCP schemas on demand by default, so
  idle servers cost little. When a discovery step is involved, resolve the
  tools you expect to need together rather than one at a time.

## Subagents

- Use the `Agent` tool (e.g. a read-only explore type) for broad, noisy searches
  across many files so only the conclusion enters the main context.
- Subagents bill in their own context windows — the audit script reports them
  separately. Don't spawn one for work that fits inline.

## Instruction-file and tool hygiene

- Keep `CLAUDE.md` lean; it is paid context in every session. Facts derivable
  from the code don't belong there.
- Scope directory-specific rules to per-directory instruction files so they only
  load where they apply.
- Disable unused MCP servers when their names/instructions add measurable
  overhead. With tool search (the current default), full schemas are deferred
  until needed, so the cost of idle servers is usually small.
- Avoid unnecessary mid-session edits to `CLAUDE.md` or settings — early-prompt
  changes invalidate the cache for everything after them. A correction the work
  needs still beats cache preservation.

## Where the transcripts live

`scripts/audit.py` reads Claude Code's local session transcripts:

```
~/.claude/projects/<project-slug>/*.jsonl
~/.claude/projects/<project-slug>/<session>/subagents/*.jsonl
```

The slug is the working directory with separators and the drive colon replaced
by dashes. One API request can span several JSONL lines (one per content block);
the script deduplicates by message ID and tool-result ID — naive line counting
roughly doubles every figure. Images inside tool results are estimated by pixel
patches, never by base64 length, which would overestimate them ~20-fold.
