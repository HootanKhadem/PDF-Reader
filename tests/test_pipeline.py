"""Real model + SQLite integration tests. No external API stand-ins."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app import core
from app.main import app


def test_ingestion_idempotency_and_provenance(library):
    path,result=library
    assert result['pages']==2 and result['chunks']>2
    assert core.ingest_file(path)['status']=='unchanged'
    books=core.list_books()
    assert len(books)==1
    with core.db() as con:
        before=con.execute('SELECT count(*) FROM chunks').fetchone()[0]
        ids=[r[0] for r in con.execute('SELECT id FROM chunks')]
    assert before==len(set(ids))
    with path.open('wb') as src:
        src.write(b'broken pdf')
    with pytest.raises(Exception):
        core.ingest_file(path)
    assert core.list_books()[0]['chunks']==before


def test_semantic_budget_filter_and_expansion(library):
    bid=core.list_books()[0]['id']
    found=core.search('keeping magic active after being hurt',bid,token_budget=128)
    assert found['results'][0]['page']==1
    assert 0<found['text_tokens']<=128
    assert sum(len(core.token_ids(r['text'])) for r in found['results'])<=128
    hit=found['results'][0]
    assert hit['book_id']==bid and '#page=1' in hit['source_url']
    expanded=core.read_chunk(hit['id'],2)
    assert all(c['book_id']==bid and c['page']==1 for c in expanded['chunks'])
    result=core.search('turning somebody into rock',bid,rerank=False)
    assert result['results'][0]['page']==2
    assert core.read_page(bid,1,128)['truncated']


def test_rest_validation_errors_and_contract(library):
    with TestClient(app) as client:
        assert client.get('/health').status_code==200
        for body in ({'query':'x','token_budget':0},{'query':'   '},{'query':'x','top_k':21}):
            assert client.post('/search',json=body).status_code==422
        assert client.post('/search',json={'query':'x','book_id':'missing'}).status_code==404
        assert client.get('/chunks/missing').status_code==404
        assert client.get('/chunks/missing?neighbors=3').status_code==422
        assert client.get('/books/missing/pages/1').status_code==404
        response=client.post('/search',json={'query':'" OR * DROP TABLE books; --','rerank':False})
        assert response.status_code==200
        assert len(client.get('/books').json())==1
        assert '/search' in client.get('/openapi.json').json()['paths']
        assert client.get('/index').json()['ready']
        assert client.post('/index/sync').status_code==202
        assert client.get('/ingest').json()['phase']=='completed'
        assert client.get('/index').json()['ready']


def test_chunk_coverage_and_size():
    text=('An unusual concept with punctuation.\n\n'*300)+'Final sentence.'
    chunks=list(core.split_text(text,core.embedder().tokenizer))
    assert chunks[0][0]==0 and chunks[-1][1]==len(text)
    assert all(len(core.token_ids(c[2]))<=320 for c in chunks)
    assert all(b[0]<=a[1] for a,b in zip(chunks,chunks[1:]))


def test_scanned_page_ocr():
    with pymupdf.open() as source, pymupdf.open() as scan:
        page=source.new_page()
        page.insert_text((70,100),'A basilisk can turn a creature into stone.',fontsize=20)
        image=page.get_pixmap(matrix=pymupdf.Matrix(2,2))
        scanned=scan.new_page()
        scanned.insert_image(scanned.rect,stream=image.tobytes('png'))
        assert not scanned.get_text().strip()
        text,used_ocr=core.extract_page(scanned)
        assert used_ocr and 'basilisk' in text.lower() and 'stone' in text.lower()


def test_search_during_ingestion(library):
    path,_=library
    second=path.with_name('second.pdf')
    second.write_bytes(path.read_bytes())
    def queries():
        for _ in range(8):
            result=core.search('sustaining magic after injury',token_budget=128,rerank=False)
            assert result['results'] and result['text_tokens']<=128
    with ThreadPoolExecutor(max_workers=2) as pool:
        ingest=pool.submit(core.ingest_file,second)
        query=pool.submit(queries)
        assert ingest.result()['status']=='indexed'
        query.result()
    assert len(core.list_books())==2
