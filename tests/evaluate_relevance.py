"""Measure retrieval quality and latency against your own labeled questions.

Run inside Docker after indexing:
  docker compose exec -T library python -m tests.evaluate_relevance --cases /app/tests/relevance_cases.json
Compare settings with --compare; gate changes with --min-mrr.
Case format: see tests/relevance_cases.example.json (pages are one-based PDF pages).
"""
import argparse
import json
import sys
import time

import httpx

from tests.relevance_metrics import score_case, summarize, validate_cases

BASE = 'http://localhost:8000'
CONFIGS = {'rerank': {'rerank': True}, 'no-rerank': {'rerank': False}}


def run(client, books, cases, options, top_k, budget):
    scores, latencies = [], []
    for case in cases:
        book = next((b for b in books if case['book'].lower() in (b['title'] + ' ' + b['filename']).lower()), None)
        if not book:
            raise SystemExit(f"No indexed book matches {case['book']!r}")
        started = time.perf_counter()
        response = client.post(BASE + '/search', json={'query': case['query'], 'book_id': book['id'],
                                                        'top_k': top_k, 'token_budget': budget} | options)
        response.raise_for_status()
        latencies.append(time.perf_counter() - started)
        scores.append(score_case(case, response.json()['results']))
    return scores, latencies


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--cases', default='tests/relevance_cases.json')
    ap.add_argument('--top-k', type=int, default=5)
    ap.add_argument('--budget', type=int, default=1500)
    ap.add_argument('--compare', action='store_true', help='report rerank and no-rerank side by side')
    ap.add_argument('--min-mrr', type=float, help='exit 1 if the first config scores below this')
    ap.add_argument('--verbose', action='store_true', help='list queries that missed or returned noise')
    args = ap.parse_args(argv)
    cases = validate_cases(json.load(open(args.cases)))
    configs = CONFIGS if args.compare else {'rerank': CONFIGS['rerank']}
    report = {}
    with httpx.Client(timeout=300) as client:
        books = client.get(BASE + '/books').json()
        client.post(BASE + '/search', json={'query': 'warmup'})  # keep model load out of latency
        for name, options in configs.items():
            scores, latencies = run(client, books, cases, options, args.top_k, args.budget)
            report[name] = summarize(scores, latencies)
            if args.verbose:
                report[name]['problem_queries'] = [s for s in scores if not s['found'] or s['noise']]
    print(json.dumps(report, indent=2))
    first = next(iter(report.values()))
    if args.min_mrr is not None and first['mrr'] < args.min_mrr:
        print(f"MRR {first['mrr']:.3f} is below {args.min_mrr}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
