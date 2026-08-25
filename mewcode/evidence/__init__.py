"""Evidence-first completion harness.

This package is intentionally usable without the interactive AgentLoop.  That
keeps verification deterministic and makes it suitable for both the TUI and CI.
"""

from .bundle import BundleExporter, BundlePaths
from .gate import EvidenceGate
from .models import (
    ArtifactRef,
    CommandEvidence,
    CommandStatus,
    CriterionEvidence,
    CriterionStatus,
    EvidenceBundle,
    GateDecision,
    GateVerdict,
    RequirementContract,
    RequirementCriterion,
    VerificationReceipt,
    VerifierSpec,
    WorkspaceSnapshot,
)
from .orchestrator import EvidenceOrchestrator, EvidenceRunResult
from .runner import TrustedArgvRunner
from .store import EvidenceStore, default_control_plane_root
from .workspace import GitWorkspace, WorkspaceError, capture_workspace

__all__ = [
    "ArtifactRef",
    "BundleExporter",
    "BundlePaths",
    "CommandEvidence",
    "CommandStatus",
    "CriterionEvidence",
    "CriterionStatus",
    "EvidenceBundle",
    "EvidenceGate",
    "EvidenceOrchestrator",
    "EvidenceRunResult",
    "EvidenceStore",
    "GateDecision",
    "GateVerdict",
    "GitWorkspace",
    "RequirementContract",
    "RequirementCriterion",
    "TrustedArgvRunner",
    "VerificationReceipt",
    "VerifierSpec",
    "WorkspaceError",
    "WorkspaceSnapshot",
    "capture_workspace",
    "default_control_plane_root",
]
