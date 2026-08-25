from __future__ import annotations

from mewcode.context_router import (
    ArtifactKind,
    HybridContextRouter,
    RouterArtifact,
    RouterQuery,
)


def artifact(
    artifact_id: str,
    name: str,
    description: str,
    *,
    tokens: int = 10,
    project: str | None = None,
    language: str | None = None,
) -> RouterArtifact:
    return RouterArtifact(
        artifact_id=artifact_id,
        kind=ArtifactKind.TOOL,
        name=name,
        description=description,
        estimated_tokens=tokens,
        project_fingerprint=project,
        language=language,
    )


def test_hybrid_router_handles_cjk_typo_and_exact_name() -> None:
    router = HybridContextRouter(
        (
            artifact("tool:read", "ReadFile", "Read a source file from disk"),
            artifact("tool:search", "CodeSearch", "代码搜索与符号查找"),
        )
    )
    exact = router.search("ReadFile", max_results=1)
    assert exact.items[0].artifact.name == "ReadFile"
    cjk = router.search("代码搜锁", max_results=1)
    assert cjk.items[0].artifact.name == "CodeSearch"
    assert cjk.items[0].ngram_score > 0


def test_scope_surfaced_and_token_budget_are_hard_filters() -> None:
    router = HybridContextRouter(
        (
            artifact(
                "project-a",
                "PythonDeploy",
                "deploy python service",
                project="a",
                language="python",
                tokens=30,
            ),
            artifact(
                "project-b",
                "PythonDeployOther",
                "deploy python service",
                project="b",
                language="python",
                tokens=5,
            ),
            artifact("generic", "Deploy", "deploy python service", tokens=20),
        )
    )
    result = router.search(
        RouterQuery(
            text="deploy python",
            project_fingerprint="a",
            language="python",
            already_surfaced=frozenset({"project-a"}),
            token_budget=19,
            max_results=5,
        )
    )
    assert result.items == ()
    assert result.metrics.filtered_count == 1
    assert result.metrics.surfaced_filtered_count == 1
    assert result.metrics.budget_skipped_count == 1


def test_mmr_prefers_diverse_results() -> None:
    router = HybridContextRouter(
        (
            artifact("a", "SearchTests", "search tests by name"),
            artifact("b", "SearchTestFiles", "search test files by name"),
            artifact("c", "SearchSymbols", "search code symbols and definitions"),
        ),
        mmr_lambda=0.4,
    )
    result = router.search("search tests symbols", max_results=2)
    names = {item.artifact.name for item in result.items}
    assert "SearchSymbols" in names
    assert len(names) == 2
