"""Deterministic quarantine checks for trace-derived experience text."""

from __future__ import annotations

import re
from dataclasses import dataclass

from mewcode.evolution.models import ExperienceCandidate


_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "prompt_injection",
        re.compile(
            r"(?:ignore|disregard|override)\s+(?:all\s+)?(?:previous|system|developer)"
            r"|忽略(?:之前|以上|系统|开发者).{0,12}(?:指令|规则)",
            re.IGNORECASE,
        ),
    ),
    (
        "policy_bypass",
        re.compile(
            r"(?:bypass|disable|skip).{0,24}(?:permission|approval|security|test)"
            r"|(?:绕过|关闭|跳过).{0,16}(?:权限|审批|安全|测试)",
            re.IGNORECASE,
        ),
    ),
    (
        "credential_material",
        re.compile(
            r"(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*['\"]?"
            r"[A-Za-z0-9_./+=-]{8,}",
            re.IGNORECASE,
        ),
    ),
    (
        "private_absolute_path",
        re.compile(
            r"(?:[A-Za-z]:\\Users\\[^\\\s]+\\|/(?:home|Users)/[^/\s]+/)"
        ),
    ),
    (
        "external_exfiltration",
        re.compile(
            r"(?:upload|exfiltrat|send).{0,32}(?:credential|secret|token|private)"
            r"|(?:上传|外传|发送).{0,20}(?:凭证|密钥|令牌|隐私)",
            re.IGNORECASE,
        ),
    ),
    (
        "source_disclosure",
        re.compile(
            r"(?:print|show|reveal|dump|upload|send).{0,32}(?:all\s+)?source\s+code"
            r"|(?:输出|显示|泄露|上传|发送).{0,20}(?:全部|所有)?.{0,8}(?:源代码|源码)",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class SanitizationReport:
    safe: bool
    reasons: tuple[str, ...]


class CandidateSanitizer:
    """Classify unsafe content without executing or rewriting candidate text."""

    def inspect(self, candidate: ExperienceCandidate) -> SanitizationReport:
        chunks = (
            candidate.task_signature,
            candidate.failure_signature,
            candidate.root_cause_family,
            candidate.symptom,
            candidate.decision,
            *candidate.context_constraints,
            *candidate.procedure,
            *candidate.failed_attempts,
        )
        text = "\n".join(chunks)
        reasons = set(candidate.security_flags)
        for name, pattern in _RULES:
            if pattern.search(text):
                reasons.add(name)
        ordered = tuple(sorted(reasons))
        return SanitizationReport(safe=not ordered, reasons=ordered)
