import pymupdf
from contextlib import closing
from fastapi.testclient import TestClient

from app import core
from app import main


def test_upload_indexes_and_serves_pdf_with_validation(tmp_path,monkeypatch):
    root=tmp_path/'root'
    root.mkdir()
    monkeypatch.setattr(core,'BOOKS',root)
    monkeypatch.setattr(core,'UPLOADS',tmp_path/'uploads')
    monkeypatch.setattr(core,'DATA',tmp_path/'data')
    main.state.update(running=False,phase='idle',results=[])
    with pymupdf.open() as doc:
        page=doc.new_page()
        page.insert_textbox(pymupdf.Rect(50,50,550,750),
            'A compass helps explorers navigate unfamiliar terrain and find their way home. '*10)
        content=doc.tobytes()
    # REST-only client: the live protocol evaluation owns MCP lifecycle checks.
    with closing(TestClient(main.app)) as client:
        assert client.post('/books/upload',files={'file':('bad.txt',content)}).status_code==422
        assert client.post('/books/upload',files={'file':('../bad.pdf',content)}).status_code==422
        assert client.post('/books/upload',files={'file':('bad.pdf',b'not a PDF')}).status_code==400
        assert not list(core.UPLOADS.glob('*.part'))
        # Uploading a new book must not rescan every older source PDF.
        (root/'OtherExisting.pdf').write_bytes(b'bad existing source')
        response=client.post('/books/upload',files={'file':('Navigation.pdf',content,'application/pdf')})
        assert response.status_code==202,response.text
        assert client.get('/ingest').json()['phase']=='completed'
        books=client.get('/books').json()
        assert len(books)==1 and books[0]['filename']=='Navigation.pdf'
        bid=books[0]['id']
        assert client.get(f'/books/{bid}/pdf').content==content
        found=client.post('/search',json={'query':'finding a route in unknown country','book_id':bid,'rerank':False})
        assert found.status_code==200 and found.json()['results']
        assert client.post('/books/upload',files={'file':('Navigation.pdf',content)}).status_code==409
        main.state['running']=True
        assert client.post('/books/upload',files={'file':('Other.pdf',content)}).status_code==409
        main.state['running']=False
        monkeypatch.setenv('MAX_UPLOAD_MB','0')
        assert client.post('/books/upload',files={'file':('TooBig.pdf',content)}).status_code==413
        assert not main.state['running']
        assert not list(core.UPLOADS.glob('*.part'))
