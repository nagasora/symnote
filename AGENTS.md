# SymNote development guidance

## Product constraints

- SymNote is a personal, local-first application. User notes, tasks, embeddings, and generated content must remain usable without a hosted database.
- Treat the configured SQLite database and `.env` as user-owned state. Never overwrite, delete, reset, or commit them.
- Prefer additive, versioned database migrations and backward-compatible configuration changes.
- AI-assisted features should degrade gracefully when the network or API key is unavailable; core note and task workflows must remain functional.

## Development workflow

- Use Python 3.12 and the repository `.venv` when available.
- Install runtime dependencies with `python -m pip install -r requirements.txt` and development tools with `python -m pip install -e ".[dev]"` once packaging is configured.
- Run tests with `python -m pytest`.
- Keep UI wiring in `src/symnote/app.py` or focused view modules; keep persistence and domain behavior under `src/symnote/core/`.
- Add or update tests for database migrations and all non-trivial core behavior.

<!-- SORA-AGENT-TREE:BEGIN -->
## Cost-efficient agent tree

- Root/orchestrator: `gpt-6-astra`, reasoning `medium`. Own scope, acceptance criteria, integration, verification, and the final response.
- Delegate on demand: `explorer` = `gpt-5.6-luna` / `max` for bounded repository investigation; `worker` = `gpt-5.6-sol` / `high` for implementation and focused tests; `researcher` = `gpt-5.6-luna` / `max` for focused source lookup.
- Only when needed: `reviewer` = `gpt-6-astra` / `xhigh` for independent review after integration, when a concrete correctness/security/concurrency risk or explicit user request justifies it. Self-review is not independent review.
- Split useful work; do not spawn every role. Handle small tasks locally. Delegate only a concrete bounded subtask that can run alongside useful parent work. Keep at most three children active; children must not delegate further.
- Each handoff specifies objective, owned files/read-only scope, acceptance criteria, and required evidence. Never overlap writers or repeat the same investigation. Pass distilled context, not the entire history; when using explicit model overrides, use a fresh or limited-history fork.
- Return findings or changed files, validation evidence, and unresolved risks. Root integrates and verifies proportionately. If a requested model is unavailable, report that limitation rather than silently substituting it.
<!-- SORA-AGENT-TREE:END -->
