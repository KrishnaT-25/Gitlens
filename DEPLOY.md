# Free, no-card deployment

This guide deploys the API to Render Free, PostgreSQL to Neon Free, and the extension as a manually loaded Chrome extension. Do not enter billing information or select paid plans. Free tiers can sleep, throttle, or change; use this for a demo, not an uptime guarantee. Render's free Postgres is intentionally not used because it expires.

## 1. Prepare the source

1. Put the project in a **public GitHub repository**. Do not commit `server/.env`, `extension/.env`, keys, or tokens.
2. Ensure the GitHub OAuth app's callback is exactly `https://copoojjfbgbgcnaokahlbopnafkljfkf.chromiumapp.org/github`.
3. Note the OAuth client ID and client secret, Gemini API key, and create a GitHub token that can read public repositories. Add secrets only in provider dashboards, never in code or this chat.

## 2. Create free PostgreSQL

1. Create a Neon account/project using its Free option. Stop if the flow asks for a card or paid plan.
2. Copy the **direct** TLS connection string. Keep it private; it is the value for Render `DATABASE_URL`.
3. In Neon, use the SQL editor to confirm the project is active. Render runs Alembic migrations on service start.

## 3. Create the Render API

1. Create a Render account without adding a payment method. Choose **New → Blueprint** and connect the public repository containing `render.yaml`.
2. Confirm the proposed resource is only the `gitlens-api` **Free Web Service**. Do not add a worker or database. Apply the blueprint.
3. Fill the prompted values in the Render service environment page:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | Neon TLS connection string |
   | `GITHUB_OAUTH_CLIENT_ID` | Existing OAuth app client ID |
   | `GITHUB_OAUTH_CLIENT_SECRET` | OAuth app secret |
   | `GEMINI_API_KEY` | Gemini key |
   | `GITHUB_TOKEN` | Token with public repository read access |

   Render generates `SESSION_PEPPER` and `ADMIN_TOKEN`; leave those generated values in place. `WORKER_MODE=inprocess` is set by the blueprint. It requires no Actions secrets or paid worker. Email is disabled for hosted sign-in because this build uses GitHub OAuth.
4. Deploy. Check `https://<service>.onrender.com/health` returns `{"ok":true}` and `/readyz` returns the same after Neon is reachable. The first request after inactivity can be slow while Render and Neon wake.

## 4. Build and load the extension

1. In PowerShell, from `extension`:

   ```powershell
   $env:API_BASE_URL = 'https://<service>.onrender.com'
   npm ci
   npm test
   npm run build
   ```

2. Open `chrome://extensions`, enable Developer mode, select **Load unpacked**, and choose `extension/dist`.
3. Confirm its ID is `copoojjfbgbgcnaokahlbopnafkljfkf`. This must match the OAuth callback and Render `EXTENSION_ORIGIN`.
4. Visit a public GitHub repository, sign in with GitHub, wait for analysis, ask a question, and open a citation. Reload the extension after each new build.

## Environment variables

The Blueprint supplies static values and generated secrets. Set only the prompted secrets above. Do not add `WORKER_DISPATCH_*`; those variables are only used if explicitly switching to the optional Actions worker mode. `DATABASE_URL`, Gemini, GitHub, OAuth, and generated session secrets belong only in Render's server environment.

## Troubleshooting

- **First request is slow:** Render Free sleeps after inactivity. Keep the panel open; it displays a waking status and retries readiness. Do not repeatedly reload while it wakes.
- **`/health` works but `/readyz` fails:** Check the Neon project is active and `DATABASE_URL` is the complete TLS string. Inspect Render logs for migration or connection errors; never paste the connection string into an issue.
- **Sign-in callback mismatch:** Verify the OAuth callback, extension ID, and `GITHUB_OAUTH_REDIRECT_URI` match exactly.
- **Extension CORS/network error:** Rebuild with the exact HTTPS Render host (no extra path or trailing slash), reload unpacked extension, and confirm `extension/dist/manifest.json` grants that host.
- **Analysis remains queued after sleep/restart:** Send a request to wake the web service, then check `/api/snapshots/{id}/status`. Startup recovers queued and expired-lease work. Check Render logs and Neon status.
- **Quota or model errors:** Gemini and GitHub enforce their own changing free-tier limits. Retry after the provider limit resets; do not increase app quotas to bypass provider limits.

## Rollback and cleanup

Before schema changes, export a Neon backup. To stop charges, delete any accidentally created paid resource and verify Render shows no paid services; this project configuration creates only a Free web service. Removing the Render service does not delete the Neon project, so delete that separately if you no longer need the data.
