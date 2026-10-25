import pymupdf
import pytest
from app import core,vector_index


@pytest.fixture(autouse=True)
def isolated_library(tmp_path,monkeypatch):
    """Every test gets its own SQLite UUID and real Qdrant collection."""
    monkeypatch.setattr(core,'DATA',tmp_path/'data')
    monkeypatch.setattr(core,'UPLOADS',tmp_path/'uploads')
    yield
    assert core.DATA.resolve().is_relative_to(tmp_path.resolve())
    collection,_=core.vector_config()
    vector_index.delete_collection(collection)


@pytest.fixture
def library(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'BOOKS',tmp_path)
    path=tmp_path/'sample.pdf'
    with pymupdf.open() as doc:
        p=doc.new_page()
        assert p.insert_textbox(pymupdf.Rect(50,50,550,750),
            'Maintaining magical focus\nA wizard must concentrate to sustain a spell. '
            'Taking damage requires a Constitution saving throw to avoid losing concentration.\n'*18,fontsize=9)>=0
        p=doc.new_page()
        assert p.insert_textbox(pymupdf.Rect(50,50,550,750),
            'Stone gaze\nThe basilisk can petrify a creature that looks into its eyes. '
            'The victim becomes a statue of stone.\n'*18,fontsize=9)>=0
        doc.save(path)
    result=core.ingest_file(path)
    assert result['status']=='indexed',result
    return path,result
