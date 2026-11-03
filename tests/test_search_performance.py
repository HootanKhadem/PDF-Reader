"""Latency and relevance behavior of core.search (real models + Qdrant, like the other suites)."""
import pytest

from app import core, vector_index


class FakeReranker:
    def __init__(self,scorer):
        self.scorer=scorer
        self.calls=[]

    def predict(self,pairs,**kwargs):
        self.calls.append(pairs)
        return [self.scorer(q,t) for q,t in pairs]


def test_repeat_search_is_served_from_cache(library,monkeypatch):
    bid=core.list_books()[0]['id']
    first=core.search('keeping magic active after being hurt',bid)
    monkeypatch.setattr(core,'embedder',lambda:pytest.fail('model used on cache hit'))
    monkeypatch.setattr(core,'retrieve',lambda *a:pytest.fail('retrieval ran on cache hit'))
    again=core.search('keeping magic active after being hurt',bid)
    assert again==first and again is not first


def test_reingest_invalidates_cache(library):
    path,_=library
    bid=core.list_books()[0]['id']
    core.search('turning somebody into rock',bid,rerank=False)
    assert core.search_cache.get(('turning somebody into rock',bid,5,1500,False,0)) is not None
    core.ingest_file(path,force=True)
    assert core.search_cache.get(('turning somebody into rock',bid,5,1500,False,0)) is None


def test_search_skips_qdrant_sync_roundtrip_when_current(library,monkeypatch):
    monkeypatch.setattr(vector_index,'ensure',lambda *a:pytest.fail('ensure() called on hot path'))
    monkeypatch.setattr(core,'sync_index',lambda *a,**k:pytest.fail('sync on hot path'))
    assert core.search('basilisk petrify',rerank=False,top_k=1)['results']


def test_search_repairs_lost_collection(library):
    collection,_=core.vector_config()
    vector_index.delete_collection(collection)
    core.search_cache.clear()
    found=core.search('basilisk petrify',rerank=False,top_k=1)
    assert found['results'][0]['page']==2
    assert core.index_status()['ready']


def test_rerank_pool_is_bounded(library,monkeypatch):
    fake=FakeReranker(lambda q,t:0.0)
    monkeypatch.setattr(core,'reranker',lambda:fake)
    monkeypatch.setattr(core.ranking,'RERANK_CANDIDATES',5)
    core.search("wizard concentrate spell",rerank=True)
    assert len(fake.calls[0])<=5


def test_weak_matches_are_omitted_not_padded(library,monkeypatch):
    # Reranker says only the petrify page is relevant; the rest must not fill top_k.
    fake=FakeReranker(lambda q,t:9.0 if 'petrify' in t else -9.0)
    monkeypatch.setattr(core,'reranker',lambda:fake)
    found=core.search('what turns creatures to stone',top_k=5,token_budget=4000)
    assert found['results'] and all(r['page']==2 for r in found['results'])
    assert found['weak_matches_omitted'] is True


def test_best_hit_survives_when_everything_scores_low(library,monkeypatch):
    fake=FakeReranker(lambda q,t:-12.0 if 'petrify' not in t else -5.0)
    monkeypatch.setattr(core,'reranker',lambda:fake)
    found=core.search('anything',top_k=3,token_budget=4000)
    assert found['results'] and found['results'][0]['page']==2


def test_warmup_loads_both_models(monkeypatch):
    calls=[]
    class Embed:
        def encode(self,*a,**k): calls.append('embed')
    class Rerank:
        def predict(self,*a,**k): calls.append('rerank')
    monkeypatch.setattr(core,'embedder',lambda:Embed())
    monkeypatch.setattr(core,'reranker',lambda:Rerank())
    core.warmup()
    assert calls==['embed','rerank']


def test_qdrant_client_is_reused():
    assert vector_index.client() is vector_index.client()


def test_neighbors_add_same_page_context_within_budget(library):
    bid=core.list_books()[0]['id']
    plain=core.search('basilisk petrify',bid,top_k=1,token_budget=2000,rerank=False)
    assert 'context' not in plain['results'][0]
    rich=core.search('basilisk petrify',bid,top_k=1,token_budget=2000,rerank=False,neighbors=2)
    hit=rich['results'][0]
    assert hit['context'], 'sample page has several chunks'
    assert all(c['id']!=hit['id'] for c in hit['context'])
    expanded={c['id'] for c in core.read_chunk(hit['id'],2)['chunks']}
    assert {c['id'] for c in hit['context']}<=expanded
    assert rich['text_tokens']==sum(r['text_tokens']+sum(c['text_tokens'] for c in r['context']) for r in rich['results'])
    assert rich['text_tokens']<=2000


def test_neighbors_never_exceed_budget(library):
    bid=core.list_books()[0]['id']
    tight=core.search('basilisk petrify',bid,top_k=1,token_budget=128,rerank=False,neighbors=2)
    assert tight['text_tokens']<=128


def test_neighbors_are_part_of_cache_key(library):
    bid=core.list_books()[0]['id']
    core.search('basilisk petrify',bid,top_k=1,rerank=False)
    assert 'context' in core.search('basilisk petrify',bid,top_k=1,rerank=False,neighbors=1)['results'][0]


def test_rest_neighbors_validation(library):
    from contextlib import closing
    from fastapi.testclient import TestClient
    from app.main import app
    with closing(TestClient(app)) as client:  # no lifespan: the MCP session manager can start once per process
        assert client.post('/search',json={'query':'basilisk','neighbors':3}).status_code==422
        body=client.post('/search',json={'query':'basilisk petrify','neighbors':1,'rerank':False,'top_k':1}).json()
        assert 'context' in body['results'][0]


def test_onnx_failure_falls_back_to_pytorch(monkeypatch):
    built=[]
    class Fake:
        def __init__(self,name,**kwargs):
            built.append(kwargs)
            if 'backend' in kwargs:
                raise RuntimeError('optimum not installed')
    monkeypatch.setattr(core,'CrossEncoder',Fake)
    monkeypatch.setenv('RERANK_BACKEND','onnx')
    core.reranker.cache_clear()
    try:
        assert isinstance(core.reranker(),Fake)
    finally:
        core.reranker.cache_clear()
    assert 'backend' in built[0] and 'backend' not in built[1]
