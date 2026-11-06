# GitLens Phase 1

An in-page assistant for public GitHub repositories. The Chrome extension is React/JavaScript; the API and ingestion worker are Python/FastAPI; PostgreSQL stores commit-specific snapshots and full-text indexes. The optional hosted demo uses Render Free for the API and in-process worker, plus Neon Free for PostgreSQL; no payment method is required by this configuration.

## Local setup

1. Start PostgreSQL with the pgvector extension available and create a database named `gitlens`. For Docker, use `pgvector/pgvector:pg17` rather than the plain PostgreSQL image.
2. Copy `server/.env.example` to `server/.env`; set `DATABASE_URL` and `SESSION_PEPPER`. Register a GitHub OAuth app with callback `https://copoojjfbgbgcnaokahlbopnafkljfkf.chromiumapp.org/github`, then set `GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET` in the server file. Keep the client secret out of the extension. `EMAIL_PROVIDER=console` remains available for backend-only local tests.
3. In `server/`, run `python -m venv .venv`, activate it, run `pip install -r requirements-dev.txt`, and `alembic upgrade head`.
4. Start the API with `python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001` and the worker with `python -m app.worker` in separate terminals.
5. In `extension/`, run `npm ci`, copy `.env.example` to `.env`, then `npm run build`. Load `extension/dist` at `chrome://extensions` using **Load unpacked**.
6. Visit a public GitHub repository and use **Continue with GitHub**. The OAuth app requests no scopes and GitLens stores its own session token; it does not retain the GitHub access token.

Run backend tests with `python -m pytest` and extension tests with `npm test`. No-card hosted setup is in [`DEPLOY.md`](DEPLOY.md); operating limits are in `docs/operations.md`.

## Database and CI verification

Migration `0004_agentic_rag` requires pgvector (`CREATE EXTENSION vector` privileges).
`pg_search` is optional: unavailable extension files or insufficient privileges leave
the native PostgreSQL GIN full-text index in place. Embedding storage and its HNSW
index still require pgvector. Downgrades retain installed extensions, which may be
shared by other applications, while removing GitLens tables and indexes.

CI runs backend and frontend as independent jobs. Backend CI starts
`pgvector/pgvector:pg17` without pg_search, upgrades a fresh database, runs pytest
(including PostgreSQL fallback regressions), and performs a full downgrade/upgrade.
For local integration checks, use a disposable test database and set both
`DATABASE_URL` and `TEST_DATABASE_URL` to its SQLAlchemy PostgreSQL URL before
running `alembic upgrade head` and `python -m pytest -q` from `server/`.
The integration tests truncate application data; never point these settings at a
development or production database containing data you need.

Frontend CI builds, tests, and lints the extension even when the backend job fails.
Automated adapter tests mock GitHub, Gemini, and email; PostgreSQL integration
tests use a real database but mock external providers. Live-provider evaluations
remain separate under `eval/` and are not run by CI. This repair changes no public
HTTP endpoints or request/response formats.

## Information-retrieval evaluation

The offline IR evaluator measures retrieval independently of Gemini's answer. It calls the same snapshot-scoped hybrid retriever used by chat and scores returned chunk IDs against human-reviewed `relevant_chunk_ids` in `eval/ir_testset.jsonl`. Run these commands from `server/` so the existing `server/.env` settings are loaded:

```powershell
$env:GITLENS_EVAL_SNAPSHOT_ID = "<ready-snapshot-id>"
# Optional for test-set generation; read from the environment, never committed.
$env:GEMINI_API_KEY = "<your-key>"
python ..\eval\generate_testset.py --count 50 --export-for-review
```

Review `eval/manual_review.jsonl`, correct weak or unsupported questions and answers, then run:

```powershell
python ..\eval\run_ir_eval.py
python ..\eval\compare_configs.py
```

The generator samples distinct indexed chunks and asks Gemini for one grounded question/reference answer per chunk. The generated test set is a draft: verify the questions, answers, and source chunk IDs before treating the metrics as a quality signal. `--export-for-review PATH` selects another review file; `--snapshot-id`, `--output`, and `--count` can override defaults. The API key comes from `GEMINI_API_KEY` through server settings.

`eval/config.yaml` selects the snapshot, test set, results directory, top-k, chunk size/overlap profile, embedding model, and comparison variants. Current production indexes use Tree-sitter/section chunks with 80-line/6,000-character fallback windows and no overlap. Index configuration is not persisted per snapshot yet, so this evaluator accepts only that current chunk profile and the embedding model already stored with the snapshot. It deliberately rejects chunk-size, overlap, or embedding-model variants rather than pretending to reindex; compare retrieval top-k values now. True indexing experiments need separately versioned indexes and reindexing.

Results go to timestamped files in `eval/results/` (ignored by Git): per-question CSV includes retrieved chunk IDs/scores and first relevant rank; summary JSON includes the aggregate metrics and ten worst cases with expected and retrieved locations; config comparisons write one aggregate CSV. Metrics are macro-averaged across questions:

- **Hit Rate@k**: fraction of questions with at least one relevant chunk in the first *k* results.
- **Recall@k**: fraction of each question's relevant chunks retrieved in the first *k*.
- **Precision@k**: relevant results divided by *k*.
- **MRR**: average reciprocal rank of the first relevant result; higher rewards finding evidence early.
- **nDCG@k**: relevance gain discounted by rank and normalized against an ideal ordering; higher rewards placing all relevant evidence near the top.
- **File Hit Rate@k**: fraction of questions where at least one expected source file appears in the top *k* chunks, even if the exact judged chunk did not. It can reveal chunk-boundary misses that chunk-level recall cannot distinguish.

Run metric unit tests with `python -m pytest tests/test_ir_metrics.py` from `server/`.
