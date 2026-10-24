# Operations and release checklist

## Local development

Use PostgreSQL 16 or newer, Python 3.12 or newer, Node.js 20 or newer, and Chrome. Follow the commands in the root README. `server/.env` and `extension/.env` are local, ignored files. Set a random `SESSION_PEPPER`; never put it or provider keys in the extension. Set `GEMINI_API_KEY` locally to exercise real summaries and chat. A GitHub token is optional for local public repository testing, but API rate limits are much lower without one.

For Chrome sign-in, register a GitHub OAuth App under GitHub Settings → Developer settings → OAuth Apps. Use `http://127.0.0.1:8001` as its local homepage and `https://copoojjfbgbgcnaokahlbopnafkljfkf.chromiumapp.org/github` as its exact authorization callback. Leave Device Flow disabled. Add its client ID and client secret to `server/.env` as `GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET`. Keep the secret out of Git and the extension. The extension requests no OAuth scopes; the server reads GitHub's public user ID, discards the GitHub access token, and creates a GitLens session. `EMAIL_PROVIDER=console` remains for backend-only local tests.

The API starts on port 8001. The worker is a separate process and must run alongside it. Run `alembic upgrade head` before either starts. `GET /healthz` checks the process; `GET /readyz` checks PostgreSQL.

## No-card hosted demo

See [`DEPLOY.md`](../DEPLOY.md) for the exact Render + Neon steps and environment variables. Keep every service on a clearly labeled Free plan; do not add a card or create a paid resource. Render's free web service runs the worker in-process because Render does not offer a free separate worker. Jobs remain in PostgreSQL across sleep/restarts; the worker scans on startup, wakes on submissions, uses leases, and recovers expired work. GitHub Actions ingestion is manual-only recovery, with no scheduled duplicate polling.

## Acceptance run

On a public GitHub repository, open GitLens, choose **Continue with GitHub**, authorize the scope-free app, and reopen the panel. Wait for `ready`; confirm that the summary appears and uses the correct SHA. Ask a question whose answer is in a file and open its citation. Refresh GitHub, sign out, and verify that a protected API call requires sign-in. Repeat with two explicit 40-character commits and confirm distinct snapshot IDs and SHA-pinned citations. Test an empty or oversized repository and confirm that the panel reports an unsupported state. Check `extension/dist` for provider keys before sharing it.

## Failure handling and observability

- `GET /internal/metrics` requires the `ADMIN_TOKEN` bearer token and reports counts of job and snapshot states. Keep this token private. Alert on growing queued counts, jobs stuck in running beyond their lease, or repeated failed snapshots. Requests log ID, path, status, and duration; provider keys and source text are not logged.
- Worker leases last 15 minutes. An expired lease can be reclaimed; a third expired attempt is marked failed. In-process mode wakes on submission and sleeps until the next durable retry/lease deadline. `POST /api/snapshots/{id}/retry` allows a user to requeue a failed snapshot within quota. Unsupported snapshots require a new code or limit change, not a retry.
- Before migrations, back up PostgreSQL. Check `alembic check` on the current schema. For a release, run `alembic upgrade head` in staging, run the acceptance flow, then deploy production. A rollback should restore the database backup when a migration loses data; `alembic downgrade` is useful for migration rehearsal, not a substitute for backup.
- Quotas, archive limits, and model selection are in `server/app/config.py`. Start with the defaults and increase only after measuring load. PostgreSQL is the shared source of truth. Render's free deployment has sleep and resource limits; the same API and worker code can move to dedicated services later.

## Current limits

Phase 1 supports only public repositories and source files within configured archive/file/text limits. Search is lexical PostgreSQL full-text search, so vocabulary mismatches can produce an evidence gap. No repository code is executed. Gemini's free tier has provider limits and may use submitted data to improve Google products; review current provider terms before inviting users. Render Free sleeps after 15 minutes of inactivity, so the first request can take around a minute; the extension displays a waking status and retries readiness. Render can suspend a free service for excessive outbound traffic or exhausted free hours. Neon has free quotas. A complete hosted smoke run requires those accounts and credentials in their dashboards.
