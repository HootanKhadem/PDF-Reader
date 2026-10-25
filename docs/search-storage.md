# SQLite + Qdrant storage

Implemented 2026-10-25 at the user's request.

SQLite is authoritative for books, extracted pages, chunks, keyword search (FTS5), and recovery copies of embeddings. Qdrant holds a derivative cosine/HNSW index. Search sends the query embedding and optional book filter to Qdrant, combines the returned IDs with SQLite keyword candidates, then fetches only candidate text for local reranking. The application no longer loads every vector or computes a full-corpus NumPy similarity matrix per query.

## Persistence and isolation

Qdrant image: `qdrant/qdrant:v1.20.0`. Storage: `qdrant-data` Docker volume. Dashboard: http://localhost:6333/dashboard. REST binds only to localhost on the host; the application uses the internal Docker address. SQLite, model cache, and uploaded originals retain their existing volumes.

A persistent random UUID in each SQLite library, embedding-model identity, and vector dimension define its Qdrant collection. Tests receive independent UUIDs/collections and delete only their own collections. Stable UUID point IDs derive from versioned chunk IDs. Book ID and generation payload indexes support filtering and cleanup.

## Synchronization

1. OCR/chunking/embedding completes before final replacement.
2. One SQLite transaction replaces book text/chunks and invalidates its sync marker.
3. Qdrant acknowledges current vectors, then removes obsolete generations for that book.
4. SQLite records successful synchronization only after acknowledgement.

Final replacement and candidate retrieval share an application lock. Keep one application worker. If a write fails or the process stops between databases, SQLite retains the pending state and recovery embeddings. Startup, subsequent searches, and unchanged-file ingestion retry pending synchronization. Searches return HTTP 503 while Qdrant is unavailable; there is no silent full-scan fallback. Text and original PDFs remain accessible.

Startup detects missing collections and count mismatches. `POST /index/sync` forces reconciliation from SQLite without OCR or embedding inference; `GET /index` reports readiness, counts, and pending books. Orphan points for deleted metadata are pruned during reconciliation. Treat SQLite as the source of truth; restoring Qdrant alone does not restore text or metadata.

## Search choices

HNSW uses cosine distance, m=16, ef_construct=128, hnsw_ef=128, a 1000 KB indexing/full-scan threshold, and an indexed book-ID filter. Small filtered subsets may still use Qdrant's internal exact search when cheaper. For larger subsets it can use HNSW; approximate retrieval can trade some recall for speed. Background indexing must complete before interpreting indexed-vector counts.

Keyword matching stays in SQLite; rank fusion and local reranking preserve hybrid retrieval. The default excerpt budget remains 1500 embedding-tokenizer tokens, excluding metadata. Qdrant changes retrieval scale, not OCR accuracy or embedding quality.

Elasticsearch remains a possible alternative for richer keyword analysis and aggregations. It is not part of this installation.

Primary references:

* [Qdrant release](https://github.com/qdrant/qdrant/releases/tag/v1.20.0)
* [Collection configuration](https://api.qdrant.tech/api-reference/collections/create-collection)
* [Vector queries](https://api.qdrant.tech/api-reference/search/query-points)
* [Hybrid retrieval](https://qdrant.tech/documentation/search/hybrid-queries/)
