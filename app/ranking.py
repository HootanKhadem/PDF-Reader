"""Model-free helpers for result selection and caching.

Kept free of heavy imports so the logic is cheap to unit test.
"""
import copy
import os
import threading
from collections import OrderedDict


def _env_number(name, default, cast):
    try:
        return cast(os.getenv(name, default))
    except ValueError:
        return cast(default)


# Cross-encoder candidates scored per query. Cost grows linearly with this number.
RERANK_CANDIDATES = max(5, _env_number('RERANK_CANDIDATES', 40, int))
# Cross-encoder logits: drop results this far below the best one.
RERANK_MARGIN = _env_number('RERANK_MARGIN', 6.0, float)
# Rank-fusion scores (rerank off): drop results below this share of the best one.
FUSION_MIN_RATIO = _env_number('FUSION_MIN_RATIO', 0.45, float)
CACHE_SIZE = _env_number('SEARCH_CACHE_SIZE', 128, int)


def reranker_options(backend=None, onnx_file=None):
    """CrossEncoder kwargs for the optional quantized ONNX backend (empty = PyTorch)."""
    backend = (backend if backend is not None else os.getenv('RERANK_BACKEND', 'torch')).lower()
    if backend != 'onnx':
        return {}
    onnx_file = onnx_file or os.getenv('RERANK_ONNX_FILE', 'onnx/model_qint8_avx2.onnx')
    return {'backend': 'onnx', 'model_kwargs': {'file_name': onnx_file}}


def rerank_text(section, text, limit=120):
    """Reranker input: the book section gives short or numeric passages their topic."""
    section = (section or '').strip()
    if not section:
        return text
    if len(section) > limit:
        section = section[:limit].rstrip()
    return f'{section}\n{text}'


def drop_weak(ranked, scores, reranked, margin=None, ratio=None):
    """Split a best-first list into (kept, weak). The best hit is always kept.

    A relative cutoff keeps a query with one good passage from being padded
    with unrelated pages just to reach top_k.
    """
    margin = RERANK_MARGIN if margin is None else margin
    ratio = FUSION_MIN_RATIO if ratio is None else ratio
    if not ranked:
        return [], []
    best = scores[ranked[0]]
    floor = best - margin if reranked else best * ratio
    kept = [ranked[0]] + [i for i in ranked[1:] if scores[i] >= floor]
    return kept, [i for i in ranked[1:] if scores[i] < floor]


class ResultCache:
    """Small thread-safe LRU. Writes computed before a clear() are discarded."""

    def __init__(self, size=CACHE_SIZE):
        self.size = size
        self._items = OrderedDict()
        self._lock = threading.Lock()
        self._epoch = 0

    @property
    def epoch(self):
        with self._lock:
            return self._epoch

    def get(self, key):
        with self._lock:
            if key not in self._items:
                return None
            self._items.move_to_end(key)
            return copy.deepcopy(self._items[key])

    def put(self, key, value, epoch):
        if self.size <= 0:
            return False
        with self._lock:
            if epoch != self._epoch:
                return False
            self._items[key] = copy.deepcopy(value)
            self._items.move_to_end(key)
            while len(self._items) > self.size:
                self._items.popitem(last=False)
            return True

    def clear(self):
        with self._lock:
            self._epoch += 1
            self._items.clear()
