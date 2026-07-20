# Optional always-on snippet

This skill loads only when cost or auditing comes up. If you want a few of its
defaults active in **every** session, copy the block below into the instruction
file your agent always loads (`CLAUDE.md` for Claude Code, `AGENTS.md` for Codex
and others).

This is optional. The always-on file is itself re-read on every turn, so keep it
this short and do not paste the whole skill in there.

```markdown
## Token discipline (lars-nehrig-compressor)

- Most cost is usually context re-processed every turn, not reply length. Retrieve
  narrowly (search limits, offsets, summaries first); prefer reading pages as text
  when content is the question; act on element references instead of pixel
  coordinates.
- Batch independent tool calls into one message. Don't re-fetch what is already in
  context unless it may have changed.
- Ask clarifying questions once, bundled, with a recommended default — and only
  when the answer changes the work.
- Never trade verification, sufficient reading before edits, or the deliverable
  itself for token savings. Never guess instead of looking: a redo or a wrong fact
  costs more than any saving.
```
