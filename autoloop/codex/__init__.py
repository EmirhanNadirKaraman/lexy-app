"""Codex reviewer providers.

Two transports, both selectable, neither replacing the other:

* `conversation.py` + `quota.py` — `codex_cli`, one `codex exec` process per
  turn. Stateless between turns, so it cannot chunk. It classifies failures from
  text, so `quota.py` carries the guard that keeps the loop's OWN prompt out of
  the evidence: `codex exec` echoes the whole prompt onto stderr.
* `app_server.py` + `app_server_conversation.py` + `wire.py` +
  `protocol_errors.py` — `codex_app_server`, one `codex app-server` process
  holding one thread. It can chunk, and classifies failures from protocol
  fields rather than from stderr text.
"""
