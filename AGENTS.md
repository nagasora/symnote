# SymNote development guidance

## Product constraints

- SymNote is a personal, local-first application. User notes, tasks, embeddings, and generated content must remain usable without a hosted database.
- Treat the configured SQLite database and `.env` as user-owned state. Never overwrite, delete, reset, or commit them.
- Prefer additive, versioned database migrations and backward-compatible configuration changes.
- AI-assisted features should degrade gracefully when the network or API key is unavailable; core note and task workflows must remain functional.

## Multi-agent workflow

- For work with two or more independent investigation or implementation tracks, delegate bounded tracks in parallel and wait for them before consolidating the result.
- Prefer `architecture_auditor` for codebase/design exploration, `persistence_auditor` for local data durability, and `quality_checker` for tests and verification.
- Agents share one checkout. Assign overlapping file edits to only one agent, and keep audit agents read-only.
- The main agent owns integration decisions, final verification, and the user-facing summary.

## Development workflow

- Use Python 3.12 and the repository `.venv` when available.
- Install runtime dependencies with `python -m pip install -r requirements.txt` and development tools with `python -m pip install -e ".[dev]"` once packaging is configured.
- Run tests with `python -m pytest`.
- Keep UI wiring in `src/symnote/app.py` or focused view modules; keep persistence and domain behavior under `src/symnote/core/`.
- Add or update tests for database migrations and all non-trivial core behavior.

