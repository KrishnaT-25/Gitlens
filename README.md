# GitLens Phase 1

An in-page assistant for public GitHub repositories. The Chrome extension is React/JavaScript; the API and ingestion worker are Python/FastAPI; PostgreSQL stores commit-specific snapshots and full-text indexes. The optional hosted demo uses Render Free for the API and in-process worker, plus Neon Free for PostgreSQL; no payment method is required by this configuration.

## Local setup

1. Start PostgreSQL and create a database named `gitlens`.
2. Copy `server/.env.example` to `server/.env`; set `DATABASE_URL` and `SESSION_PEPPER`. Register a GitHub OAuth app with callback `https://copoojjfbgbgcnaokahlbopnafkljfkf.chromiumapp.org/github`, then set `GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET` in the server file. Keep the client secret out of the extension. `EMAIL_PROVIDER=console` remains available for backend-only local tests.
3. In `server/`, run `python -m venv .venv`, activate it, run `pip install -r requirements-dev.txt`, and `alembic upgrade head`.
4. Start the API with `python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001` and the worker with `python -m app.worker` in separate terminals.
5. In `extension/`, run `npm ci`, copy `.env.example` to `.env`, then `npm run build`. Load `extension/dist` at `chrome://extensions` using **Load unpacked**.
6. Visit a public GitHub repository and use **Continue with GitHub**. The OAuth app requests no scopes and GitLens stores its own session token; it does not retain the GitHub access token.

Run backend tests with `python -m pytest` and extension tests with `npm test`. No-card hosted setup is in [`DEPLOY.md`](DEPLOY.md); operating limits are in `docs/operations.md`.
