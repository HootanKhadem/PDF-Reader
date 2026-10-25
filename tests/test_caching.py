import pymupdf
from app import core,extraction


def test_raw_ocr_cache_retains_short_stat_rows(tmp_path,monkeypatch):
    with pymupdf.open() as source,pymupdf.open() as scan:
        page=source.new_page()
        page.insert_text((60,100),'HP 14 (4d6)',fontsize=24)
        page.insert_text((60,140),'A magic sword can fly through the air.',fontsize=16)
        image=page.get_pixmap(matrix=pymupdf.Matrix(2,2))
        scanned=scan.new_page()
        scanned.insert_image(scanned.rect,stream=image.tobytes('png'))
        cache=tmp_path/'ocr.tsv'
        first=extraction.extract(scanned,cache)
        assert cache.is_file() and 'HP 14' in first[0]
        def no_ocr(*args,**kwargs):
            raise AssertionError('Cached OCR should not run Tesseract again')
        monkeypatch.setattr(extraction.subprocess,'run',no_ocr)
        assert extraction.extract(scanned,cache)==first


def test_unchanged_chunk_embeddings_reused_on_forced_repair(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path/'data')
    path=tmp_path/'cache-check.pdf'
    with pymupdf.open() as doc:
        p=doc.new_page()
        p.insert_textbox(pymupdf.Rect(50,50,550,750),'A compass helps an explorer navigate unfamiliar terrain. '*20)
        doc.save(path)
    result=core.ingest_file(path)
    def no_embedding(*args,**kwargs):
        raise AssertionError('Unchanged chunks should reuse existing embeddings')
    monkeypatch.setattr(core.embedder(),'encode',no_embedding)
    progress=[]
    repaired=core.ingest_file(path,force=True,progress=lambda **s:progress.append(s))
    assert repaired['chunks']==result['chunks']
    assert any(s.get('reused_chunks')==result['chunks'] for s in progress)
