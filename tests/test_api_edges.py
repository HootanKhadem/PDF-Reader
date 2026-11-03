"""REST/MCP wrappers and job-runner error paths."""
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from app import core, main


def test_home_page_and_unknown_pdf(library):
    with closing(TestClient(main.app)) as client:
        assert client.get('/').status_code == 200
        assert client.get('/books/missing/pdf').status_code == 404


def test_ingest_endpoint_scans_books_folder(library):
    with closing(TestClient(main.app)) as client:
        assert client.post('/ingest').status_code == 202
        assert client.get('/ingest').json()['phase'] == 'completed'
        assert client.get('/ingest').json()['results'][0]['status'] == 'unchanged'


def test_mcp_tools_delegate_to_core(library):
    book = main.list_books()[0]
    assert set(book) == {'id', 'title', 'filename', 'pages', 'chunks'}
    found = main.search_books('basilisk petrify', book['id'], top_k=1, rerank=False, neighbors=1)
    hit = found['results'][0]
    assert hit['page'] == 2 and 'context' in hit
    assert main.read_chunk(hit['id'], 1)['chunks']
    assert main.read_page(book['id'], 1, 128)['page'] == 1


def test_scan_records_per_file_errors(monkeypatch):
    def boom(path, progress):
        raise RuntimeError('bad pdf')
    monkeypatch.setattr(core, 'ingest_file', boom)
    main.state.update(running=True, results=[])
    main.scan([Path('x.pdf')])
    assert main.state['results'] == [{'file': 'x.pdf', 'status': 'error', 'error': 'bad pdf'}]
    assert main.state['phase'] == 'completed_with_errors' and main.state['running'] is False


def test_repair_index_failure_is_reported(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError('qdrant gone')
    monkeypatch.setattr(core, 'sync_index', boom)
    main.state.update(running=True)
    main.repair_index()
    assert main.state['phase'] == 'failed' and main.state['error'] == 'qdrant gone'
    assert main.state['running'] is False
