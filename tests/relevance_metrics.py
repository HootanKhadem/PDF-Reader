"""Model-free retrieval metrics for the relevance harness."""
import math


def score_case(case, results):
    """Score one query. `results` are search results in rank order (dicts with a `page`)."""
    wanted = set(case['pages'])
    pages = [r['page'] for r in results]
    relevant = [p in wanted for p in pages]
    first = next((i + 1 for i, ok in enumerate(relevant) if ok), None)
    return {
        'query': case['query'],
        'pages': pages,
        'first_relevant_rank': first,
        'reciprocal_rank': 1 / first if first else 0.0,
        'precision': sum(relevant) / len(relevant) if relevant else 0.0,
        'noise': len(relevant) - sum(relevant),
        'found': first is not None,
    }


def percentile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def summarize(scores, latencies=(), k=3):
    n = len(scores)
    if not n:
        return {'cases': 0}
    return {
        'cases': n,
        f'hit@{k}': sum(1 for s in scores if s['first_relevant_rank'] and s['first_relevant_rank'] <= k) / n,
        'hit@any': sum(s['found'] for s in scores) / n,
        'mrr': sum(s['reciprocal_rank'] for s in scores) / n,
        'precision': sum(s['precision'] for s in scores) / n,
        'noise_per_query': sum(s['noise'] for s in scores) / n,
        'latency_p50_s': percentile(list(latencies), .5),
        'latency_p95_s': percentile(list(latencies), .95),
    }


def validate_cases(cases):
    """Reject malformed or placeholder cases before they silently score zero."""
    for i, c in enumerate(cases):
        if not str(c.get('query', '')).strip():
            raise ValueError(f'case {i}: empty query')
        if not str(c.get('book', '')).strip():
            raise ValueError(f'case {i}: missing book (title or filename fragment)')
        pages = c.get('pages')
        if not pages or not all(isinstance(p, int) and p >= 1 for p in pages):
            raise ValueError(f'case {i}: pages must be a non-empty list of one-based PDF pages')
    return cases
