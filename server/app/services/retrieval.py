import re
from sqlalchemy import bindparam, cast, select, func, literal_column, or_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from app.models import Chunk, Snapshot, ChunkEmbedding, SymbolReference, Vector768
from app.config import settings
from app.adapters.embeddings import embed_texts, embedding_profile, semantic_status, validate_vectors
from app.errors import AppError
from app.deadlines import check_deadline

STOP_WORDS = {"a", "about", "an", "and", "are", "can", "could", "describe", "do", "does", "for", "from", "give", "how", "in", "is", "it", "me", "of", "on", "or", "please", "should", "show", "tell", "the", "this", "to", "use", "used", "what", "where", "which", "who", "why", "with", "would"}
SYNONYM_GROUPS = (
    ("api", "apis", "endpoint", "endpoints", "route", "routes"),
    ("auth", "authentication"),
    ("error", "errors", "exception", "exceptions"),
    ("config", "configuration", "settings"),
    ("test", "tests", "testing"),
    ("dependency", "dependencies", "package", "packages"),
    ("database", "db", "sql", "postgres", "postgresql"),
)
SYNONYMS = {term: tuple(other for other in group if other != term) for group in SYNONYM_GROUPS for term in group}

FILE_REFERENCE = re.compile(r"(?<![\w.-])(?:[\w.-]+[\\/])*[\w.-]+\.[A-Za-z0-9]{1,12}(?![\w-]|\.[\w.-])")
PATH_LINE_REFERENCE = re.compile(r"(?<![\w.-])((?:[\w.-]+[\\/])*[\w.-]+\.[A-Za-z0-9]{1,12}):L?(\d+)(?!\d)", re.IGNORECASE)
LINE_REFERENCE = re.compile(r"\b(?:lines?|L)\s*(\d+)(?:\s*(?:-|to)\s*(?:L|line\s*)?(\d+))?\b", re.IGNORECASE)

LEXEME = re.compile(r"[A-Za-z0-9_]{2,}")
PIPELINE_FLOW = re.compile(
    r"\b(?:end[- ]to[- ]end|(?:whole|entire|full|complete)\s+(?:rag\s+)?pipeline|"
    r"(?:explain|walk\s+(?:me\s+)?through)\s+(?:the\s+)?(?:rag\s+)?pipeline|"
    r"how\s+does\s+(?:the\s+)?(?:rag\s+)?pipeline\s+work|"
    r"(?:indexing|ingestion)\b.*\b(?:to|and|then)\s+(?:chat|answer(?:s|ing)?))\b",
    re.IGNORECASE,
)
PIPELINE_STAGES = (
    ("(ingest | ingestion | archive | download) & (chunk | chunks | index | indexing | embeddings)",
     "job | worker | lease | persist | publish | commit"),
    ("(chat | answer | answering) & (retrieval | retrieve | context | citation | citations | prompt)",
     "agent | model | gemini | llm | generation | generate"),
)
AUXILIARY_PATH = r"(^|/)(docs?|examples?|eval|fixtures?|tests?)(/|$)|(^|/)(test_|smoke_)|\.(spec|test)\.|\.(md|mdx|rst|txt)$"
BROAD_FLOW = re.compile(r"\b(?:walk\s+(?:me\s+)?through|step[- ]by[- ]step|end[- ]to[- ]end|each\s+(?:stage|step|do)|what\s+happens)\b", re.I)
FLOW_PARTS = re.compile(r"[.!?;]\s+|,\s*(?:and\s+)?|\s+(?:and|then)\s+", re.I)
EXPLICIT_SYMBOL = re.compile(r"\x60([A-Za-z_]\w*)\x60|\b([A-Za-z_]\w*)\s*(?=\()|"
                             r"\b(?:[A-Za-z]*[a-z0-9][A-Z]\w*|[A-Za-z]\w*_\w+)\b", re.ASCII)
COMMON_PRODUCT_NAMES = {"github", "gitlab", "javascript", "typescript", "postgresql"}


def _scope(snapshot: Snapshot, path: str, symbol: str) -> tuple:
    filters = [Chunk.snapshot_id == snapshot.id, Chunk.index_version == snapshot.index_version]
    if path:
        filters.append((Chunk.path == path) | Chunk.path.startswith(path + "/", autoescape=True))
    if symbol:
        filters.append(Chunk.symbol_path.icontains(symbol, autoescape=True))
    return tuple(filters)


def _explicit_file_chunks(db: Session, scope: tuple, question: str, limit: int) -> list[Chunk]:
    """Prioritize a file explicitly named in the question over broad keyword matches."""
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
    matches = []
    for reference in list(dict.fromkeys(references))[:limit]:
        if "/" in reference:
            predicate = func.lower(Chunk.path) == reference
        else:
            predicate = func.lower(Chunk.path).endswith("/" + reference, autoescape=True) | (func.lower(Chunk.path) == reference)
        if line_range:
            predicate = predicate & (Chunk.line_start <= line_range[1]) & (Chunk.line_end >= line_range[0])
        chunks = list(db.execute(
            select(Chunk).where(*scope, predicate).order_by(Chunk.line_start, Chunk.path, Chunk.id).limit(limit)
        ).scalars())
        if chunks:
            matches.append(chunks)
    # Reserve one slot per named file before adding further excerpts from any file.
    ordered = (chunks[i] for i in range(limit) for chunks in matches if i < len(chunks))
    unique = {chunk.id: chunk for chunk in ordered}
    return list(unique.values())[:limit]

def _direct_terms(question: str) -> list[str]:
    terms = []
    for raw in LEXEME.findall(question):
        # Keep identifiers and split common code naming conventions.
        parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw).replace("_", " ").split()
        for term in (raw.lower(), *(part.lower() for part in parts)):
            if len(term) >= 2 and term not in STOP_WORDS and term not in terms:
                terms.append(term)
                if len(terms) == 24:
                    return terms
    return terms


def _terms(question: str) -> list[str]:
    direct = _direct_terms(question)
    expansions = []
    # Expand concepts in context, rather than turning generic words into broad OR queries.
    for pattern, aliases in (
        (r"\blocal\s+storage\b", ("localstorage",)),
        (r"\bsession\s+storage\b", ("sessionstorage",)),
        (r"\bdata\s+structures?\b", ("array", "object", "dict")),
    ):
        if re.search(pattern, question, re.IGNORECASE):
            expansions.extend(aliases)
    expansions.extend(alias for term in direct for alias in SYNONYMS.get(term, ()))
    return list(dict.fromkeys([*direct, *expansions]))[:24]


def _tsquery(expression: str):
    return func.to_tsquery(literal_column("'simple'"), expression)


def _fts(db: Session, scope: tuple, expression: str, limit: int, direct: list[str] | None = None, *,
         stage_signal: str = "", prefer_source: bool = False, filename_terms: tuple[str, ...] = ()) -> list[Chunk]:
    if not expression:
        return []
    query = _tsquery(expression)
    vector = func.to_tsvector(literal_column("'simple'"), Chunk.path + literal_column("' '") + Chunk.text)
    ranks = [func.ts_rank_cd(vector, _tsquery(" | ".join(direct))).desc()] if direct else []
    if prefer_source or stage_signal:
        ranks.append(Chunk.path.bool_op("~*")(AUXILIARY_PATH).asc())
    if stage_signal:
        ranks.append(vector.bool_op("@@")(_tsquery(stage_signal)).desc())
    matches = vector.bool_op("@@")(query)
    if filename_terms:
        basename = func.lower(func.regexp_replace(Chunk.path, "^.*/", ""))
        filename_match = or_(*(basename.contains(term, autoescape=True) for term in filename_terms))
        ranks.append(filename_match.desc())
        matches = matches | filename_match
    stmt = (select(Chunk).where(*scope, matches)
            .order_by(*ranks, func.ts_rank_cd(vector, query).desc(), Chunk.path, Chunk.line_start, Chunk.id).limit(limit))
    return list(db.scalars(stmt))


def _select_evidence(ordered: list[Chunk], limit: int) -> list[Chunk]:
    """Keep strong matches from the same file and discard near-identical line windows."""
    selected = []
    for chunk in ordered:
        if any(
            item.path == chunk.path
            and item.line_start <= chunk.line_start and item.line_end >= chunk.line_end
            for item in selected
        ):
            continue
        selected.append(chunk)
        if len(selected) == limit:
            break
    return selected


def search(db: Session, snapshot_id: str, question: str, limit: int = 8, *,
           path: str = "", symbol: str = "") -> list[Chunk]:
    """Rank keyword matches within the published index and any requested scope."""
    snapshot = db.get(Snapshot, snapshot_id)
    if not snapshot or limit <= 0:
        return []
    scope = _scope(snapshot, path, symbol)
    exact = _explicit_file_chunks(db, scope, question, limit)
    return exact or _select_evidence(_fts(db, scope, " | ".join(_terms(question)),
                                         max(limit, min(64, limit * 4)), _direct_terms(question)), limit)


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

def _keyword_ids(db: Session, scope: tuple, question: str, limit: int, mode: str | None) -> list[str]:
    terms = _terms(question)
    if terms and mode == "bm25":
        query = " ".join(terms)
        stmt = (select(Chunk.id).where(*scope, Chunk.text.bool_op("@@@")(query)
                                      | Chunk.path.bool_op("@@@")(query) | Chunk.symbol_path.bool_op("@@@")(query))
                .order_by(func.paradedb.score(Chunk.id).desc(), Chunk.path, Chunk.line_start, Chunk.id).limit(limit))
        try:
            with db.begin_nested():
                ids = list(db.scalars(stmt))
            if ids:
                return ids
        except DBAPIError:
            # An unavailable optional extension must not invalidate the caller's transaction.
            pass
    return [chunk.id for chunk in _fts(db, scope, " | ".join(terms), limit, _direct_terms(question))]


def _vector_ids(db: Session, scope: tuple, question: str, limit: int, report: dict) -> list[str]:
    if semantic_status(report)["mode"] != "hybrid":
        return []
    profile = embedding_profile()
    compatible = (
        select(ChunkEmbedding.chunk_id, ChunkEmbedding.values.label("embedding"))
        .join(Chunk, Chunk.id == ChunkEmbedding.chunk_id)
        .where(*scope, ChunkEmbedding.fingerprint == profile["fingerprint"],
               ChunkEmbedding.model == profile["model"], ChunkEmbedding.dimensions == profile["dimensions"],
               ChunkEmbedding.format_version == profile["formatVersion"])
    )
    if not db.scalar(compatible.with_only_columns(ChunkEmbedding.chunk_id).limit(1)):
        return []

    try:
        vector = validate_vectors(embed_texts([question], "RETRIEVAL_QUERY"), 1)[0]
        candidates = compatible.cte("compatible_embeddings").prefix_with("MATERIALIZED", dialect="postgresql")
        query_vector = cast(bindparam("query_vector", vector, type_=Vector768()), Vector768())
        stmt = (select(candidates.c.chunk_id)
                .order_by(candidates.c.embedding.op("<=>")(query_vector), candidates.c.chunk_id).limit(limit))
        with db.begin_nested():
            return list(db.scalars(stmt))
    except AppError as exc:
        if exc.code == "chat_timeout":
            raise
    except DBAPIError:
        pass
    return []


def _pipeline_anchors(db: Session, scope: tuple, question: str, limit: int) -> list[Chunk]:
    if limit < 2 or not PIPELINE_FLOW.search(question):
        return []
    anchors = []
    for expression, signal in PIPELINE_STAGES:
        check_deadline()
        hits = _fts(db, scope, expression, 1, stage_signal=signal)
        if not hits:
            return []  # Only reserve slots when the repository has evidence of both stages.
        anchors.extend(hits)
    return anchors


def _symbol_anchors(db: Session, snapshot: Snapshot, scope: tuple, question: str, limit: int) -> list[Chunk]:
    """Find definitions and occurrences even when broad keyword ranking misses them."""
    question = FILE_REFERENCE.sub(" ", question)
    names = dict.fromkeys((match[1] or match[2] or match[0]).strip()
                          for match in EXPLICIT_SYMBOL.finditer(question)
                          if match[1] or match[2] or match[0].lower() not in COMMON_PRODUCT_NAMES)
    prefer_source = not re.search(r"\b(?:docs?|documentation|readme)\b", question, re.I)
    hits = []
    for name in list(names)[:min(limit, 4)]:
        check_deadline()
        references = (select(SymbolReference.chunk_id, func.bool_or(SymbolReference.is_definition).label("definition"))
                      .where(SymbolReference.snapshot_id == snapshot.id,
                             SymbolReference.index_version == snapshot.index_version, SymbolReference.name == name)
                      .group_by(SymbolReference.chunk_id).subquery())
        stmt = select(Chunk).join(references, references.c.chunk_id == Chunk.id).where(*scope)
        ordering = [Chunk.path.bool_op("~*")(AUXILIARY_PATH).asc()] if prefer_source else []
        indexed = list(db.scalars(stmt.order_by(references.c.definition.desc(), *ordering,
                                                Chunk.path, Chunk.line_start, Chunk.id).limit(2)))
        # PostgreSQL's text parser can treat code punctuation as part of a token.
        # Match a literal identifier with word boundaries in the same index scope.
        occurrence_stmt = (select(Chunk).where(*scope, Chunk.text.bool_op("~*")(rf"\m{name}\M"))
                           .order_by(*ordering, Chunk.path, Chunk.line_start, Chunk.id).limit(24))
        occurrences = list(db.scalars(occurrence_stmt))
        candidates = {chunk.id: chunk for chunk in [*indexed, *occurrences]}
        by_path = {}
        for chunk in candidates.values():
            by_path.setdefault(chunk.path, chunk)
        ranked = dict.fromkeys([*(chunk.id for chunk in by_path.values()), *candidates])
        hits.extend(candidates[chunk_id] for chunk_id in list(ranked)[:3])
    return hits


def _flow_anchors(db: Session, scope: tuple, question: str, limit: int) -> list[Chunk]:
    """Give distinct clauses in a broad question their own bounded keyword search."""
    if limit < 2 or not BROAD_FLOW.search(question):
        return []
    parts = [_direct_terms(part) for part in FLOW_PARTS.split(question)]
    parts = [terms for terms in parts if terms and terms[0] not in {"cite", "sources"}]
    if len(parts) < 2:
        return []
    hits = []
    for terms in parts[:min(limit, 4)]:
        check_deadline()
        filename_terms = (terms[0],) if len(terms) <= 4 and len(terms[0]) >= 5 else ()
        candidates = _fts(db, scope, " | ".join(terms), 4, prefer_source=True, filename_terms=filename_terms)
        if candidates:
            paths = {hit.path for hit in hits}
            hits.append(next((chunk for chunk in candidates if chunk.path not in paths), candidates[0]))
    return hits


def hybrid_search_scored(db: Session, snapshot_id: str, question: str, limit: int = 8, *,
                         path: str = "", symbol: str = "") -> list[tuple[Chunk, float]]:
    """Return ranked evidence from the published snapshot version and requested scope."""
    check_deadline()
    snapshot = db.get(Snapshot, snapshot_id)
    if not snapshot or limit <= 0:
        return []
    scope = _scope(snapshot, path, symbol)
    report = snapshot.index_report or {}
    exact = _explicit_file_chunks(db, scope, question, limit)
    anchors = []
    if not (path or symbol or LINE_REFERENCE.search(question) or PATH_LINE_REFERENCE.search(question)):
        anchors = _pipeline_anchors(db, scope, question, limit) or _flow_anchors(db, scope, question, limit)
    if exact and not anchors:
        return [(chunk, 1.0 / rank) for rank, chunk in enumerate(exact, 1)]

    symbols = _symbol_anchors(db, snapshot, scope, question, limit)
    candidate_limit = min(80, max(limit * 6, 24))
    keyword_ids = _keyword_ids(db, scope, question, candidate_limit, report.get("keywordSearch"))
    vector_ids = _vector_ids(db, scope, question, candidate_limit, report)
    all_ids = set(keyword_ids) | set(vector_ids)
    by_id = {chunk.id: chunk for chunk in [*anchors, *exact, *symbols]}
    if all_ids:
        by_id.update((chunk.id, chunk) for chunk in db.scalars(select(Chunk).where(*scope, Chunk.id.in_(all_ids))))

    ranks: dict[str, float] = dict.fromkeys(by_id, 0.0)
    for ranking in (keyword_ids, vector_ids):
        for rank, chunk_id in enumerate(ranking, 1):
            if chunk_id in ranks:
                ranks[chunk_id] += 1.0 / (60 + rank)
    # A small bounded bonus makes literal matches stronger than synonym-only hits.
    terms = set(_direct_terms(question))
    question_lower = question.lower()
    line_match = LINE_REFERENCE.search(question)
    for chunk_id, chunk in by_id.items():
        if terms:
            lexemes = set(LEXEME.findall(f"{chunk.path} {chunk.symbol_path or ''} {chunk.text}".lower()))
            ranks[chunk_id] += 0.008 * len(terms & lexemes) / len(terms)
        if chunk.path.lower() in question_lower:
            ranks[chunk_id] += 0.8
        if chunk.path.rsplit("/", 1)[-1].lower() in question_lower:
            ranks[chunk_id] += 0.45
        if chunk.symbol_path and any(part.lower() in question_lower for part in re.split(r"[./:]+", chunk.symbol_path) if len(part) > 2):
            ranks[chunk_id] += 0.4
        if line_match and chunk.line_start <= int(line_match.group(1)) <= chunk.line_end:
            ranks[chunk_id] += 1.0
    # Reserve room for both stages ahead of generic or explicitly named matches.
    stage_bonus = max(ranks.values(), default=0.0) + 0.001
    for chunk_id in {chunk.id for chunk in anchors}:
        ranks[chunk_id] += stage_bonus
    for rank, chunk_id in enumerate(dict.fromkeys(chunk.id for chunk in symbols), 1):
        ranks[chunk_id] += (2 * stage_bonus if rank == 1 else stage_bonus / rank)
    ordered = sorted(by_id.values(), key=lambda c: (-ranks[c.id], c.path, c.line_start, c.id))
    return [(chunk, ranks[chunk.id]) for chunk in _select_evidence(ordered, limit)]


def hybrid_search(db: Session, snapshot_id: str, question: str, limit: int = 8, *,
                  path: str = "", symbol: str = "") -> list[Chunk]:
    """Return ranked chunks without scores."""
    ranking = hybrid_search_scored(db, snapshot_id, question, limit, path=path, symbol=symbol)
    return [chunk for chunk, _score in ranking]
