# Multi-repository response quality benchmark

[The case list](response_quality_cases.jsonl) contains 100 prompts across four public repositories at pinned commits. The file labels identify expected implementation evidence. They are a starting point for grading, since other files can also support a correct answer.

## Baseline

Production retrieval code from the local working tree, including the pending multi-file and walkthrough changes, ran against PostgreSQL full-text search. Each repository was downloaded and parsed with the application indexer. The runner checked the first eight chunks for the expected files.

| Repository | All expected files | Some expected files | None | Negative controls |
| --- | ---: | ---: | ---: | ---: |
| GitLens | 15 | 3 | 6 | 1 |
| DropSync | 10 | 3 | 10 | 2 |
| Requests | 20 | 2 | 2 | 1 |
| Flask | 17 | 2 | 5 | 1 |
| Total | 62 | 10 | 23 | 5 |

Complete file coverage was 62 of 95 answerable prompts (65%). For the 19 prompts with multiple expected files, coverage was complete in eight (42%), partial in ten, and absent in one. These are file-level retrieval scores. They do not establish line-level grounding or answer correctness. The five negative controls need generated answers before they can be graded. This run used no embeddings or Gemini generation.

The DropSync comparison retrieved both named files. The walkthrough retrieved the server signaling code but missed the meeting page and browser peer implementation in the first eight chunks. The unscoped `RTCPeerConnection` lookup also missed its expected file. In GitLens, documentation or lockfiles sometimes outranked implementation files. Prioritize exact symbols and code for behavior questions, then allocate evidence across the requested stages. Keep documentation eligible when the question asks about docs or setup.

## Reproduce the run

Migrate a disposable PostgreSQL database as described in the [README](../README.md). Set both `DATABASE_URL` and `GITLENS_EVAL_DATABASE_URL` to that same test database, then run from the repository root:

```text
python -m eval.run_response_quality
```

The runner rolls back its seeded rows and writes the per-case evidence paths and ranges to `eval/results/response_quality.json`. Use `--repository owner/name` and `--limit N` for a smaller run. For live generation, provide `GEMINI_API_KEY` through the environment and add `--answers`. Grade the saved answers against the pinned source for factual accuracy, coverage of each requested part, and whether every cited line supports the associated claim. For negative controls, check that the response challenges the unsupported premise.
