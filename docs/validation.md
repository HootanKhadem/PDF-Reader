# Verification — 2026-10-25

Current extraction revision: `v4-confidence-columns-cells`. Original PDFs remain read-only and unchanged.

## Private books and first-run onboarding

The two existing PDF files were moved into the gitignored host `books/` folder. Only that directory is mounted read-only at `/books`; filenames, existing book IDs, indexed chunks, and original-page links remain valid. PDFs, generated book-preview images, and IDE files are excluded from Git; private books are also excluded from Docker build context.

Startup and `GET /library/status` distinguish an empty library, available-but-unindexed PDFs, and an indexed library. The empty browser state displays an upload walkthrough and disables search until material is indexed. Existing local PDFs produce indexing guidance. Missing originals do not hide otherwise searchable saved text.

An isolated empty-library container on localhost:8001 verified the upload guide and disabled controls. A generated PDF uploaded through its API indexed successfully; refreshing the browser removed the guide and enabled search. Its temporary metadata/upload paths and Qdrant collection are independent of the real library.

## SQLite + Qdrant migration

Implemented with Qdrant `v1.20.0` in Docker. SQLite remains authoritative for text, metadata, keyword search, and recovery embeddings; Qdrant supplies semantic candidates. Existing vectors migrated without OCR or embedding inference. The application fetches only candidate text, with no full-corpus vector scan.

Final index: **5,220 SQLite chunks = 5,220 Qdrant points = 5,220 HNSW-indexed vectors**, green status, zero pending books. The collection and both real books survived recreation of both containers. Each test used its own SQLite UUID/Qdrant collection; temporary test collections were removed.

A new PDF uploaded through the live API was indexed in Qdrant, remained searchable after recreation, and retained identical source bytes. Only that uniquely named, hash-verified test book was then removed. The real library remains two books.

Actual outage check: Qdrant was stopped while the application continued running. Search and health returned HTTP 503; both books and repaired SQLite page text remained accessible. Qdrant was restarted; health recovered and the full REST/MCP query evaluation passed. There was no silent full-scan fallback. Compose now defines an explicit `/readyz` health probe and waits for Qdrant readiness before starting the application.

New regressions verify missing-collection rebuild without embedding calls, idempotent synchronization, obsolete-generation removal, real connection failure with durable pending state and recovery, and candidate-only SQLite reads. `GET /index` reports state; `POST /index/sync` forces reconciliation without OCR or embedding inference. The sync endpoint also passed its REST regression.

| Book | PDF pages | Searchable chunks |
| --- | ---: | ---: |
| Player's Handbook | 387 | 2,172 |
| Monster Manual | 390 | 3,048 |
| Total | 777 | 5,220 |

Both books survived container recreation. A subsequent scan reported both `unchanged`. Only these two real books remain after removal of a disposable upload probe.

## Reported extraction failures

The initial extractor trusted existing OCR whenever enough characters were present, accepting artwork debris. A two-column heuristic also interleaved four-column lists. Fresh confidence-scored OCR, column grouping, and noise rejection replace those paths.

The first confidence filter also omitted short stat rows. A real-page regression caught this; confidently recognized compact rows are now retained. Stored REST page 20 contains both `HP 14` and `HP 27`, both monster headings, and zero one-/two-character debris lines. Page 382 retains column order: Aboleth → Brown Bear → Mastiff → Animated Armor. These checks exercise indexed API text, not only an extraction helper.

## Regression checks

`docker compose exec -T library python -m pytest -q -p no:cacheprovider`: **20 passed**, exit 0. New checks cover empty-library upload guidance, discovery of unindexed PDFs, additional material alongside an existing index, and missing original sources without losing indexed-book availability.

Coverage: semantic matching, budgets, citations, filtering, chunk expansion/coverage, idempotence, failed replacement preservation, REST validation, scanned OCR, concurrent ingestion/search, four-column order, native numeric cells, reported pages and HP rows, upload/validation/source serving, raw OCR reuse, and reuse of unchanged same-model embeddings. Upstream deprecation/settings warnings remain; no test failures.

## Live retrieval and protocol checks

`docker compose exec -T library python -m tests.evaluate`: **4/4 queries passed**, exit 0.

Every REST search and MCP search result explicitly reported `retrieval_backend: qdrant`; index point/chunk counts matched and the HNSW index was present.

| Query | Returned PDF pages | Excerpt tokens |
| --- | --- | ---: |
| keeping a spell active after getting hurt | 372, 362, 341, 52 | 1,200 |
| how to recover lost health while sleeping | 369, 210, 84, 189, 367 | 1,200 |
| a creature whose stare turns victims into stone | 35, 288, 336, 228, 208 | 1,128 |
| a floating monster with many magical eye beams | 97, 38, 292, 172, 9 | 1,200 |

Budget: 1,200 text tokens, excluding metadata. Concentration check requires the actual saving-throw passage, included on PDF page 362. Other checks look for expected topic terms. These are smoke checks, not exhaustive relevance or answer-accuracy measurements.

MCP initialization, all four tools, bounded search, and chunk reading succeeded. REST page reads respected their budget; original-PDF range reading returned HTTP 206 with a valid PDF header. Both reported pages passed live API checks including HP rows. Service remains healthy on localhost.

## Actual uploaded-book persistence

`tests.live_upload create` uploaded a uniquely named generated PDF through the running HTTP endpoint. Automatic indexing processed only that book. Search, metadata, and original-PDF bytes verified. After container recreation, `tests.live_upload verify` confirmed all remained available. `tests.live_upload cleanup` removed only the uniquely named, hash-verified test PDF and its derived rows. Original books were unaffected.

Upload validation covers non-PDF content, unsafe filenames/extensions, duplicate names, busy ingestion, size limits, and temporary-file cleanup. Uploaded PDFs persist in `uploaded-books`. Raw OCR and indexes persist in `library-data`; models persist separately.

## Limits

OCR can still misread decorated names, numbers, and tables. Filtering may omit difficult text. Images are not visually indexed. Verify exact rules/statistics against original pages. All dependencies and checks run inside Docker; no hosted generation API required or tested. No external AI client configured; REST/MCP protocol clients verified.
