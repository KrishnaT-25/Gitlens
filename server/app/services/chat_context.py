"""Deterministic chat routing and lossless overlap reduction before generation."""
import re
from dataclasses import dataclass

from app.models import Chunk, SnapshotNode

COMPLEX = re.compile(r"\b(across|between|compare|versus|architecture|workflow|flow|overview|explain|end.to.end|multi.file|in detail|detailed|step.by.step|trade.?offs?|why)\b", re.I)
LOOKUP = re.compile(r"^\s*(what\b|which\b|where\b|show\b|list\b|read\b|find\b|how many\b)", re.I)
FILES = re.compile(r"[\w./-]+\.(?:py|jsx?|tsx?|json|ya?ml|toml|md|txt|html|css|sql)\b", re.I)
MAP_NOTICE = "Partial navigation map; use list_dir or search to inspect omitted entries."


def question_kind(question: str, has_history: bool = False) -> str:
    """Conservative, model-free routing; unknown or ambiguous follow-ups keep tools."""
    if COMPLEX.search(question) or len(set(FILES.findall(question))) > 1:
        return "complex"
    if has_history and len(question.split()) < 9 and re.search(r"\b(it|that|this|those|them|there)\b", question, re.I):
        return "standard"
    if LOOKUP.search(question) and len(question.split()) <= 35:
        return "lookup"
    return "standard"


@dataclass
class PreparedContext:
    text: str
    chunks: list[Chunk]
    candidate_chars: int
    duplicate_chunks: int
    removed_overlap_lines: int


def _runs(lines: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    runs: list[list[tuple[int, str]]] = []
    for number, content in lines:
        if not runs or number != runs[-1][-1][0] + 1:
            runs.append([])
        runs[-1].append((number, content))
    return runs


def prepare_context(chunks: list[Chunk], max_chars: int) -> PreparedContext:
    """Remove identical same-file overlaps, preserving all admitted source lines.

    Partial chunks retain their original IDs/ranges. Shared lines are explicitly
    referenced from earlier evidence; full coverage remains present in the prompt.
    Conflicting text or inconsistent line metadata is never merged. No ORM rows
    are mutated and the existing character cap is unchanged.
    """
    blocks, included = [], []
    covered: dict[tuple[str, int], str] = {}
    seen_ids: set[str] = set()
    candidate_chars = duplicates = removed = used = 0
    for chunk in chunks:
        if chunk.id in seen_ids:
            duplicates += 1
            continue
        seen_ids.add(chunk.id)
        header = f"CHUNK {chunk.id} FILE {chunk.path} LINES {chunk.line_start}-{chunk.line_end}"
        full = f"{header}\n{chunk.text}"
        candidate_chars += len(full) + 2
        lines = chunk.text.splitlines()
        valid_lines = len(lines) == chunk.line_end - chunk.line_start + 1
        numbered = list(enumerate(lines, chunk.line_start)) if valid_lines else []
        conflicting = any((chunk.path, n) in covered and covered[chunk.path, n] != content for n, content in numbered)
        fresh = [(n, content) for n, content in numbered if (chunk.path, n) not in covered]
        overlap = len(numbered) - len(fresh) if not conflicting else 0
        if numbered and overlap == len(numbered):
            duplicates += 1
            removed += overlap
            continue
        block, saved_lines = full, 0
        if overlap:
            parts = [header, "Shared lines are in preceding evidence for this file; additional lines:"]
            for run in _runs(fresh):
                parts.extend([f"LINES {run[0][0]}-{run[-1][0]}", "\n".join(content for _, content in run)])
            compact = "\n".join(parts)
            if len(compact) < len(full):
                block, saved_lines = compact, overlap
        cost = len(block) + (2 if blocks else 0)
        if used + cost > max_chars:
            continue
        blocks.append(block)
        included.append(chunk)
        used += cost
        removed += saved_lines
        if not conflicting:
            covered.update({(chunk.path, n): content for n, content in numbered})
    return PreparedContext("\n\n".join(blocks), included, candidate_chars, duplicates, removed)


def compact_map(nodes: list[SnapshotNode], relevant_paths: list[str], max_chars: int) -> str:
    """Prefer root directories and evidence paths; truncate whole entries, never paths."""
    root = next((node for node in nodes if node.path == "" and node.node_type == "directory"), None)
    lines = []
    if root and root.summary:
        lines.append("ROOT " + " ".join(root.summary.split())[:800])
    relevant = set(relevant_paths)
    directories = [node for node in nodes if node.path and node.node_type == "directory"]
    files = [node for node in nodes if node.node_type == "file"]
    def related(path: str) -> bool:
        return any(name.startswith(path + "/") for name in relevant)
    directories.sort(key=lambda node: (bool(node.parent_path), not related(node.path), node.path))
    # Root structure first, then exact evidence paths, then nearby navigation.
    lines.extend("DIR " + node.path for node in directories if not node.parent_path and related(node.path))
    for node in sorted((n for n in files if n.path in relevant), key=lambda n: n.path):
        suffix = " — " + " ".join(node.summary.split())[:120] if node.summary else ""
        lines.append("FILE " + node.path + suffix)
    lines.extend("DIR " + node.path for node in directories if node.parent_path and related(node.path))
    lines.extend("DIR " + node.path for node in directories if not node.parent_path and not related(node.path))
    lines.extend("FILE " + node.path for node in sorted(files, key=lambda n: n.path) if not node.parent_path and node.path not in relevant)
    kept, used = [], 0
    budget = max(0, max_chars - len(MAP_NOTICE) - 1)
    for line in lines:
        if used + len(line) + 1 <= budget:
            kept.append(line)
            used += len(line) + 1
    return "\n".join([*kept, MAP_NOTICE])[:max_chars]
