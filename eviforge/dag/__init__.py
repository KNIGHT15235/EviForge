"""Typed Agent DAGs with evidence and resumable checkpoints."""
from eviforge.dag.graph import DAGError, DriftError, ReplayRequired, validate_graph
from eviforge.dag.models import (GraphSpec, NodeSpec, InputRef, CommandSpec, ArtifactRef,
                                 DAGRunResult, NodeResult, OUTPUT_ADAPTER, INPUT_ADAPTER)

def __getattr__(name):
    if name == "DAGRunner":
        from eviforge.dag.scheduler import DAGRunner
        return DAGRunner
    raise AttributeError(name)

__all__ = ["DAGRunner", "GraphSpec", "NodeSpec", "InputRef", "CommandSpec", "ArtifactRef",
           "DAGRunResult", "NodeResult", "DAGError", "DriftError", "ReplayRequired",
           "validate_graph", "OUTPUT_ADAPTER", "INPUT_ADAPTER"]
