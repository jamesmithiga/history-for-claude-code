# Roadmap (prioritised)

## P1: Retention and reach
1. **Retention guard.** Show a warning in Index health when `cleanupPeriodDays` is unset or short, with a one-click explanation. Optionally keep raw copies of transcripts (opt-in, compressed) for anyone who needs byte-exact originals.
2. **Index session titles and summaries** as searchable rows, so a query like "schema migration" also matches session titles.
3. **Duplicate collapsing** for resumed or forked sessions (same `uuid` in several sessions), as a grouped result in the UI.
4. **Windows scheduled task** (`cch sync` hourly) for machines where neither Claude Code nor the web app runs for weeks.

## P2: Semantic retrieval (optional; FTS stays the baseline)
1. A `SemanticRetriever` that implements the existing `search.Retriever` protocol, storing vectors in the already-created `embeddings` table (message_id → model + float32 blob).
2. Local embeddings via `fastembed` (ONNX, CPU, ~100 MB model), installed as an optional extra. Embed user and assistant messages and summaries, but not raw tool output, chunked at about 512 tokens.
3. Hybrid ranking: Reciprocal Rank Fusion of the BM25 and cosine rankings (k = 60), exposed as `mode=hybrid` in the API, CLI and MCP.
4. Backfill embeddings in the background sync worker, tracked per message with the model name so a model change re-embeds.

## P3: Grounded answers (opt-in, explicit consent)
1. "Ask your history": retrieve the top-k excerpts (hybrid), then send them to a configured model. The API key goes in the OS keyring (`keyring`), never in the config file. The answer cites `[msg id]`, and the UI keeps retrieved quotes visually separate from the model's interpretation.
2. Inside Claude Code this already works today with no extra API: the MCP tools retrieve, and the running Claude session does the reasoning.

## P4: Deeper editor integration
1. ~~VS Code extension~~ (done in 0.1: webview search, search selection, one-click resume, insert pack). Next: a sidebar tree of recent sessions per project, and "related history" for the open file (sessions that touched the same path).
2. A `/history` slash command (`~/.claude/commands/history.md`) that tells Claude to call `search_history` for the user's query.
3. Watch mode based on filesystem events (`ReadDirectoryChangesW` via `watchdog`, optional) instead of polling.

## P5: Scale hardening
1. Benchmarks with a synthetic 500k-message corpus (the real one is about 10k). Today's page query includes an exact `COUNT(*)`, which should be capped or estimated above about 50k matches.
2. A trigram secondary index (FTS5 `trigram`) to speed up Literal mode on large corpora.
