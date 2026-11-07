from types import SimpleNamespace

from eval import retriever as eval_retriever
from app.services import retrieval


def test_hybrid_search_keeps_chunk_only_api_compatible(monkeypatch):
    first = SimpleNamespace(id="first")
    second = SimpleNamespace(id="second")
    monkeypatch.setattr(retrieval, "hybrid_search_scored", lambda *_args, **_kwargs: [(first, 0.08), (second, 0.06)])
    assert retrieval.hybrid_search(None, "snapshot", "question", 2) == [first, second]


def test_evaluation_retrieve_returns_ids_scores_and_source_locations(monkeypatch):
    chunk = SimpleNamespace(id="chunk-1", path="src/app.py", line_start=4, line_end=8, text="def run(): pass")

    class FakeSession:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def get(self, _model, _snapshot_id): return SimpleNamespace(id="snapshot", status="ready")

    monkeypatch.setattr(eval_retriever, "SessionLocal", FakeSession)
    monkeypatch.setattr(eval_retriever, "hybrid_search_scored", lambda *_args: [(chunk, 0.27)])
    assert eval_retriever.retrieve("where is run defined", 5, "snapshot") == [{
        "id": "chunk-1", "score": 0.27, "path": "src/app.py", "line_start": 4,
        "line_end": 8, "text": "def run(): pass",
    }]
