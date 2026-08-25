# AGENTS.md

**The guide for this repository is [`CLAUDE.md`](./CLAUDE.md). Read that.**

It applies to every coding agent working here regardless of vendor — Claude,
Codex, or anything else — and to humans. This file exists only because some
tools look for `AGENTS.md` by convention; it is a pointer, not a second guide.

## Why a pointer and not a copy

There was briefly a real copy here: 357 lines, then the same length as `CLAUDE.md`,
278 of them identical to it, produced by find-replacing "Claude" with "Codex".
Two problems made it worse than useless.

**It stated things that were not true.** The substitution was mechanical, so it
claimed the app "uses Codex Haiku for guided chat" and labelled `llm_service`
as "Codex Haiku". No such model exists. The app calls **Claude Haiku**
(`claude-haiku-4-5-20251001`) through `services/llm_provider.py`, selected by
`LLM_PROVIDER`. An agent instruction file is prompt content: a false claim in
it is not a typo, it is something an agent acts on.

**Two guides drift.** `CLAUDE.md` §8b pins non-obvious invariants of the
progression state machine, §10 lists traps where the obvious fix is wrong, and
§12 carries the hard rules (never read the real `.env`; no AI attribution in
commit messages). A second copy means two agents holding different beliefs
about the same invariants, diverging silently, surfacing only as an agent doing
the wrong thing. This repository has already paid that bill once — see
`CLAUDE.md` §2 on the `src/app/` duplication, where 537 tests exercised code
nothing ran.

One guide, one source of truth. If you need to record something tool-specific,
add it to `CLAUDE.md` and say which tool it applies to.
