---
name: lars-nehrig-compressor
description: "Analyzes and reduces agent token spend where it accrues: context size and turn count. Bundles a transcript audit script for Claude Code (deduplicated billed usage, separate text/image estimates, subagent coverage) plus levers for narrow retrieval, turn batching, and cache-friendly prompts, with guardrails so savings never cause rework or guessing. Use when the user explicitly asks about token cost, API spend, context-window usage, why sessions are expensive, or wants an audit of where tokens go. Not for performance tuning of application code, and not needed for routine coding or browsing."
license: MIT
metadata:
  author: Lars Nehrig
  version: "1.1.6"
---

# lars-nehrig-compressor

One goal: **minimize the expected total spend until a verifiably correct result** —
not the size of any single reply or tool call. Most of a session's cost is usually
the context that gets re-processed turn after turn, not the text the model writes.
But workflows differ, so this skill starts with measurement, not belief.

## Two cost heuristics

1. **Context exposure.** A tool result does not cost its face value once. It can be
   re-processed on later turns until the session ends or the client compacts it, so
   its real cost scales with how long it stays alive. Caching softens this (cache
   reads are ~10% of the input rate) but does not remove it — and cache *writes*
   cost more than reads by an order of magnitude.
2. **Turn overhead.** Each turn re-pays for the accumulated context (cheaply when
   cached, expensively when not). Removing an unnecessary turn saves the whole
   context once; two independent tool calls sent in one message save a full turn.

These are heuristics, not laws: caching, compaction, model choice, and subagent
isolation all shift the numbers. Which is why:

## Measure before you optimize

For Claude Code, run the bundled script (local files only, nothing leaves the
machine):

```
python "${CLAUDE_SKILL_DIR}/scripts/audit.py"           # current project
python "${CLAUDE_SKILL_DIR}/scripts/audit.py" --last   # most recent session
python "${CLAUDE_SKILL_DIR}/scripts/audit.py" --json   # machine-readable
```

(`${CLAUDE_SKILL_DIR}` points at this skill's folder in Claude Code; outside
of it, use the path to `scripts/audit.py` directly. The transcript root honors
`CLAUDE_CONFIG_DIR`.)

It separates **authoritative usage** (billed tokens from the transcripts,
deduplicated, including subagents) from the **estimated one-time tool payload**
(how much each tool wrote into context, once — text approximated as chars/4,
images via the official resize formula, never by base64 length). Attack
whatever the authoritative block says is large; use the payload table only for
targeting — it measures size written, not how long it stayed alive in context.
For other agents, apply the same idea to whatever usage logs they expose.

## Lever 1 — Keep context deliberate

- **Retrieve narrowly.** Search with match limits and filename-only modes before
  reading files; read with offset/limit when the location is known; summarize long
  command output (`--stat`, `--oneline`, `head`, `wc -l`) before requesting all of
  it. Receive the relevant lines, not the surrounding thousand.
- **Pick the medium by the question.** Text or an accessibility tree answers "what
  does it say / did it work"; an image answers "how does it look". Reading a page
  as structured text is also *more reliable* than interpreting a bitmap.
- **Act on element references, not pixel coordinates.** Coordinate clicking needs a
  screenshot to find the pixel and another to confirm the result — a loop of extra
  turns and images. One page read yields stable references you can act on without
  any image.
- **Don't re-fetch what is already in context** — unless it may have changed since
  you fetched it.

## Lever 2 — Finish in fewer turns

- **Batch independent tool calls into one message.** Serialize only when a later
  call genuinely needs an earlier result. Never add a call just to batch something.
- **Turn tasks into verifiable goals.** "Make it work" invites a blind retry
  loop, each iteration a full-context turn. Pin the goal to evidence instead —
  a failing check that captures the problem and must turn green — so the loop
  terminates on proof, not on vibes.
- **Ask once, bundled, with a recommended default** — and only when the answer
  changes what you will do. Five drip-fed questions cost five turns. If the answer
  would not change the work, pick the sensible default, state it, and continue.

## Lever 3 — Be cache-friendly

Prompt caches match on the exact prefix of the request; changes early in the
prompt invalidate everything cached after that point. In agent clients that manage
caching for you, the actionable part is simply:

- Keep always-loaded instruction files short and stable. Avoid unnecessary
  mid-session churn in them — but a needed correction beats cache preservation.
- Start a fresh session for an unrelated task instead of dragging dead context.
- Disable tool servers you don't use when their presence adds measurable
  overhead; some clients (e.g. Claude Code with tool search) defer full tool
  schemas until needed, which makes idle servers cheap.
- Cache pricing and lifetime vary by provider and plan; measure, don't assume.

## Output length — the honest priority

Don't start with reply length; measure first (output is often a single-digit share
of spend). But don't pad either: output tokens carry the highest per-token price
and become context themselves. Cut ceremony and repetition — never necessary
detail, evidence, or readability.

## Guardrails — never traded for tokens

A saving that causes rework is a loss: a redo burns many full-context turns.

- **Never skip verification** to save tokens.
- **Read enough before editing.** An under-informed edit is a loan at high
  interest.
- **Never guess instead of looking.** These levers remove redundant and
  low-information context; they never remove information acquisition. If a rule
  here seems to conflict with getting the facts, the facts win.
- **Don't ration the deliverable.** If the document, visual, or design is the
  product, produce it properly.
- **Honor explicit requests.** A requested screenshot, full read, or subagent is
  executed, not optimized away. This skill shapes defaults; it does not override
  instructions.

## Subagents: saving or waste

A subagent has its own context window — and its own bill, which the audit reports
separately. Use one to keep a large, noisy retrieval out of the main context so
only the conclusion returns. Don't use one for work that fits inline: every spawn
re-derives context from cold.

## Optional: an always-on layer

This skill loads only when cost or auditing comes up. If you want two or three of
its defaults active in every session, [CLAUDE-SNIPPET.md](CLAUDE-SNIPPET.md) has a
short, data-free block for your agent's always-loaded instruction file. Optional —
the always-on file is itself paid context on every turn, so keep it minimal and
don't duplicate this skill into it.

## Agent-specific notes

Claude Code tool mappings (search limits, browser references, batching,
transcript locations): see [references/claude-code.md](references/claude-code.md).
