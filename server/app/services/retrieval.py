import re
from sqlalchemy import select, func, literal_column
from sqlalchemy.orm import Session
from app.models import Chunk

STOP_WORDS = {"a", "about", "an", "and", "are", "can", "could", "describe", "do", "does", "for", "from", "give", "how", "in", "is", "it", "me", "of", "on", "or", "please", "should", "show", "tell", "the", "this", "to", "use", "used", "what", "where", "which", "who", "why", "with", "would"}
SYNONYM_GROUPS = (
    ("api", "apis", "endpoint", "endpoints", "route", "routes", "handler", "handlers"),
    ("auth", "authentication", "authorization", "login", "logout", "session", "sessions"),
    ("bug", "bugs", "error", "errors", "exception", "exceptions", "failure", "failures"),
    ("class", "classes", "struct", "structs", "type", "types"),
    ("config", "configuration", "settings", "environment"),
    ("function", "functions", "method", "methods", "def", "func"),
    ("test", "tests", "testing", "spec", "specs"),
    ("dependency", "dependencies", "package", "packages", "library", "libraries"),
    ("database", "db", "sql", "postgres", "postgresql"),
)
SYNONYMS = {term: tuple(other for other in group if other != term) for group in SYNONYM_GROUPS for term in group}

FILE_REFERENCE = re.compile(r"(?<![\w.-])(?:[\w.-]+[\\/])*[\w.-]+\.[A-Za-z0-9]{1,12}(?![\w.-])")

def _explicit_file_chunks(db: Session, snapshot_id: str, question: str, limit: int) -> list[Chunk]:
    """Prioritize a file explicitly named in the question over broad keyword matches."""
    references = [match.group(0).replace("\\", "/").lower() for match in FILE_REFERENCE.finditer(question)]
    for reference in references:
        if "/" in reference:
            predicate = func.lower(Chunk.path) == reference
        else:
            predicate = func.lower(Chunk.path).like(f"%/{reference}") | (func.lower(Chunk.path) == reference)
        chunks = list(db.execute(
            select(Chunk).where(Chunk.snapshot_id == snapshot_id, predicate)
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
    explicit_file = _explicit_file_chunks(db, snapshot_id, question, limit)
    if explicit_file:
        return explicit_file
    terms = _terms(question)
    if not terms:
        return []
    query = func.to_tsquery(literal_column("'simple'"), " | ".join(terms))
    vector = func.to_tsvector(literal_column("'simple'"), Chunk.path + literal_column("' '") + Chunk.text)
    candidate_limit = min(64, max(limit * 4, limit))
    stmt = select(Chunk).where(Chunk.snapshot_id == snapshot_id, vector.op("@@")(query)).order_by(func.ts_rank_cd(vector, query).desc(), Chunk.path, Chunk.line_start).limit(candidate_limit)
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
    anchor_ids = {chunk.id for chunk in anchors}
    candidates = {}
    per_side = max(1, limit - len(anchors))
    for chunk in anchors:
        neighbors = (
            select(Chunk).where(Chunk.snapshot_id == snapshot_id, Chunk.path == chunk.path, Chunk.line_start < chunk.line_start)
            .order_by(Chunk.line_start.desc()).limit(per_side),
            select(Chunk).where(Chunk.snapshot_id == snapshot_id, Chunk.path == chunk.path, Chunk.line_start > chunk.line_start)
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
