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

## Ingestion redaction

Redaction runs before chunks, symbols, summaries, or embeddings are built. Source
files use the available Tree-sitter grammar to identify literal values associated
with sensitive assignment, parameter, and mapping names. It preserves type
annotations, type aliases, calls, references, interpolation syntax, quote
delimiters, operators, and surrounding statements. Without a grammar, a
conservative fallback masks quoted credential assignments only.

Configuration files (`.env` and its variants, JSON/JSONC, YAML, TOML, INI,
CFG/CONF, properties, `.npmrc`, and `.pypirc`) have separate scalar handling,
including unquoted values and YAML blocks. Recognised provider-token formats,
literal URL passwords, and private keys are removed in either mode. This is
pattern-based detection, not a guarantee that every possible secret is detected.

Original LF/CRLF separators, terminal newlines, and source line counts are
preserved. Quoted values retain their delimiters, so citation line numbers still
refer to the original repository. Redaction changes value contents and may change
column offsets. There are no public API or database schema changes.

Existing snapshots contain the earlier stored redaction output. They need a
fresh ingestion to restore damaged code; cached chunks and embeddings cannot
recover the original text. Retrying a job that resumes existing staged chunks
does not itself guarantee re-redaction. Do not send old unredacted content to a
provider when arranging a rebuild.

## Embedding compatibility and repair

Run `python -m alembic upgrade head` before starting the updated web and worker
processes. Migration `0005_embedding_compatibility` adds model-space metadata to
vectors, invalidates legacy semantic readiness, and queues ready snapshots for a
leased repair. Unknown legacy formatting is never assigned a current fingerprint.
Text indexes, summaries, chunk IDs, and citation line numbers remain usable.
Downgrading drops the compatibility columns; upgrading again marks retained
vectors unknown and queues repairs again. Do not run old and new worker versions
concurrently during this schema rollout.

Each new vector stores model, dimensions, formatting version and a SHA-256
fingerprint. The snapshot report stores the completed profile as well. Retrieval
requires both the matching report and matching vector metadata; partial batches,
other snapshots/index versions, and mismatched models/formats are excluded.
Vector distance is computed over a materialized set of compatible rows from that
snapshot, rather than traversing a global HNSW graph containing other spaces.
This is exact vector search bounded by the snapshot's chunk limit (default 5,000).
Changing the model or formatting version triggers one repair on next submission
or status access. Workers delete incompatible vectors and resume already
checkpointed compatible batches. A vector-only repair reuses the cached summary.

Supported models are `gemini-embedding-2` and `gemini-embedding-001`; an optional
`models/` prefix is normalized. Embedding 2 uses `title: … | text: …` documents
and `task: code retrieval | query: …` queries, with one Content request per chunk.
001 uses native `RETRIEVAL_DOCUMENT` / `CODE_RETRIEVAL_QUERY` fields. See
[Google's embedding formats](https://ai.google.dev/gemini-api/docs/embeddings).
The current database vector column requires **768 dimensions**; other dimensions
fail configuration validation and need a separate storage migration. Formatting
version 2 includes path/symbol titles, task prefixes, 8,000-character content and
1,000-character title bounds. Bump that version when any of those rules change.

Limits in `server/.env.example` default to 32 inputs, 1 MiB per request, 4 MiB
per streamed response, and a 120-second overall adapter deadline (40 seconds per
HTTP operation). Both byte limits are configurable within hard upper bounds.
Oversized requests, HTTP 413, and response-cap overflow split batches into halves,
preserving input order; a single oversized item fails safely. Malformed responses
are rejected without splitting. Count, dimensions, numeric type, finite float32
range and nonzero norm are checked, and vectors are normalized before storage.
429 responses use at most two retries with backoff; no paid/provider fallback is
used.

Inspect `GET /api/snapshots/{id}/status` → `search` or `snapshots.index_report` for
`ready` versus keyword fallback and a safe error code. Completed batches survive
quota/provider failures; a later explicitly requeued job resumes them. A failed
semantic stage does not cause repeated automatic quota spending on each poll.
For an operator-approved retry after capacity returns, reset only that snapshot's
done/failed job to `queued`, attempts 0, `next_attempt_at=now()`, and clear its
lease, dispatch marker and error; then wake/run the worker. The public retry
endpoint remains limited to failed snapshots.

`python -m pytest -q tests/test_embeddings.py tests/test_postgres.py` runs mocked
HTTP regressions separately from real PostgreSQL/pgvector integration checks;
set `DATABASE_URL` and `TEST_DATABASE_URL` to a disposable migrated database.
Coverage includes default responses above 256 KB, adaptive splitting, malformed
JSON/vectors, streaming caps/deadlines, checkpoint resume, lease theft,
cross-space retrieval exclusion, and populated migration downgrade/upgrade.
These checks do not measure live Gemini availability, quota or retrieval quality.

## Current limits

Phase 1 supports only public repositories and source files within configured archive/file/text limits. Search is lexical PostgreSQL full-text search, so vocabulary mismatches can produce an evidence gap. No repository code is executed. Gemini's free tier has provider limits and may use submitted data to improve Google products; review current provider terms before inviting users. Render Free sleeps after 15 minutes of inactivity, so the first request can take around a minute; the extension displays a waking status and retries readiness. Render can suspend a free service for excessive outbound traffic or exhausted free hours. Neon has free quotas. A complete hosted smoke run requires those accounts and credentials in their dashboards.
