# AGENTS.md

<!-- contextbaton:start -->
## Handoff notes (contextbaton)

This project keeps short handoff notes and decisions in `.context/`.

- **Start of a session:** call the `get_latest_handoff` MCP tool (or read the newest file in `.context/handoffs/`).
- **Outdated notes** may be wrong: check the code before trusting them, then call `confirm_note` if they're still true.
- **End of a session:** call `write_handoff` to leave a note for whoever comes next. Use `record_decision` for important decisions.
- Never put secrets in notes. contextbaton redacts them, but don't rely on that.
<!-- contextbaton:end -->
