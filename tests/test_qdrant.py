import re
from contextlib import closing

import pymupdf
from fastapi.testclient import TestClient
from app import core,vector_index
from app.main import app


def test_migrate_recover_missing_collection_without_embedding(library,monkeypatch):
    collection,_=core.vector_config()
    vector_index.delete_collection(collection)
    with monkeypatch.context() as patch:
        def no_embedding(*args,**kwargs):
            raise AssertionError('Migration must reuse SQLite embeddings')
        patch.setattr(core.embedder(),'encode',no_embedding)
        assert core.sync_index()['synced_books']==1
    status=core.index_status()
    assert status['ready'] and status['qdrant_points']==status['sqlite_chunks']
    assert core.sync_index()['synced_books']==0
    assert core.search('holding magic after injury',rerank=False)['retrieval_backend']=='qdrant'


def test_replacement_removes_obsolete_generations(library):
    path,_=library
    with core.db() as con:
        old={r[0] for r in con.execute('SELECT id FROM chunks')}
    with pymupdf.open() as doc:
        p=doc.new_page()
        p.insert_textbox(pymupdf.Rect(50,50,550,750),'A compass helps explorers navigate unfamiliar territory. '*15)
        doc.save(path)
    assert core.ingest_file(path)['status']=='indexed'
    with core.db() as con:
        current={r[0] for r in con.execute('SELECT id FROM chunks')}
    collection,_=core.vector_config()
    points=vector_index.request('POST',f'/collections/{collection}/points/scroll',
                                {'limit':100,'with_payload':True,'with_vector':False})['points']
    indexed={p['payload']['chunk_id'] for p in points}
    assert indexed==current and not indexed.intersection(old)


def test_real_connection_failure_keeps_pending_data_then_recovers(library,monkeypatch):
    path,_=library
    with monkeypatch.context() as patch:
        patch.setattr(vector_index,'URL','http://127.0.0.1:1')
        result=core.ingest_file(path,force=True)
        assert result['status']=='index_pending'
        with closing(TestClient(app)) as client:
            assert client.get('/books').status_code==200
            assert client.post('/search',json={'query':'magical focus','rerank':False}).status_code==503
        assert not core.index_status()['ready']
    assert core.sync_index()['synced_books']==1
    assert core.index_status()['ready']
    assert core.search('magical focus',rerank=False)['results']


def test_search_fetches_candidates_not_all_sqlite_vectors(library,monkeypatch):
    trace=[]
    original=core.db
    def traced_db():
        con=original()
        con.set_trace_callback(trace.append)
        return con
    monkeypatch.setattr(core,'db',traced_db)
    result=core.search('magical focus',rerank=False)
    assert result['results'] and result['retrieval_backend']=='qdrant'
    selects=[s.lower() for s in trace if s.lstrip().lower().startswith('select')]
    assert not any(re.search(r'\bvector\b',s) and 'length(vector)' not in s for s in selects)
    assert any('where c.id in (' in s for s in selects)
