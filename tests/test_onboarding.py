from contextlib import closing

import pymupdf
from fastapi.testclient import TestClient

from app import core
from app.main import app


def test_empty_library_prompts_upload_without_indexing(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'BOOKS',tmp_path/'books')
    with closing(TestClient(app)) as client:
        result=client.get('/library/status')
        assert result.status_code==200
        data=result.json()
        assert data['state']=='empty' and data['suggested_action']=='upload'
        assert data['indexed_books']==data['source_pdfs']==0
        assert 'Upload a PDF' in data['message']
        assert client.get('/books').json()==[]
        assert client.post('/search',json={'query':'anything'}).json()['results']==[]


def test_available_books_prompt_indexing_and_ignore_other_files(tmp_path,monkeypatch):
    directory=tmp_path/'books'
    directory.mkdir()
    monkeypatch.setattr(core,'BOOKS',directory)
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(directory/'New material.PDF')
    (directory/'notes.txt').write_text('Not a PDF')
    (tmp_path/'outside.pdf').write_bytes(b'Not in books/')
    with closing(TestClient(app)) as client:
        data=client.get('/library/status').json()
        assert data['state']=='needs_indexing' and data['suggested_action']=='ingest'
        assert data['source_pdfs']==data['unindexed_pdfs']==1
        assert data['indexed_books']==0


def test_indexed_library_reports_new_material_without_blocking_search(library):
    path,_=library
    with closing(TestClient(app)) as client:
        assert client.get('/library/status').json()['message'] is None
        path.with_name('Another book.pdf').write_bytes(path.read_bytes())
        data=client.get('/library/status').json()
        assert data['state']=='ready' and data['indexed_books']==1
        assert data['source_pdfs']==2 and data['unindexed_pdfs']==1
        assert data['suggested_action']=='ingest'


def test_missing_originals_do_not_hide_existing_index(library):
    path,_=library
    path.unlink()  # Generated fixture only; indexed text must remain available.
    with closing(TestClient(app)) as client:
        data=client.get('/library/status').json()
        assert data['state']=='ready' and data['indexed_books']==1
        assert data['source_pdfs']==0 and data['missing_source_pdfs']==1
        assert data['suggested_action']=='upload'
        assert len(client.get('/books').json())==1
