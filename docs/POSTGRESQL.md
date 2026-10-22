# Public Web PostgreSQL deployment

Local SymNote defaults to SQLite and stays usable without a hosted database. The public Web variant explicitly selects `SYMNOTE_STORAGE_BACKEND=postgres` and requires `DATABASE_URL`. A missing or invalid PostgreSQL configuration fails closed; it never silently writes to local SQLite. The public server requires Google OAuth and refuses E2E login.

## Initial architecture and limits

Start with one Web container and a managed PostgreSQL database in the same region. `render.postgres.yaml` is a proposed blueprint, not evidence of a deployed service. It creates paid resources only when applied. Configure secret values in the hosting provider, never commit `.env`. Set `SYMNOTE_ALLOWED_HOSTS` to the actual assigned hostname and `app.symnote.app` before launch; the blueprint includes `*.onrender.com` only to allow initial provider-hostname checks. Preserve the Google redirect URI `https://app.symnote.app/api/auth/google/callback`.

Tenant data uses deterministic per-user schemas; accounts, invitations, sessions, pending Google PKCE state, and AI quota reservations are shared in `symnote_system`. Tenant selection comes from the authenticated server-side user. All schemas currently share one database role: application isolation is not PostgreSQL RLS or a separate security role per tenant. Advisory transaction locks preserve quota and tenant write consistency across connections.

This is an initial small-service architecture. Many tenant schemas eventually add catalog and migration overhead. Measure latency, database CPU/connections, error rate, and AI spending before increasing instances. Horizontal deployment also requires reviewing every process-local feature and moving slow jobs out of request handlers. Remote MCP lifecycle and macOS Calendar/Gmail keyring integration are not made cloud-ready by this change. Do not share an owner's external credentials with other users. A later shared-table tenant model or another PostgreSQL host remains possible; no Kubernetes or microservices are needed initially.

## Local integration environment

Use `compose.postgres.yaml` with a dedicated environment file and `SYMNOTE_POSTGRES_PASSWORD`. The password is passed separately through `PGPASSWORD`, so URI-special characters are preserved. PostgreSQL has no published host port in this compose file. Its named volume is persistent; do not run volume deletion commands against user data.

```sh
SYMNOTE_ENV_FILE=/absolute/path/to/private-web.env docker compose -f compose.postgres.yaml up --build -d
```

The private environment file needs owner email, Google client ID/secret, public origin, allowed hosts, and optionally AI configuration. Keep local SQLite configuration in its existing environment. This command is preparation for a local test; it does not provision cloud hosting.

## Migration and return to SQLite

Stop writes during final cutover. Preserve an independent backup of the original SQLite file. Use the same authenticated user ID as the intended destination (the configured owner uses `owner`). The source must have schema version 16, pass integrity checks, and contain only supported tables. Import refuses a nonempty destination and preserves IDs, notes, tasks, relations, embeddings, and Gmail processed markers. Local OAuth credentials/tokens and per-tenant quota history are deliberately excluded. Google sessions/accounts are not imported from the local system database; configure/invite accounts explicitly.

```sh
python -m symnote.postgres_cli inspect /absolute/path/to/source.db
python -m symnote.postgres_cli import /absolute/path/to/source.db --user-id owner
# After checking the dry run, with backend=postgres and DATABASE_URL configured:
python -m symnote.postgres_cli import /absolute/path/to/source.db --user-id owner --apply
python -m symnote.postgres_cli export /absolute/path/to/new-export.db --user-id owner
```

Import reads the source SQLite database without modifying it and runs the destination inserts in a transaction. Export takes a consistent PostgreSQL snapshot, builds a fresh private SQLite file, verifies foreign keys/integrity, and refuses to overwrite an existing path. An exported file is suitable for core local workflows; it is not a full backup of cloud accounts/sessions or provider credentials.

Managed database backups must cover all tenant schemas and `symnote_system`; configure provider retention and test restoring into a separate database before relying on them. A tenant export supplements that backup. Preserve `.env` and secrets through a separate secure process. Keep the original SQLite database until cutover, reconciliation, and rollback checks are complete.

## Launch gates and cost

Before launch: review paid plans and data transfer; provision a managed database; securely configure secrets; import the approved data; verify invite restrictions and two-user isolation; test Google login on the public URL; test AI through the configured provider; change DNS only after health/login checks; test on a physical phone with the Mac off. Local API tests and container builds do not prove these live checks.

The proposed smallest paid Render configuration is Web $7/month plus PostgreSQL compute $6/month plus storage (listed at $0.30/GB/month), approximately $13.30/month for 1 GB before tax, AI usage, bandwidth, and other overages. Confirm the checkout estimate before applying. No paid resources or private data transfer are authorized merely by committing this file.

Official references checked 2026-10-21: [pricing](https://render.com/pricing), [Blueprint specification](https://render.com/docs/blueprint-spec), [compute plans](https://render.com/docs/compute-plans).
