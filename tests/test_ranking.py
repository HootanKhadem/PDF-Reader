"""Model-free tests for result selection and the search cache."""
from app import ranking


def test_rerank_text_prefixes_and_caps_section():
    assert ranking.rerank_text('Ability Scores','Subtract 10.')=='Ability Scores\nSubtract 10.'
    assert ranking.rerank_text('','body')=='body' and ranking.rerank_text(None,'body')=='body'
    assert ranking.rerank_text('x'*500,'body').split('\n')[0]=='x'*120


def test_drop_weak_cross_encoder_margin():
    scores={'a':8.0,'b':6.5,'c':1.0,'d':-9.0}
    kept,weak=ranking.drop_weak(['a','b','c','d'],scores,True,margin=6)
    assert kept==['a','b'] and weak==['c','d']


def test_drop_weak_fusion_ratio():
    scores={'a':0.032,'b':0.020,'c':0.010}
    kept,weak=ranking.drop_weak(['a','b','c'],scores,False,ratio=.5)
    assert kept==['a','b'] and weak==['c']


def test_drop_weak_always_keeps_best_even_if_negative():
    kept,weak=ranking.drop_weak(['a','b'],{'a':-7.0,'b':-30.0},True,margin=6)
    assert kept==['a'] and weak==['b']
    assert ranking.drop_weak([],{},True)==([],[])


def test_cache_returns_independent_copies_and_evicts_lru():
    cache=ranking.ResultCache(size=2)
    epoch=cache.epoch
    cache.put('a',{'r':[1]},epoch)
    cache.put('b',{'r':[2]},epoch)
    cache.get('a')['r'].append(99)
    assert cache.get('a')=={'r':[1]}
    cache.put('c',{'r':[3]},epoch)   # evicts b, the least recently used
    assert cache.get('b') is None and cache.get('a') and cache.get('c')


def test_cache_clear_discards_stale_in_flight_write():
    cache=ranking.ResultCache(size=4)
    epoch=cache.epoch
    cache.clear()                     # e.g. a book was re-ingested mid-search
    assert cache.put('a',{'r':1},epoch) is False
    assert cache.get('a') is None
    assert cache.put('a',{'r':1},cache.epoch) is True


def test_cache_size_zero_disables_caching():
    cache=ranking.ResultCache(size=0)
    assert cache.put('a',{},cache.epoch) is False and cache.get('a') is None
