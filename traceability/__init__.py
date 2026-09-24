"""批次分级追溯领域包。"""
from __future__ import annotations

from .audit import build_box_report, render_box_report
from .chain import ChainReport, verify_lot
from .errors import (
    AuthorizationError,
    ConditionalReleaseError,
    DomainError,
    ImmutableHistoryError,
    ReleaseBlocked,
    SamplePlanViolation,
    SamplingRuleNotFound,
    ValidationError,
)
from .roles import Role, Stage, STAGE_ROLE
from .rules import AcceptanceRule, GradeLevel, RuleRegistry, Verdict, evaluate_grade
from .service import QualityTraceService
from .store import Store

__all__ = [
    "QualityTraceService",
    "Store",
    "Role",
    "Stage",
    "STAGE_ROLE",
    "AcceptanceRule",
    "GradeLevel",
    "RuleRegistry",
    "Verdict",
    "evaluate_grade",
    "ChainReport",
    "verify_lot",
    "build_box_report",
    "render_box_report",
    "DomainError",
    "ValidationError",
    "AuthorizationError",
    "ReleaseBlocked",
    "ConditionalReleaseError",
    "ImmutableHistoryError",
    "SamplePlanViolation",
    "SamplingRuleNotFound",
]
