"""Embeddings for semantic search. An embedding is a list of numbers (a vector) that a small language
model computes from a text, so that texts with similar meaning get vectors pointing in similar directions.
Comparing two vectors (cosine similarity) then measures how alike two jobs, or a job and the CV, are.
This module builds the text that gets embedded and defines the Embedder interface; the model comes later."""
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Protocol

PASSAGE_PREFIX = "passage: "  # e5 models are trained with "passage: " for documents and "query: " for searches
QUERY_PREFIX = "query: "


class Embedder(Protocol):
    """Anything that turns texts into vectors: the real ONNX model, or FakeEmbedder in tests."""
    model: str  # stored next to each vector, so vectors from different models are never compared
    dims: int

    def embed_passages(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _skill_names(skills) -> list[str]:
    # job_detail gives [{name, ...}], the list view and records give plain names; accept both
    names = [s.get("name") if isinstance(s, Mapping) else s for s in (skills or [])]
    return [str(n).strip() for n in names if n and str(n).strip()]


def embed_text(job: Mapping) -> str:
    """'passage: title | company | skill, skill | description', skipping empty parts.
    Whitespace is collapsed so formatting-only changes don't change text_hash and force a re-embed."""
    parts = [job.get("title"), job.get("company"), ", ".join(_skill_names(job.get("skills"))),
             job.get("description")]
    parts = [re.sub(r"\s+", " ", str(p)).strip() for p in parts if p is not None]
    return PASSAGE_PREFIX + " | ".join(p for p in parts if p)


def text_hash(text: str) -> str:
    """sha256 of the embedded text: if it hasn't changed, the stored vector is still valid."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def to_pgvector(vec: Sequence[float]) -> str:
    """pgvector's text form '[0.1,0.2]', passed as %s::vector, so no pgvector Python package is needed."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
