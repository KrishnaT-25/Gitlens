# Phase 1 contract

- Supported GitHub pages on the exact HTTPS `github.com` host: repository root; `/tree/{ref}[/directory]`; `/blob/{ref}/{file}`; `/commit/{sha}`; `/commits[/{ref}[/path]]`; and `/issues[/{number}]`. PR pages (`/pull`, `/pulls` and descendants) return `unsupported_route` (422); the extension displays an explanation and makes no analysis request. Other routes, including settings and search, are unsupported.
- Root, issue pages, and bare `/commits` select the default branch. Named branch/tag URLs resolve the longest matching ref prefix before the remaining directory/file path, including slash-containing and percent-encoded names. Equal-name branch/tag matches prefer the branch; `refs/heads/` and `refs/tags/` explicitly disambiguate. Full 40-character SHAs in tree/blob and 7–40-character hexadecimal commit identifiers resolve to the provider's immutable 40-character SHA. Missing selected refs return `ref_not_found` (422), never silently use the default branch. Malformed URLs (including incomplete routes, invalid ports, escapes, controls, and traversal) return `invalid_url` (400).
- The widget displays the resolved ref and abbreviated actual SHA; its tooltip contains the complete SHA. The ref is request-specific, not persisted on a shared snapshot: aliases of the same repository/SHA share one snapshot and ingestion job.
- A snapshot is unique for `(repository_id, sha)` and moves `queued → indexing → summarizing → ready`, or to `failed`/`unsupported`. A failed job may retry up to three times. Ready requires indexed chunks and a completed summary. No progress percentage is returned.
- `GET /api/auth/github/config` returns the public OAuth client ID and fixed Chrome redirect URI. The extension uses GitHub OAuth 2.0 authorization code with PKCE and validates state. `POST /api/auth/github` accepts `{code,codeVerifier}`, exchanges server-side, reads only GitHub's public user identity, and returns `{token,expiresAt}`. `GET /api/auth/me` and `POST /api/auth/logout` require this GitLens bearer token. Email-code endpoints remain for local test compatibility and are disabled in production.
- `POST /api/repositories` accepts `{url}` and returns `{snapshotId,sha,status,ref}`. The additive `ref` field is a fully qualified branch/tag (`refs/heads/main`, `refs/tags/v1.0`) or the resolved SHA for commit selection. Clients preserve the submission's ref while polling or retrying. `GET /api/snapshots/{id}/status` returns `{snapshotId,sha,status,errorCode,search}`; `search` is additive (see below). Retry responses retain their existing shape. `GET /api/snapshots/{id}/summary` returns a summary only when ready. `POST /api/snapshots/{id}/chat` accepts `{question,history}` and returns `{answer,insufficientEvidence,citations,sha}`.
- Chat retrieval combines keyword search (BM25 when available, PostgreSQL full-text search otherwise) with compatible embeddings when available. Each search uses one snapshot and its published index version. The internal search tool applies optional file/directory paths and literal, case-insensitive symbol substrings before ranking candidates. Keyword ranking favors direct terms and allows separate chunks from one file to rank together. For whole-pipeline questions with evidence of both indexing and answering, retrieval reserves space for both stages. Defaults are 8 matching chunks, up to 16 context chunks, and 56,000 context characters. Recognized follow-ups reuse the recent user topic on the first search. Prior assistant answers provide conversational context only. Citations require retrieved evidence.
- The chat request accepts an optional `answerLength` field with values `default`, `short`, or `detailed`. It defaults to `default` when omitted. All modes use the same evidence and citation requirements.
- Chat history accepts `user` and `assistant` roles. Defaults allow two messages and 8,000 total content characters. User history has the same limit as a new question (2,000 by default), and assistant history has the same 6,000-character limit as an answer.
- Errors use `{error:{code,message}}`; the API never includes secrets, raw provider responses, or repository source in error bodies.
- Chat citations are scoped to the source excerpts actually delivered to the model. A bounded file read can cite only the lines it returned, and tool entries omitted for size are ineligible. Inline source ranges must fit a verified cited excerpt. If the inspection budget prevents a grounded answer, the API returns `chat_tool_budget` (503). If the model has enough evidence when it reaches the limit, chat can still return a cited answer.

The status response's `search` object exposes semantic indexing independently of
snapshot readiness. A ready snapshot can serve its completed text index and
summary while embeddings are unavailable or being repaired:

```json
{"mode":"keyword","semanticStatus":"keyword_only","keywordSearch":"postgres_fts","embeddingFingerprint":"<sha256>","reason":"embedding_rate_limit"}
```

`mode` is `hybrid` only for a completed, compatible embedding index with a configured
provider key; otherwise it is `keyword`. `semanticStatus` is `pending`, `ready`,
`keyword_only`, `incompatible`, or `unavailable`. `reason` is null on success and a
safe `embedding_*` code on failure/incompatibility. `embeddingFingerprint` is null
for legacy indexes. `keywordSearch` is `bm25` or `postgres_fts`. This describes
index capability, not whether a particular query's provider call succeeds: a
transient query embedding failure still falls back to keyword retrieval.

Submission or status polling schedules a single leased repair when the configured
embedding space changes. `status` remains `ready` for an existing usable index;
repair reuses chunk IDs, source line ranges, and cached summaries. Chat shapes,
authentication, citations, and snapshot identity are unchanged.

While a replacement index is built, chat and summary requests use the published
version. The worker switches the index version, summary, and search report in one
transaction. Rebuild failures and exhausted retries are recorded on the job while
the snapshot continues to serve its published index.

Chat processing has one overall deadline (`CHAT_TIMEOUT_SECONDS`, default 60,
supported range 1–60 seconds). It starts before database authentication and
includes database retries, quotas, retrieval/query embeddings, tool execution,
model turns and retry delays. Expiry returns HTTP **504** with
`{"error":{"code":"chat_timeout","message":"Repository analysis timed out. Please try a more focused question."}}`.
This is a timeout failure, not an insufficient-evidence answer. Existing success
and citation shapes are unchanged. Snapshot ingestion/indexing deadlines are
independent of the chat deadline.

The extension allows **70 seconds** for a chat response, including reading the
body. Other API calls retain 28 seconds. Server wake-up readiness checks run
before the API request and retain their separate limits. Rebuild/reload the
extension when adopting this client change.

Chat routing is deterministic and adds no classifier-model call. A short lookup
with initial source evidence may call `final_answer` on its first turn; all
repository tools remain available when evidence is inadequate. Multi-file,
comparison, explanation/flow and reasoning questions retain initial inspection.
Missing evidence and ambiguous short follow-ups also retain inspection. Citation
IDs are still checked against this snapshot's displayed/retrieved evidence.

Initial context removes duplicate IDs, contained chunks and identical same-file
overlaps only when this reduces payload size. Partial-overlap blocks identify
their original chunk range and the additional line ranges; shared lines remain
in earlier blocks. Conflicting text and inconsistent line metadata are preserved.
Original chunk IDs and citation ranges are unchanged. The evidence limits remain
8 anchors, 16 context chunks and 56,000 characters by default.

The compact navigation map prioritizes root structure and relevant source paths,
keeps whole entries, and explicitly points to `list_dir`/`search` for expansion.
Its effective limit is the smaller of `CHAT_MAP_CHARS` (default 4,000) and
`MAX_REPO_MAP_CHARS`. It remains in conversation context on every agent turn and
is navigation metadata, not citation evidence. Lookup answers request minimal
thinking, ordinary inspection uses low, and complex explanations request medium.
Models without documented minimal support retain low thinking instead.
