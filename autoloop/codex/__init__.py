"""Codex reviewer providers.

Two transports, both selectable, neither replacing the other:

* `conversation.py` + `quota.py` — `codex_cli`, one `codex exec` process per
  turn. Stateless between turns, so it cannot chunk.
* `app_server.py` + `app_server_conversation.py` + `wire.py` +
  `protocol_errors.py` — `codex_app_server`, one `codex app-server` process
  holding one thread. It can chunk, and classifies failures from protocol
  fields rather than from stderr text.
"""
