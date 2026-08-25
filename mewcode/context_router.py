"""Deterministic hybrid retrieval for local Tool, Skill, and Memory headers.

The router combines token/BM25-like lexical evidence with Unicode character
n-grams, applies hard scope filters, then uses MMR to avoid spending the
context budget on near-duplicates.  It deliberately has no model dependency:
callers can add an optional reranker later without making the main loop block.
"""

from __future__ import annotations

import math
import re
import time
import unicodedata
from collections import Counter
from enum import StrEnum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator


_WORD_RE = re.compile(r"[\w.+#/-]+", re.UNICODE)


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _terms(value: str) -> tuple[str, ...]:
    return tuple(_WORD_RE.findall(normalize_text(value)))


def _ngrams(value: str, sizes: tuple[int, ...] = (2, 3)) -> frozenset[str]:
    compact = "".join(character for character in normalize_text(value) if not character.isspace())
    grams: set[str] = set()
    for size in sizes:
        grams.update(
            compact[index : index + size]
            for index in range(max(0, len(compact) - size + 1))
        )
    return frozenset(grams)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class ArtifactKind(StrEnum):
    TOOL = "tool"
    SKILL = "skill"
    MEMORY = "memory"
    CODE_SYMBOL = "code_symbol"
    EXPERIENCE = "experience"


class RouterArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1, max_length=256)
    kind: ArtifactKind
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=16_000)
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    project_fingerprint: str | None = None
    language: str | None = None
    risk_level: str | None = None
    estimated_tokens: int = Field(default=1, ge=1)

    @field_validator("artifact_id", "name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @property
    def searchable_text(self) -> str:
        return " ".join((self.name, *self.aliases, *self.keywords, self.description))


class RouterQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=8_000)
    kinds: frozenset[ArtifactKind] = frozenset()
    project_fingerprint: str | None = None
    language: str | None = None
    maximum_risk_level: str | None = None
    max_results: int = Field(default=5, ge=0, le=100)
    token_budget: int = Field(default=4_096, ge=0)
    already_surfaced: frozenset[str] = frozenset()

    @field_validator("text")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class RoutedArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: RouterArtifact
    lexical_score: float = Field(ge=0.0)
    ngram_score: float = Field(ge=0.0, le=1.0)
    relevance_score: float = Field(ge=0.0)
    mmr_score: float


class RouterMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_count: int = Field(ge=0)
    filtered_count: int = Field(ge=0)
    surfaced_filtered_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    selected_tokens: int = Field(ge=0)
    budget_skipped_count: int = Field(ge=0)
    fallback_used: bool = False
    latency_ms: float = Field(ge=0.0)


class RouterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[RoutedArtifact, ...]
    metrics: RouterMetrics


class HybridContextRouter:
    """In-memory local hybrid index with deterministic ranking and MMR."""

    _RISK_ORDER = {"low": 0, "l0": 0, "medium": 1, "l1": 1, "high": 2, "l2": 2, "l3": 3, "l4": 4}

    def __init__(self, artifacts: Iterable[RouterArtifact] = (), *, mmr_lambda: float = 0.78) -> None:
        if not 0.0 <= mmr_lambda <= 1.0:
            raise ValueError("mmr_lambda must be between zero and one")
        self.mmr_lambda = mmr_lambda
        self._artifacts: dict[str, RouterArtifact] = {}
        self._terms: dict[str, tuple[str, ...]] = {}
        self._ngrams: dict[str, frozenset[str]] = {}
        self.replace(artifacts)

    def replace(self, artifacts: Iterable[RouterArtifact]) -> None:
        indexed: dict[str, RouterArtifact] = {}
        for artifact in artifacts:
            if artifact.artifact_id in indexed:
                raise ValueError(f"duplicate artifact_id: {artifact.artifact_id}")
            indexed[artifact.artifact_id] = artifact
        self._artifacts = indexed
        self._terms = {key: _terms(value.searchable_text) for key, value in indexed.items()}
        self._ngrams = {key: _ngrams(value.searchable_text) for key, value in indexed.items()}

    def upsert(self, artifact: RouterArtifact) -> None:
        self._artifacts[artifact.artifact_id] = artifact
        self._terms[artifact.artifact_id] = _terms(artifact.searchable_text)
        self._ngrams[artifact.artifact_id] = _ngrams(artifact.searchable_text)

    def remove(self, artifact_id: str) -> None:
        self._artifacts.pop(artifact_id, None)
        self._terms.pop(artifact_id, None)
        self._ngrams.pop(artifact_id, None)

    def get(self, artifact_id: str) -> RouterArtifact | None:
        return self._artifacts.get(artifact_id)

    def artifacts(self) -> tuple[RouterArtifact, ...]:
        return tuple(self._artifacts[key] for key in sorted(self._artifacts))

    def _allowed(self, artifact: RouterArtifact, query: RouterQuery) -> bool:
        if query.kinds and artifact.kind not in query.kinds:
            return False
        if artifact.project_fingerprint and normalize_text(artifact.project_fingerprint) != normalize_text(query.project_fingerprint or ""):
            return False
        if artifact.language and normalize_text(artifact.language) != normalize_text(query.language or ""):
            return False
        if artifact.risk_level and query.maximum_risk_level:
            actual = self._RISK_ORDER.get(normalize_text(artifact.risk_level), math.inf)
            maximum = self._RISK_ORDER.get(normalize_text(query.maximum_risk_level), -1)
            if actual > maximum:
                return False
        return True

    def search(self, query: RouterQuery | str, **changes: object) -> RouterResult:
        started = time.perf_counter()
        request = RouterQuery(text=query, **changes) if isinstance(query, str) else query
        all_artifacts = tuple(self._artifacts.values())
        eligible = [artifact for artifact in all_artifacts if self._allowed(artifact, request)]
        surfaced = {
            normalize_text(item) for item in request.already_surfaced
        }
        unsurfaced = [
            artifact
            for artifact in eligible
            if normalize_text(artifact.artifact_id) not in surfaced
            and normalize_text(artifact.name) not in surfaced
        ]
        query_terms = _terms(request.text)
        query_term_set = set(query_terms)
        query_counts = Counter(query_terms)
        query_grams = _ngrams(request.text)
        document_frequency = Counter(
            term for artifact in unsurfaced for term in set(self._terms[artifact.artifact_id])
        )
        total_documents = max(1, len(unsurfaced))
        ranked: list[tuple[float, float, float, RouterArtifact]] = []
        for artifact in unsurfaced:
            terms = self._terms[artifact.artifact_id]
            counts = Counter(terms)
            lexical = 0.0
            for term, query_frequency in query_counts.items():
                tf = counts[term]
                if not tf:
                    continue
                idf = math.log(1.0 + (total_documents + 1) / (document_frequency[term] + 1))
                lexical += query_frequency * (tf / (tf + 0.75)) * idf
            name = normalize_text(artifact.name)
            normalized_query = normalize_text(request.text)
            if normalized_query == name:
                lexical += 10.0
            elif normalized_query and normalized_query in name:
                lexical += 4.0
            ngram = _jaccard(query_grams, self._ngrams[artifact.artifact_id])
            relevance = lexical + 3.0 * ngram
            term_overlap = bool(query_term_set & set(terms))
            substring_match = bool(normalized_query and normalized_query in normalize_text(artifact.searchable_text))
            # N-gram overlap alone is useful for CJK and short typos, but long
            # unrelated identifiers share incidental grams. Require a modest
            # similarity floor when there is no lexical anchor.
            short_non_ascii_query = len(normalized_query) <= 8 and any(
                ord(character) > 127 for character in normalized_query
            )
            if relevance > 0.0 and (
                term_overlap
                or substring_match
                or ngram >= 0.10
                or (short_non_ascii_query and ngram > 0.0)
            ):
                ranked.append((relevance, lexical, ngram, artifact))

        candidates = sorted(ranked, key=lambda item: (-item[0], item[3].artifact_id))
        selected: list[RoutedArtifact] = []
        selected_grams: list[frozenset[str]] = []
        remaining = request.token_budget
        budget_skips = 0
        while candidates and len(selected) < request.max_results:
            best_index = 0
            best_mmr = -math.inf
            for index, (relevance, _lexical, _ngram, artifact) in enumerate(candidates):
                novelty_penalty = max(
                    (_jaccard(self._ngrams[artifact.artifact_id], grams) for grams in selected_grams),
                    default=0.0,
                )
                mmr = self.mmr_lambda * relevance - (1.0 - self.mmr_lambda) * novelty_penalty
                if mmr > best_mmr:
                    best_index, best_mmr = index, mmr
            relevance, lexical, ngram, artifact = candidates.pop(best_index)
            if artifact.estimated_tokens > remaining:
                budget_skips += 1
                continue
            selected.append(
                RoutedArtifact(
                    artifact=artifact,
                    lexical_score=lexical,
                    ngram_score=ngram,
                    relevance_score=relevance,
                    mmr_score=best_mmr,
                )
            )
            selected_grams.append(self._ngrams[artifact.artifact_id])
            remaining -= artifact.estimated_tokens

        elapsed = (time.perf_counter() - started) * 1000
        return RouterResult(
            items=tuple(selected),
            metrics=RouterMetrics(
                candidate_count=len(all_artifacts),
                filtered_count=len(all_artifacts) - len(eligible),
                surfaced_filtered_count=len(eligible) - len(unsurfaced),
                selected_count=len(selected),
                selected_tokens=request.token_budget - remaining,
                budget_skipped_count=budget_skips,
                latency_ms=elapsed,
            ),
        )


__all__ = [
    "ArtifactKind",
    "HybridContextRouter",
    "RoutedArtifact",
    "RouterArtifact",
    "RouterMetrics",
    "RouterQuery",
    "RouterResult",
    "normalize_text",
]
