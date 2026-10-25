"""Disposable live-upload probe. Run create, recreate container, verify, cleanup."""
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import pymupdf
from app import core

BASE='http://localhost:8000'
RECEIPT=core.DATA/'upload-check-receipt.json'


def verify(receipt):
    with httpx.Client(timeout=120) as client:
        books=client.get(BASE+'/books').json()
        book=next(b for b in books if b['id']==receipt['id'])
        assert book['filename']==receipt['filename']
        response=client.get(BASE+f"/books/{book['id']}/pdf")
        response.raise_for_status()
        assert hashlib.sha256(response.content).hexdigest()==receipt['sha256']
        found=client.post(BASE+'/search',json={'query':'navigating unknown territory with a compass',
            'book_id':book['id'],'token_budget':300,'rerank':False})
        found.raise_for_status()
        assert found.json()['results'] and found.json()['text_tokens']<=300
        assert found.json()['retrieval_backend']=='qdrant'
        index=client.get(BASE+'/index').json()
        assert index['ready'] and index['sqlite_chunks']==index['qdrant_points']
    print('Uploaded PDF, metadata, searchable index, and source bytes verified.',flush=True)


def main(mode):
    if mode=='create':
        assert not RECEIPT.exists(),'Clean up existing test receipt first'
        name='upload-check-'+uuid.uuid4().hex+'.pdf'
        with pymupdf.open() as doc:
            page=doc.new_page()
            remaining=page.insert_textbox(pymupdf.Rect(50,50,550,750),
                'A compass helps explorers navigate unfamiliar terrain and find a route home. '*10)
            assert remaining>=0
            pdf=doc.tobytes()
        with httpx.Client(timeout=120) as client:
            assert all(b['filename']!=name for b in client.get(BASE+'/books').json())
            response=client.post(BASE+'/books/upload',files={'file':(name,pdf,'application/pdf')})
            assert response.status_code==202,response.text
            for _ in range(120):
                status=client.get(BASE+'/ingest').json()
                if not status['running']:
                    break
                time.sleep(.5)
            assert status['phase']=='completed',status
            assert len(status['results'])==1 and status['results'][0]['file']==name
            book=next(b for b in client.get(BASE+'/books').json() if b['filename']==name)
        receipt={'filename':name,'id':book['id'],'sha256':hashlib.sha256(pdf).hexdigest()}
        RECEIPT.write_text(json.dumps(receipt),encoding='utf-8')
        verify(receipt)
        print('Ready for container recreation; receipt saved in persistent data volume.',flush=True)
    elif mode=='verify':
        verify(json.loads(RECEIPT.read_text(encoding='utf-8')))
    elif mode=='cleanup':
        receipt=json.loads(RECEIPT.read_text(encoding='utf-8'))
        name=receipt['filename']
        assert name.startswith('upload-check-') and name==Path(name).name
        target=core.UPLOADS/name
        assert target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==receipt['sha256']
        with core.db() as con:
            book=con.execute('SELECT filename FROM books WHERE id=?',(receipt['id'],)).fetchone()
            assert book and book['filename']==name
            con.execute('DELETE FROM chunks_fts WHERE id IN (SELECT id FROM chunks WHERE book_id=?)',(receipt['id'],))
            for table in ('chunks','pages'):
                con.execute(f'DELETE FROM {table} WHERE book_id=?',(receipt['id'],))
            con.execute('DELETE FROM books WHERE id=?',(receipt['id'],))
        core.sync_index(prune=True)
        target.unlink()
        RECEIPT.unlink()
        print('Only the owned upload-check fixture and its derived index were removed.',flush=True)
    else:
        raise ValueError('Use create, verify, or cleanup')


if __name__=='__main__':
    main(sys.argv[1])
