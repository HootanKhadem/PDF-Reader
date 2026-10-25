"""Local, transactional PDF ingestion and bounded hybrid retrieval."""
import hashlib
import json
import os
import re
import sqlite3
import threading
import multiprocessing
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

import numpy as np
import pymupdf
from sentence_transformers import CrossEncoder, SentenceTransformer
from . import extraction, vector_index

DATA = Path(os.getenv('DATA_DIR', '/data'))
BOOKS = Path(os.getenv('BOOKS_DIR', '/books'))
UPLOADS = Path(os.getenv('BOOK_UPLOAD_DIR', '/uploads'))
MODEL = os.getenv('EMBEDDING_MODEL', 'intfloat/multilingual-e5-small')
RERANK = os.getenv('RERANK_MODEL', 'cross-encoder/ms-marco-MiniLM-L-6-v2')
OCR_KEY = '-ocr-' + os.getenv('OCR_LANGUAGE', 'eng') + '-' + os.getenv('FORCE_OCR', '0')
EXTRACTION_KEY='-'+extraction.VERSION+OCR_KEY
PIPELINE = 'v3-heading-320-48-' + MODEL + EXTRACTION_KEY
model_lock = threading.RLock()
index_lock = threading.RLock()


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


@lru_cache(maxsize=1)
def embedder():
    with model_lock:
        return SentenceTransformer(MODEL, device='cpu')


@lru_cache(maxsize=1)
def reranker():
    with model_lock:
        return CrossEncoder(RERANK, device='cpu', max_length=512)


def db():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA / 'library.db', timeout=60, factory=ClosingConnection)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript('''
      CREATE TABLE IF NOT EXISTS books (
        id TEXT PRIMARY KEY, title TEXT, filename TEXT, fingerprint TEXT,
        pages INTEGER, chunks INTEGER, warnings TEXT);
      CREATE TABLE IF NOT EXISTS pages (
        book_id TEXT, page INTEGER, label TEXT, section TEXT, text TEXT,
        PRIMARY KEY(book_id,page));
      CREATE TABLE IF NOT EXISTS chunks (
        id TEXT PRIMARY KEY, book_id TEXT, page INTEGER, label TEXT,
        section TEXT, ordinal INTEGER, start INTEGER, end INTEGER,
        text TEXT, vector BLOB);
      CREATE INDEX IF NOT EXISTS chunks_book ON chunks(book_id,page,ordinal);
      CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(id UNINDEXED,text, tokenize='unicode61');
      CREATE TABLE IF NOT EXISTS library_metadata (key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE IF NOT EXISTS vector_state (book_id TEXT PRIMARY KEY,fingerprint TEXT,collection TEXT);
    ''')
    if not con.execute("SELECT 1 FROM library_metadata WHERE key='library_id'").fetchone():
        con.execute('INSERT OR IGNORE INTO library_metadata VALUES (?,?)',('library_id',uuid.uuid4().hex))
        con.commit()
    return con


def vector_config():
    with db() as con:
        identity=con.execute("SELECT value FROM library_metadata WHERE key='library_id'").fetchone()[0]
        size=con.execute('SELECT length(vector) FROM chunks LIMIT 1').fetchone()
    dimension=size[0]//4 if size else 384
    model_hash=hashlib.sha256(MODEL.encode()).hexdigest()[:12]
    return f'books_{identity}_{model_hash}_{dimension}',dimension


def sync_index(force=False,prune=False):
    """Repair the derivative index from durable SQLite state, without re-embedding."""
    with index_lock:
        collection,dimension=vector_config()
        created=vector_index.ensure(collection,dimension)
        with db() as con:
            if created:
                con.execute('DELETE FROM vector_state WHERE collection=?',(collection,))
            books=con.execute('SELECT b.id,b.fingerprint,s.fingerprint AS synced,s.collection '
                'FROM books b LEFT JOIN vector_state s ON s.book_id=b.id').fetchall()
        repaired=0
        for book in books:
            if not force and book['synced']==book['fingerprint'] and book['collection']==collection:
                continue
            model=re.fullmatch(r'[0-9a-f]{64}:v\d+-heading-\d+-\d+-(.+)-v\d+-confidence-columns(?:-cells)?-ocr-.+',book['fingerprint'])
            if not model or model.group(1)!=MODEL:
                raise vector_index.IndexUnavailable('Saved embeddings use a different model. Re-index all books with the configured model.')
            with db() as con:
                rows=con.execute('SELECT id,vector FROM chunks WHERE book_id=?',(book['id'],)).fetchall()
            vector_index.replace_book(collection,book['id'],book['fingerprint'],rows)
            with db() as con:
                con.execute('INSERT OR REPLACE INTO vector_state VALUES (?,?,?)',
                            (book['id'],book['fingerprint'],collection))
            repaired+=1
        if created or repaired or prune or force:
            vector_index.prune(collection,[b['id'] for b in books])
            with db() as con:
                con.execute('DELETE FROM vector_state WHERE book_id NOT IN (SELECT id FROM books)')
        return {'collection':collection,'synced_books':repaired}


def index_status():
    collection,_=vector_config()
    with db() as con:
        count=con.execute('SELECT count(*) FROM chunks').fetchone()[0]
        pending=con.execute('SELECT count(*) FROM books b LEFT JOIN vector_state s ON s.book_id=b.id '
            'WHERE s.fingerprint IS NULL OR s.fingerprint!=b.fingerprint OR s.collection!=?',(collection,)).fetchone()[0]
    try:
        info=vector_index.info(collection)
    except vector_index.IndexUnavailable as exc:
        return {'backend':'qdrant','ready':False,'collection':collection,'pending_books':pending,'error':str(exc)}
    points=info.get('points_count',0) if info else 0
    return {'backend':'qdrant','ready':info is not None and pending==0 and points==count,
            'collection':collection,'pending_books':pending,'sqlite_chunks':count,
            'qdrant_points':points,'indexed_vectors':info.get('indexed_vectors_count',0) if info else 0,
            'status':info.get('status') if info else 'missing'}


def list_books():
    with db() as con:
        return [dict(r) | {'warnings': json.loads(r['warnings'])} for r in con.execute(
            'SELECT * FROM books ORDER BY title')]


def source_files():
    """PDFs in the private books mount and persistent upload storage."""
    files=[]
    for root in (BOOKS,UPLOADS):
        if root.is_dir():
            files.extend(p for p in root.iterdir() if p.is_file() and p.suffix.lower()=='.pdf')
    return sorted(files,key=lambda p:p.name)


def library_status():
    with db() as con:
        indexed={r['filename'] for r in con.execute('SELECT filename FROM books')}
    sources=source_files()
    names={p.name for p in sources}
    new=len(names-indexed)
    missing=len(indexed-names)
    result={'state':'ready','indexed_books':len(indexed),'source_pdfs':len(sources),
            'unindexed_pdfs':new,'missing_source_pdfs':missing,'message':None,'suggested_action':None}
    if not indexed and not sources:
        result.update(state='empty',message='Your library is empty. Upload a PDF to make its contents searchable.',suggested_action='upload')
    elif not indexed:
        result.update(state='needs_indexing',message='PDFs were found. Choose Re-index books to prepare them for search.',suggested_action='ingest')
    elif new:
        result.update(message='New PDFs are waiting. Choose Re-index books to include them in search.',suggested_action='ingest')
    elif missing:
        result.update(message='Saved passages are searchable, but some original PDFs are missing. Upload the originals or place them in books/ to restore page links.',suggested_action='upload')
    return result


def clean(text):
    text = text.replace('\x00', '').replace('\u00ad', '')
    text = re.sub(r'(\w)-\n(?=[a-z])', r'\1', text)
    return re.sub(r'[ \t]+', ' ', text).strip()


def extract_page(page):
    text,ocr,_=extraction.extract(page)
    return text,ocr


def source_path(filename):
    for root in (BOOKS,UPLOADS):
        path=root/filename
        if path.is_file():
            return path
    raise KeyError('Source PDF unavailable')


def split_text(text, tokenizer, size=320, overlap=48):
    """Keep display headings and their concept text together where possible."""
    boundaries=[0]
    for match in re.finditer(r'^.+$',text,re.MULTILINE):
        line=match.group().strip()
        letters=[c for c in line if c.isalpha()]
        if 4<=len(letters) and len(line)<=80 and all(c.isupper() for c in letters):
            if match.start()>0:
                boundaries.append(match.start())
    boundaries.append(len(text))
    for a,b in zip(boundaries,boundaries[1:]):
        for start,end,chunk in _windows(text[a:b],tokenizer,size,overlap):
            yield a+start,a+end,chunk


def _windows(text, tokenizer, size=320, overlap=48):
    """Original character slices: no decode/re-encode whitespace damage."""
    with model_lock:
        offsets = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True, verbose=False)['offset_mapping']
    start = 0
    while start < len(offsets):
        end = min(start + size, len(offsets))
        # Prefer paragraph or sentence end in last quarter, while retaining hard cap.
        if end < len(offsets):
            for i in range(end-1, start + int(size*.75), -1):
                tail = text[offsets[i][1]:offsets[i+1][0]]
                if '\n' in tail or text[offsets[i][0]:offsets[i][1]] in {'.', '!', '?'}:
                    end = i+1
                    break
        a, b = offsets[start][0], offsets[end-1][1]
        yield a, b, text[a:b]
        if end == len(offsets):
            break
        next_start = max(start+1, end-overlap)
        while next_start>start+1 and offsets[next_start][0]>0 and text[offsets[next_start][0]-1].isalnum() and text[offsets[next_start][0]].isalnum():
            next_start-=1
        start=next_start


def ingest_file(path, progress=lambda **kw: None, force=False):
    book_id = hashlib.sha256(path.name.encode()).hexdigest()[:16]
    digest = hashlib.sha256()
    with path.open('rb') as src:
        for block in iter(lambda: src.read(1024*1024), b''):
            digest.update(block)
    fingerprint = digest.hexdigest() + ':' + PIPELINE
    ocr_config=hashlib.sha256(('tsv250-psm3-v1-'+os.getenv('OCR_LANGUAGE','eng')).encode()).hexdigest()[:12]
    ocr_cache=DATA/'ocr-cache'/f'{digest.hexdigest()}-{ocr_config}'
    with db() as con:
        old = con.execute('SELECT fingerprint,warnings FROM books WHERE id=?', (book_id,)).fetchone()
        cached_pages={}
        cached_vectors={}
        if not force and old and old['fingerprint']==fingerprint:
            try:
                sync_index()
            except vector_index.IndexUnavailable as exc:
                return {'file':path.name,'status':'index_pending','error':str(exc)}
            return {'file':path.name,'status':'unchanged'}
        if not force and old and old['fingerprint'].startswith(digest.hexdigest()+':') and old['fingerprint'].endswith(EXTRACTION_KEY):
            cached_pages={r['page']:dict(r) for r in con.execute('SELECT * FROM pages WHERE book_id=?',(book_id,))}
        previous_model=re.fullmatch(r'[0-9a-f]{64}:v\d+-heading-\d+-\d+-(.+)-v\d+-confidence-columns(?:-cells)?-ocr-.+',old['fingerprint']) if old else None
        if previous_model and previous_model.group(1)==MODEL:
            cached_vectors={r['text']:r['vector'] for r in con.execute('SELECT text,vector FROM chunks WHERE book_id=?',(book_id,))}
    tokenizer = embedder().tokenizer
    pages, chunks, warnings = [], [], []
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise ValueError('Encrypted PDF: provide a decrypted copy')
        toc = doc.get_toc()
        title = (doc.metadata or {}).get('title') or path.stem.replace('_', ' ')
        section = ''
        ocr_pages, low_pages, filtered_pages = [], [], []
        if cached_pages:
            filtered_pages=next((w['pages'] for w in json.loads(old['warnings']) if w['type']=='text_filtered_pages'),[])
        extracted={}
        workers=min(8,max(1,int(os.getenv('OCR_WORKERS','4'))))
        if not cached_pages and len(doc)>=12 and workers>1:
            progress(file=path.name,phase='extracting',page=0,total_pages=len(doc))
            with ProcessPoolExecutor(max_workers=workers,mp_context=multiprocessing.get_context('spawn'),
                    initializer=extraction.init_worker,initargs=(str(path),str(ocr_cache))) as pool:
                jobs=[pool.submit(extraction.extract_number,i) for i in range(len(doc))]
                for completed,job in enumerate(as_completed(jobs),1):
                    idx,result=job.result()
                    extracted[idx]=result
                    progress(file=path.name,phase='extracting',page=completed,total_pages=len(doc))
        cached_ocr = next((w['pages'] for w in json.loads(old['warnings']) if w['type']=='ocr_pages'),[]) if cached_pages else []
        for i, page in enumerate(doc):
            if not extracted:
                progress(file=path.name, phase='extracting', page=i+1, total_pages=len(doc))
            headings = [row[1] for row in toc if row[2] == i+1]
            if headings:
                section = ' / '.join(headings)
            if i+1 in cached_pages:
                text=cached_pages[i+1]['text']
                used_ocr=i+1 in cached_ocr
            else:
                text,used_ocr,info=extracted[i] if extracted else extraction.extract(page,ocr_cache/f'{i}.tsv')
                if info['discarded_blocks']:
                    filtered_pages.append(i+1)
            if used_ocr:
                ocr_pages.append(i+1)
            if len(re.findall(r'\b[^\W\d_]{3,}\b', text)) < 12:
                low_pages.append(i+1)
            label = page.get_label() or str(i+1)
            pages.append((book_id, i+1, label, section, text))
            for ordinal, (a,b,chunk) in enumerate(split_text(text, tokenizer)):
                if not extraction.readable(chunk):
                    continue
                cid = hashlib.sha256(f'{book_id}:{fingerprint}:{i}:{ordinal}'.encode()).hexdigest()[:24]
                chunks.append((cid, book_id, i+1, label, section, ordinal, a, b, chunk))
        count = len(doc)
    warnings.append({'type':'ocr_pages', 'pages':ocr_pages})
    warnings.append({'type':'low_text_pages', 'pages':low_pages})
    warnings.append({'type':'text_filtered_pages','pages':filtered_pages})
    if not chunks:
        raise ValueError('No searchable text extracted')
    vectors=[cached_vectors.get(c[8]) for c in chunks]
    pending=[i for i,v in enumerate(vectors) if v is None]
    reused=len(chunks)-len(pending)
    progress(file=path.name,phase='embedding',completed_chunks=reused,total_chunks=len(chunks),reused_chunks=reused)
    for start in range(0,len(pending),64):
        indices=pending[start:start+64]
        progress(file=path.name, phase='embedding', completed_chunks=reused+start, total_chunks=len(chunks),reused_chunks=reused)
        with model_lock:
            batch = embedder().encode(['passage: ' + chunks[i][8] for i in indices],
                normalize_embeddings=True, batch_size=16, show_progress_bar=False)
        for i,v in zip(indices,batch):
            vectors[i]=v.astype(np.float32).tobytes()
    # Entire replacement commits atomically; readers retain old book until ready.
    result={'file':path.name,'status':'indexed','pages':count,'chunks':len(chunks),'warnings':warnings}
    with index_lock:
        with db() as con:
            con.execute('DELETE FROM chunks_fts WHERE id IN (SELECT id FROM chunks WHERE book_id=?)', (book_id,))
            con.execute('DELETE FROM chunks WHERE book_id=?', (book_id,))
            con.execute('DELETE FROM pages WHERE book_id=?', (book_id,))
            con.execute('INSERT OR REPLACE INTO books VALUES (?,?,?,?,?,?,?)',
                        (book_id,title,path.name,fingerprint,count,len(chunks),json.dumps(warnings)))
            con.executemany('INSERT INTO pages VALUES (?,?,?,?,?)',pages)
            con.executemany('INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?)', [c+(v,) for c,v in zip(chunks,vectors)])
            con.executemany('INSERT INTO chunks_fts VALUES (?,?)',[(c[0],c[8]) for c in chunks])
            con.execute('DELETE FROM vector_state WHERE book_id=?',(book_id,))
        try:
            sync_index()
        except vector_index.IndexUnavailable as exc:
            result.update(status='index_pending',error=str(exc))
    return result


def token_ids(text):
    with model_lock:
        return embedder().tokenizer(text, add_special_tokens=False, verbose=False)['input_ids']


def truncate(text, budget):
    tok = embedder().tokenizer
    with model_lock:
        data = tok(text, add_special_tokens=False, return_offsets_mapping=True, verbose=False)
    if len(data['input_ids']) <= budget:
        return text, len(data['input_ids']), False
    excerpt = text[:data['offset_mapping'][budget-1][1]]
    # Token count after cutting may differ at a word boundary. Enforce actual count.
    while len(token_ids(excerpt)) > budget:
        excerpt = excerpt[:-1]
    return excerpt, len(token_ids(excerpt)), True


def public_chunk(row):
    return {k:row[k] for k in ('id','book_id','page','label','section','ordinal','text')} | {
        'source_url':f"/books/{row['book_id']}/pdf#page={row['page']}"}


def retrieve(query,qvec,book_id):
    with index_lock:
        collection=sync_index()['collection']
        dense=vector_index.query(collection,qvec,book_id,100)
        with db() as con:
            con.execute('BEGIN')
            stopwords=set('a an the of in on at to for and or is are be been being was were it its that this these those i me my we you your can could how what when where which who with without after before from into while as by do does did'.split())
            terms=[t for t in re.findall(r'\w+',query,re.UNICODE) if t.lower() not in stopwords][:32]
            lexical=[]
            if terms:
                expr=' OR '.join('"'+t+'"'+('*' if len(t)>=4 else '') for t in terms)
                lexical=con.execute('SELECT f.id FROM chunks_fts f JOIN chunks c ON c.id=f.id '
                    'WHERE chunks_fts MATCH ?'+(' AND c.book_id=?' if book_id else '')+
                    ' ORDER BY bm25(chunks_fts) LIMIT 100',(expr,book_id) if book_id else (expr,)).fetchall()
            fused={}
            for ranked in (dense,[r['id'] for r in lexical]):
                for rank,cid in enumerate(ranked):
                    fused[cid]=fused.get(cid,0)+1/(60+rank+1)
            candidates=sorted(fused,key=lambda cid:-fused[cid])[:100]
            if not candidates:
                return {},{},[]
            marks=','.join('?' for _ in candidates)
            selected=con.execute('SELECT c.id,c.book_id,c.page,c.label,c.section,c.ordinal,c.start,c.end,c.text,b.title '
                f'FROM chunks c JOIN books b ON b.id=c.book_id WHERE c.id IN ({marks})',candidates).fetchall()
            rows={r['id']:r for r in selected}
            return rows,fused,[cid for cid in candidates if cid in rows]


def search(query, book_id=None, top_k=5, token_budget=1500, rerank=True):
    with db() as con:
        if book_id and not con.execute('SELECT 1 FROM books WHERE id=?',(book_id,)).fetchone():
            raise KeyError('Unknown book')
        has_chunks=con.execute('SELECT 1 FROM chunks LIMIT 1').fetchone()
    if not has_chunks:
        return {'results':[], 'text_tokens':0, 'token_budget':token_budget,'retrieval_backend':'qdrant'}
    with model_lock:
        qvec=embedder().encode('query: '+query,normalize_embeddings=True)
    rows,fused,candidates=retrieve(query,qvec,book_id)
    original=candidates.copy()
    if not candidates:
        return {'results':[], 'text_tokens':0, 'token_budget':token_budget,'retrieval_backend':'qdrant'}
    if rerank:
        with model_lock:
            ranks = reranker().predict([(query,rows[i]['text']) for i in candidates],batch_size=16,show_progress_bar=False)
        candidates = [i for _,i in sorted(zip(ranks,candidates),reverse=True)]
        relevance = {i:float(s) for s,i in zip(ranks,original)}
    else:
        relevance = fused
    results, used, selected = [], 0, []
    for i in candidates:
        row = rows[i]
        # Avoid sending overlapping chunks from the same page twice.
        if any(row['book_id']==r['book_id'] and row['page']==r['page'] and
               max(0,min(row['end'],r['end'])-max(row['start'],r['start'])) > .2*min(row['end']-row['start'],r['end']-r['start']) for r in selected):
            continue
        if token_budget-used < 32:
            break
        excerpt,n,cut = truncate(row['text'],token_budget-used)
        results.append(public_chunk(row) | {'title':row['title'],'text':excerpt,
            'text_tokens':n,'truncated':cut,'score':relevance[i]})
        selected.append(row)
        used += n
        if len(results)>=top_k:
            break
    return {'results':results,'text_tokens':used,'token_budget':token_budget,
            'retrieval_backend':'qdrant',
            'tokenizer':MODEL,'score_type':'cross_encoder_logit' if rerank else 'reciprocal_rank_fusion'}


def read_chunk(chunk_id, neighbors=0):
    with db() as con:
        row = con.execute('SELECT * FROM chunks WHERE id=?',(chunk_id,)).fetchone()
        if not row:
            raise KeyError('Unknown chunk')
        rows = con.execute('SELECT * FROM chunks WHERE book_id=? AND page=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal',
            (row['book_id'],row['page'],row['ordinal']-neighbors,row['ordinal']+neighbors)).fetchall()
    return {'chunks':[public_chunk(r) for r in rows]}


def read_page(book_id,page,token_budget=3000):
    with db() as con:
        row = con.execute('SELECT * FROM pages WHERE book_id=? AND page=?',(book_id,page)).fetchone()
    if not row:
        raise KeyError('Unknown page')
    text,n,cut = truncate(row['text'],token_budget)
    return dict(row) | {'text':text,'text_tokens':n,'truncated':cut}
