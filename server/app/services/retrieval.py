import re
from sqlalchemy import select, func, literal_column, text
from sqlalchemy.orm import Session
from app.models import Chunk, Snapshot, ChunkEmbedding
from app.config import settings
from app.adapters.embeddings import embed_texts
from app.errors import AppError

STOP_WORDS = {"a", "about", "an", "and", "are", "can", "could", "describe", "do", "does", "for", "from", "give", "how", "in", "is", "it", "me", "of", "on", "or", "please", "should", "show", "tell", "the", "this", "to", "use", "used", "what", "where", "which", "who", "why", "with", "would"}
SYNONYM_GROUPS = (
    ("api", "apis", "endpoint", "endpoints", "route", "routes", "handler", "handlers"),
    ("auth", "authentication", "authorization", "login", "logout", "session", "sessions"),
    ("bug", "bugs", "error", "errors", "exception", "exceptions", "failure", "failures"),
    ("class", "classes", "struct", "structs", "type", "types"),
    ("structure", "structures", "array", "arrays", "object", "objects", "map", "maps", "dictionary", "dict", "collection", "collections"),
    ("config", "configuration", "settings", "environment"),
    ("function", "functions", "method", "methods", "def", "func"),
    ("test", "tests", "testing", "spec", "specs"),
    ("dependency", "dependencies", "package", "packages", "library", "libraries"),
    ("database", "db", "sql", "postgres", "postgresql"),
    ("local", "localstorage", "storage", "persist", "persistence"),
    ("session", "sessionstorage", "browser", "storage"),
)
SYNONYMS = {term: tuple(other for other in group if other != term) for group in SYNONYM_GROUPS for term in group}

FILE_REFERENCE = re.compile(r"(?<![\w.-])(?:[\w.-]+[\\/])*[\w.-]+\.[A-Za-z0-9]{1,12}(?![\w.-])")
PATH_LINE_REFERENCE = re.compile(r"(?<![\w.-])((?:[\w.-]+[\\/])*[\w.-]+\.[A-Za-z0-9]{1,12}):L?(\d+)(?!\d)", re.IGNORECASE)
LINE_REFERENCE = re.compile(r"\b(?:lines?|L)\s*(\d+)(?:\s*(?:-|to)\s*(?:L|line\s*)?(\d+))?\b", re.IGNORECASE)

def _explicit_file_chunks(db: Session, snapshot_id: str, question: str, limit: int) -> list[Chunk]:
    """Prioritize a file explicitly named in the question over broad keyword matches."""
    snapshot = db.get(Snapshot, snapshot_id)
    if not snapshot:
        return []
    line_match = PATH_LINE_REFERENCE.search(question)
    line_range = None
    if line_match:
        references = [line_match.group(1).replace("\\", "/").lower()]
        line_range = (int(line_match.group(2)), int(line_match.group(2)))
    else:
        references = [match.group(0).replace("\\", "/").lower() for match in FILE_REFERENCE.finditer(question)]
        if not references:
            # Common repository files such as README have no required extension.
            references = [name for name in ("readme", "dockerfile", "makefile", "license", "changelog")
                          if re.search(rf"(?<![\w.-]){name}(?![\w.-])", question, re.IGNORECASE)]
        match = LINE_REFERENCE.search(question)
        if match:
            start = int(match.group(1))
            line_range = (start, int(match.group(2) or start))
    for reference in references:
        if "/" in reference:
            predicate = func.lower(Chunk.path) == reference
        else:
            predicate = func.lower(Chunk.path).like(f"%/{reference}") | (func.lower(Chunk.path) == reference)
        if line_range:
            predicate = predicate & (Chunk.line_start <= line_range[1]) & (Chunk.line_end >= line_range[0])
        chunks = list(db.execute(
            select(Chunk).where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == snapshot.index_version, predicate)
            .order_by(Chunk.line_start).limit(limit)
        ).scalars())
        if chunks:
            return chunks
    return []

def _terms(question: str) -> list[str]:
    terms = []
    for raw in re.findall(r"[A-Za-z0-9_]{2,}", question):
        # Keep full identifiers and also split common code naming conventions.
        parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw).replace("_", " ").split()
        for term in (raw.lower(), *(part.lower() for part in parts)):
            if term not in STOP_WORDS and term not in terms:
                terms.append(term)
                terms.extend(x for x in SYNONYMS.get(term, ()) if x not in terms)
            if len(terms) >= 24:
                return terms[:24]
    return terms

def search(db: Session, snapshot_id: str, question: str, limit: int = 8) -> list[Chunk]:
    snapshot = db.get(Snapshot, snapshot_id)
    if not snapshot:
        return []
    explicit_file = _explicit_file_chunks(db, snapshot_id, question, limit)
    if explicit_file:
        return explicit_file
    terms = _terms(question)
    if not terms:
        return []
    query = func.to_tsquery(literal_column("'simple'"), " | ".join(terms))
    vector = func.to_tsvector(literal_column("'simple'"), Chunk.path + literal_column("' '") + Chunk.text)
    candidate_limit = min(64, max(limit * 4, limit))
    stmt = select(Chunk).where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == snapshot.index_version,
        vector.op("@@")(query)).order_by(func.ts_rank_cd(vector, query).desc(), Chunk.path, Chunk.line_start).limit(candidate_limit)
    candidates = list(db.execute(stmt).scalars())
    # Favor relevant files before taking additional chunks from a single large file.
    selected, paths = [], set()
    for chunk in candidates:
        if chunk.path not in paths:
            selected.append(chunk)
            paths.add(chunk.path)
            if len(selected) == limit:
                return selected
    for chunk in candidates:
        if any(existing.id == chunk.id for existing in selected):
            continue
        if any(existing.path == chunk.path and existing.line_start <= chunk.line_end and chunk.line_start <= existing.line_end for existing in selected):
            continue
        selected.append(chunk)
        if len(selected) == limit:
            break
    return selected

def expand_context(db: Session, snapshot_id: str, anchors: list[Chunk], limit: int = 16) -> list[Chunk]:
    """Add nearest same-file chunks around matches within the exact snapshot boundary."""
    if not anchors:
        return []
    snapshot = db.get(Snapshot, snapshot_id)
    if not snapshot:
        return []
    anchor_ids = {chunk.id for chunk in anchors}
    candidates = {}
    per_side = max(1, limit - len(anchors))
    for chunk in anchors:
        neighbors = (
            select(Chunk).where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == snapshot.index_version, Chunk.path == chunk.path, Chunk.line_start < chunk.line_start)
            .order_by(Chunk.line_start.desc()).limit(per_side),
            select(Chunk).where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == snapshot.index_version, Chunk.path == chunk.path, Chunk.line_start > chunk.line_start)
            .order_by(Chunk.line_start).limit(per_side),
        )
        for stmt in neighbors:
            for neighbor in db.execute(stmt).scalars():
                if neighbor.id not in anchor_ids:
                    distance = abs(neighbor.line_start - chunk.line_start)
                    previous = candidates.get(neighbor.id)
                    if previous is None or distance < previous[0]:
                        candidates[neighbor.id] = (distance, neighbor)
    context = [entry[1] for entry in sorted(candidates.values(), key=lambda entry: (entry[0], entry[1].path, entry[1].line_start))]
    return sorted([*anchors, *context[:max(0, limit - len(anchors))]], key=lambda chunk: (chunk.path, chunk.line_start))

def hybrid_search(db: Session, snapshot_id: str, question: str, limit: int = 8) -> list[Chunk]:
    """Snapshot-scoped BM25/FTS + vector candidates, fused and locally reranked."""
    snapshot = db.get(Snapshot, snapshot_id)
    if not snapshot:
        return []
    exact = _explicit_file_chunks(db, snapshot_id, question, limit)
    if exact:
        return exact

    candidate_limit = min(80, max(limit * 6, 24))
    bm25_ids: list[str] = []
    terms = _terms(question)
    keyword_mode = (snapshot.index_report or {}).get("keywordSearch")
    if terms and keyword_mode == "bm25":
        # pg_search is optional on free/self-hosted PostgreSQL. Its absence falls back to FTS.
        search_text = " ".join(terms[:16])
        try:
            rows = db.execute(text("""
                SELECT id FROM chunks
                WHERE snapshot_id = :snapshot_id AND index_version = :version
                  AND (text @@@ :query OR path @@@ :query OR symbol_path @@@ :query)
                ORDER BY paradedb.score(id) DESC, path, line_start LIMIT :limit
            """), {"snapshot_id": snapshot_id, "version": snapshot.index_version,
                  "query": search_text, "limit": candidate_limit}).all()
            bm25_ids = [row[0] for row in rows]
        except Exception:
            db.rollback()
    if not bm25_ids:
        bm25_ids = [chunk.id for chunk in search(db, snapshot_id, question, candidate_limit)]

    vector_ids: list[str] = []
    has_vectors = (snapshot.index_report or {}).get("semantic") == "ready" and bool(
        db.execute(select(ChunkEmbedding.chunk_id).join(Chunk, Chunk.id == ChunkEmbedding.chunk_id)
                   .where(Chunk.snapshot_id == snapshot_id, Chunk.index_version == snapshot.index_version).limit(1)).first())
    if has_vectors and settings.gemini_api_key:
        try:
            vector = embed_texts([question], "RETRIEVAL_QUERY")[0]
            literal = "[" + ",".join(f"{value:.8f}" for value in vector) + "]"
            rows = db.execute(text("""
                SELECT ce.chunk_id FROM chunk_embeddings ce
                JOIN chunks c ON c.id = ce.chunk_id
                WHERE c.snapshot_id = :snapshot_id AND c.index_version = :version
                ORDER BY ce.embedding <=> CAST(:vector AS vector) LIMIT :limit
            """), {"snapshot_id": snapshot_id, "version": snapshot.index_version,
                  "vector": literal, "limit": candidate_limit}).all()
            vector_ids = [row[0] for row in rows]
        except (AppError, Exception):
            db.rollback()

    all_ids = list(dict.fromkeys(bm25_ids + vector_ids))
    if not all_ids:
        return []
    chunks = db.execute(select(Chunk).where(Chunk.id.in_(all_ids), Chunk.snapshot_id == snapshot_id,
                                            Chunk.index_version == snapshot.index_version)).scalars().all()
    by_id = {chunk.id: chunk for chunk in chunks}
    ranks: dict[str, float] = {}
    for ranking in (bm25_ids, vector_ids):
        for rank, chunk_id in enumerate(ranking, 1):
            ranks[chunk_id] = ranks.get(chunk_id, 0.0) + 1.0 / (60 + rank)
    question_lower = question.lower()
    line_match = LINE_REFERENCE.search(question)
    for chunk_id in list(ranks):
        chunk = by_id.get(chunk_id)
        if not chunk:
            continue
        if chunk.path.lower() in question_lower:
            ranks[chunk_id] += 0.8
        basename = chunk.path.rsplit("/", 1)[-1].lower()
        if basename in question_lower:
            ranks[chunk_id] += 0.45
        if chunk.symbol_path and any(part.lower() in question_lower for part in re.split(r"[./:]+", chunk.symbol_path) if len(part) > 2):
            ranks[chunk_id] += 0.4
        if line_match and chunk.line_start <= int(line_match.group(1)) <= chunk.line_end:
            ranks[chunk_id] += 1.0
    ordered = sorted((by_id[cid] for cid in ranks if cid in by_id),
                     key=lambda c: (-ranks[c.id], c.path, c.line_start))
    # Keep the result diverse across files, then fill remaining slots with the next best evidence.
    selected, paths = [], set()
    for chunk in ordered:
        if chunk.path not in paths:
            selected.append(chunk)
            paths.add(chunk.path)
            if len(selected) >= limit:
                return selected
    for chunk in ordered:
        if chunk.id not in {item.id for item in selected} and not any(
                item.path == chunk.path and item.line_start <= chunk.line_end and chunk.line_start <= item.line_end
                for item in selected):
            selected.append(chunk)
            if len(selected) >= limit:
                break
    return selected
