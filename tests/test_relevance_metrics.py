"""Model-free tests for the relevance harness metrics."""
import pytest

from tests.relevance_metrics import percentile, score_case, summarize, validate_cases

CASE = {'query': 'q', 'book': 'b', 'pages': [10]}


def test_score_case_ranks_and_noise():
    s = score_case(CASE, [{'page': 3}, {'page': 10}, {'page': 4}, {'page': 10}])
    assert s['first_relevant_rank'] == 2 and s['reciprocal_rank'] == .5
    assert s['precision'] == .5 and s['noise'] == 2 and s['found']


def test_score_case_miss_and_empty():
    miss = score_case(CASE, [{'page': 1}])
    assert not miss['found'] and miss['reciprocal_rank'] == 0 and miss['precision'] == 0
    assert score_case(CASE, [])['noise'] == 0


def test_summarize_means_and_hit_at_k():
    scores = [score_case(CASE, [{'page': 10}]), score_case(CASE, [{'page': 1}, {'page': 2}, {'page': 3}, {'page': 10}])]
    out = summarize(scores, [1.0, 3.0], k=3)
    assert out['cases'] == 2 and out['hit@3'] == .5 and out['hit@any'] == 1
    assert out['mrr'] == pytest.approx((1 + .25) / 2) and out['latency_p95_s'] == 3.0
    assert summarize([]) == {'cases': 0}


def test_percentile():
    assert percentile([], .5) == 0.0
    assert percentile([5, 1, 3], .5) == 3 and percentile([1, 2, 3, 4], .95) == 4


@pytest.mark.parametrize('bad', [{'query': ' ', 'book': 'b', 'pages': [1]}, {'query': 'q', 'pages': [1]},
                                 {'query': 'q', 'book': 'b', 'pages': []}, {'query': 'q', 'book': 'b', 'pages': [0]}])
def test_validate_rejects_bad_cases(bad):
    with pytest.raises(ValueError):
        validate_cases([bad])
    assert validate_cases([CASE]) == [CASE]
