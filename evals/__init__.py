"""Reproducible, cluster-aware evaluation utilities for EviForge.

The package intentionally has no dependency on a model provider.  It records
mechanism benchmarks and LLM-backed experiments with the same append-only run
schema, while keeping those two evidence classes explicitly separated.
"""

from evals.schema import DATASET_SCHEMA_VERSION, RUN_SCHEMA_VERSION, EvalCase

__all__ = ["DATASET_SCHEMA_VERSION", "RUN_SCHEMA_VERSION", "EvalCase"]
