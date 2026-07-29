"""Audit executor: orchestrates read-only Claude Code subagents over separable
audit domains, reconciles their structured findings, writes one dated Markdown
report, and proposes a dependency-aware task graph for ChatGPT review.

Modules: `findings` (strict agent-output contract), `agents` (claude-CLI
invocation, read-only tool set), `reconcile` (dedupe/classify/reject),
`taskgen` (findings → proposed tasks), `markdown` (Markdown-only write gate),
`report` (deterministic report rendering), `executor` (the TaskExecutor)."""
