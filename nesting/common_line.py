from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from shapely.geometry.base import BaseGeometry


@dataclass(frozen=True)
class CommonLineDecision:
    enabled: bool
    extra_clearance: float
    reason: str = ""


class CommonLinePolicy(Protocol):
    """Extension interface for common-line cutting (CLC).

    A production CLC policy can inspect two edges and return zero additional
    clearance for collinear compatible edges. The default policy deliberately
    keeps normal clearance to preserve conventional separated toolpaths.
    """

    name: str

    def clearance_adjustment(
        self,
        first: BaseGeometry,
        second: BaseGeometry,
        base_clearance: float,
    ) -> CommonLineDecision:
        ...


class DisabledCommonLinePolicy:
    name = "disabled"

    def clearance_adjustment(
        self,
        first: BaseGeometry,
        second: BaseGeometry,
        base_clearance: float,
    ) -> CommonLineDecision:
        return CommonLineDecision(False, base_clearance, "保持普通安全间距")


class CommonLineCutPolicy(DisabledCommonLinePolicy):
    """Placeholder for a future exact collinear-edge matching policy."""

    name = "enabled"

    def clearance_adjustment(
        self,
        first: BaseGeometry,
        second: BaseGeometry,
        base_clearance: float,
    ) -> CommonLineDecision:
        raise NotImplementedError("共边切割几何匹配尚未启用")


def create_common_line_policy(name: str) -> CommonLinePolicy:
    normalized = name.strip().lower()
    if normalized == "disabled":
        return DisabledCommonLinePolicy()
    if normalized == "enabled":
        return CommonLineCutPolicy()
    raise ValueError(f"未知共边切割策略：{name}")
