# lars-nehrig-compressor

**Spend tokens on the work, not the loop.**

[![Agent Skills](https://img.shields.io/badge/Agent%20Skills-compatible-4f46e5)](https://agentskills.io)

**By [Lars Nehrig](https://www.linkedin.com/in/ki-beratung/)** — AI Engineer, KI-Beratung ohne Hype.
Instagram: [@cosmetic.creator](https://www.instagram.com/cosmetic.creator/) · LinkedIn: [ki-beratung](https://www.linkedin.com/in/ki-beratung/)

An [Agent Skill](https://agentskills.io/) that cuts agent token spend where it
actually accrues — **context size and turn count** — and grounds it in a
measurement script instead of folklore. The behavioral playbook is portable
across Agent-Skills-compatible systems (Claude Code, Codex CLI, Cursor, VS Code,
and others); the bundled transcript auditor currently reads **Claude Code**
transcripts only. This skill audits and recommends — it does not automatically
rewrite or compact your sessions.

## Why this exists

Token advice is usually "write shorter answers". My own measured sessions
(Claude Code transcripts, deduplicated by message ID, snapshot 2026-07-17) said
otherwise: **roughly 90% of price-weighted usage was context being written to and
re-read from cache; reply text was around 9%.** Your split will differ — which is
exactly why the skill ships its own audit instead of hardcoding my numbers.

Two heuristics follow:

1. **Context exposure** — a tool result can be re-processed on every later turn,
   so its real cost scales with how long it stays alive in the session.
2. **Turn overhead** — every turn re-pays the accumulated context; batching two
   independent tool calls into one message saves an entire turn.

Plus guardrails, because a "saving" that causes rework is a loss: verification,
reading before editing, and the actual deliverable are never traded for tokens.

## What's inside

```
skills/lars-nehrig-compressor/
├── SKILL.md               # the skill: 2 cost heuristics, 3 levers, guardrails
├── CLAUDE-SNIPPET.md      # optional always-on lines for CLAUDE.md / AGENTS.md
├── references/
│   └── claude-code.md     # concrete Claude Code tool mappings
├── scripts/
│   └── audit.py           # measure YOUR cost drivers from local transcripts
├── tests/
│   └── test_audit.py      # deterministic test suite (run: python tests/test_audit.py)
└── evals/
    ├── evals.json         # trigger and behavior test cases (run them yourself)
    └── fixtures/          # files the behavior evals need
```

## Measure first

```bash
python skills/lars-nehrig-compressor/scripts/audit.py          # current project
python skills/lars-nehrig-compressor/scripts/audit.py --last   # latest session
python skills/lars-nehrig-compressor/scripts/audit.py --json   # machine-readable
```

The script reads your local Claude Code transcripts (nothing leaves your machine)
and strictly separates:

- **Authoritative usage** — billed tokens from the API usage fields, deduplicated,
  including subagent transcripts, with a dollar estimate for known models and an
  explicit coverage percentage.
- **Estimated one-time tool payload** — how much each tool wrote into context,
  once. Text is approximated as chars/4; images use Anthropic's official resize
  reference and the 28×28 patch formula (per-request vision tier), never base64
  length — base64 counting overstates screenshots roughly 20-fold. This is
  payload size for targeting, not lifetime context exposure.
- **Browser action signals** — screenshots requested, coordinate vs. ref-based
  actions from browser tool inputs. Many coordinate actions alongside many
  screenshots usually indicate a confirm-by-screenshot pattern; the script does
  not reconstruct the exact loop sequence.

## Install

**Claude Code (personal skill):**

```bash
git clone https://github.com/ki-beratung-nehrig/lars-nehrig-compressor.git
cp -r lars-nehrig-compressor/skills/lars-nehrig-compressor ~/.claude/skills/
```

**Claude Code (plugin):**

```
/plugin marketplace add ki-beratung-nehrig/lars-nehrig-compressor
/plugin install lars-nehrig-compressor@lars-nehrig-compressor
```

**Claude app / Cowork:** zip the folder `skills/lars-nehrig-compressor/` (the
skill folder itself as the ZIP root) and upload it under **Settings → Customize →
Skills**. Note: the audit script reads local Claude Code transcripts and won't
find them in those environments — the rules and guardrails still apply.

**Codex CLI / other Agent-Skills clients:** copy `skills/lars-nehrig-compressor/`
into the tool's skills directory; the folder follows the
[Agent Skills specification](https://agentskills.io/specification).

**Optional always-on layer:** copy the short block from
[CLAUDE-SNIPPET.md](skills/lars-nehrig-compressor/CLAUDE-SNIPPET.md) into your
`CLAUDE.md` or `AGENTS.md`.

## Verify it works

Run the audit before and after adopting the skill and compare:

- context p90 per request (should fall)
- screenshots requested and coordinate-vs-ref actions (screenshot loop should
  shrink)
- share of context vs. output in the authoritative block

Not the number to watch: output tokens alone — measure first; that line is often
a single-digit share.

## How it was built

Developed and hardened through six audited release rounds: every measurement
claim in this README is backed by the shipped test suite (67 deterministic
checks) and adversarial cross-review between two independent AI systems
against real session data.

## License

MIT — see [LICENSE](LICENSE).
