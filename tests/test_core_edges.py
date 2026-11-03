"""Edge cases in ingestion and extraction that the happy-path suites do not reach."""

import pymupdf
import pytest

from app import core, extraction, vector_index


def make_pdf(path, pages, **save):
    with pymupdf.open() as doc:
        for text in pages:
            page = doc.new_page()
            if text:
                page.insert_textbox(pymupdf.Rect(50, 50, 550, 750), text, fontsize=10)
        doc.save(path, **save)
    return path


LONG = 'The wizard studies ancient spellbooks and learns new incantations every morning. ' * 12


def test_clean_removes_soft_hyphens_nulls_and_joins_split_words():
    assert core.clean('con­cen\x00tration  of\tmagic') == 'concentration of magic'
    assert core.clean('spell-\ncasting') == 'spellcasting'


def test_source_path_unknown_file_raises():
    with pytest.raises(KeyError):
        core.source_path('definitely-not-here.pdf')


def test_split_text_breaks_before_uppercase_headings():
    text = 'Intro paragraph with ordinary words.\nSTONE GAZE\nThe basilisk petrifies creatures.'
    spans = list(core.split_text(text, core.embedder().tokenizer))
    assert len(spans) >= 2 and spans[1][2].startswith('STONE GAZE')


def test_encrypted_pdf_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'BOOKS', tmp_path)
    path = make_pdf(tmp_path / 'locked.pdf', [LONG], encryption=pymupdf.PDF_ENCRYPT_AES_256,
                    owner_pw='owner', user_pw='user')
    with pytest.raises(ValueError, match='Encrypted'):
        core.ingest_file(path)


def test_blank_pdf_has_no_searchable_text(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'BOOKS', tmp_path)
    path = make_pdf(tmp_path / 'blank.pdf', [''])
    with pytest.raises(ValueError, match='No searchable text'):
        core.ingest_file(path)
    assert core.list_books() == []


def test_pipeline_change_reuses_cached_pages_and_vectors(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'BOOKS', tmp_path)
    path = make_pdf(tmp_path / 'cached.pdf', [LONG, 'Short page.'])
    first = core.ingest_file(path)
    assert first['status'] == 'indexed'
    low = [w['pages'] for w in first['warnings'] if w['type'] == 'low_text_pages'][0]
    assert low == [2]
    monkeypatch.setattr(core, 'PIPELINE', core.PIPELINE.replace('v3-', 'v4-', 1))
    monkeypatch.setattr(core.embedder(), 'encode', lambda *a, **k: pytest.fail('vectors were not reused'))
    again = core.ingest_file(path)
    assert again['status'] == 'indexed' and again['chunks'] == first['chunks']


def test_unchanged_book_reports_pending_index_when_qdrant_is_down(library, monkeypatch):
    path, _ = library
    def down(*a, **k):
        raise vector_index.IndexUnavailable('down')
    monkeypatch.setattr(core, 'sync_index', down)
    result = core.ingest_file(path)
    assert result['status'] == 'index_pending' and result['error'] == 'down'


def test_sync_refuses_embeddings_from_another_model(library):
    with core.db() as con:
        con.execute("UPDATE books SET fingerprint=?", ('0' * 64 + ':v3-heading-320-48-other/model-v1-confidence-columns-ocr-eng-0',))
    with pytest.raises(vector_index.IndexUnavailable, match='different model'):
        core.sync_index(force=True)


def test_extraction_worker_entrypoints(tmp_path):
    path = make_pdf(tmp_path / 'w.pdf', [LONG])
    extraction.init_worker(str(path), str(tmp_path / 'cache'))
    index, (text, used_ocr, info) = extraction.extract_number(0)
    assert index == 0 and 'wizard' in text and not used_ocr and 'discarded_blocks' in info
    extraction.init_worker(str(path))
    assert extraction.extract_number(0)[1][0] == text


def test_unreadable_debris_is_dropped():
    assert not extraction.readable('~ ^ | ,')
    assert not extraction.readable('a b c')
    assert extraction.readable('日本語です')
