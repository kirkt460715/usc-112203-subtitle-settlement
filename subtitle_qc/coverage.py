"""可核实样本覆盖：解释语种、赛段与覆盖强度的可比范围。

抽查计划（checks.method=scheduled）带纳入概率 inclusion，是唯一的概率样本；
客诉（method=complaint）的 inclusion 为 null，只作证据线索，不能外推总体，
更不能把从未抽查的片段算成完全合格。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .metrics import MetricBundle, NO_VISIBLE_TIME
from .records import ProjectRecords

SCHEDULED = "scheduled"
COMPLAINT = "complaint"

# 覆盖强度按计划抽样的实际比例分档，跨语种比较时必须连同强度一起读。
STRONG = "strong"          # 计划抽样比例 >= 0.5
PARTIAL = "partial"        # 0 < 计划抽样比例 < 0.5
EVIDENCE_ONLY = "evidence_only"   # 只有客诉，无概率样本
UNVERIFIED = "unverified"  # 没有任何人工复核证据


@dataclass(frozen=True)
class Verification:
    delivery_id: str
    status: str
    scheduled_checks: tuple[dict[str, Any], ...]
    complaint_checks: tuple[dict[str, Any], ...]
    inclusion: float | None
    measured: bool          # 观众体验是否可基于回执测量


def verification_index(project: ProjectRecords,
                       bundle: MetricBundle) -> dict[str, Verification]:
    index: dict[str, Verification] = {}
    for metric in bundle.metrics:
        checks = project.checks_for(metric.delivery_id)
        scheduled = tuple(c for c in checks if c["method"] == SCHEDULED)
        complaints = tuple(c for c in checks if c["method"] == COMPLAINT)
        inclusion = scheduled[0]["inclusion"] if scheduled else None
        if scheduled:
            status = STRONG if inclusion >= 0.5 else PARTIAL
        elif complaints:
            status = EVIDENCE_ONLY
        else:
            status = UNVERIFIED
        # 无发言片段本就无字幕义务；screen_ms=null 的发言片段不可测量。
        measured = NO_VISIBLE_TIME not in metric.phases
        index[metric.delivery_id] = Verification(
            delivery_id=metric.delivery_id,
            status=status,
            scheduled_checks=scheduled,
            complaint_checks=complaints,
            inclusion=inclusion,
            measured=measured,
        )
    return index


@dataclass(frozen=True)
class CoverageCell:
    language: str
    part: str | None
    channel: str | None
    population: int
    scheduled_sampled: int
    complaint_only: int
    unverified: int
    scheduled_inclusions: tuple[float, ...]
    strength: str
    scheduled_weighted_viewers: float | None
    population_viewers: int


def _strength(sampled: int, population: int) -> str:
    if sampled == 0:
        return UNVERIFIED
    ratio = sampled / population
    return STRONG if ratio >= 0.5 else PARTIAL


def coverage_report(project: ProjectRecords, bundle: MetricBundle,
                    parts: bool = True) -> list[CoverageCell]:
    """按语种 x 渠道（可选再按赛段 part）汇总覆盖。

    scheduled_weighted_viewers 是按 1/inclusion 放大的观众量估计，只覆盖
    计划抽样单元格；客诉不参与放大，避免把非概率样本当成总体推断。
    """
    verify = verification_index(project, bundle)
    groups: dict[tuple[str, str | None, str | None], list[Any]] = {}
    for metric in bundle.metrics:
        part = project.segment(metric.segment_id)["part"] if parts else None
        key = (metric.language, part, metric.channel)
        groups.setdefault(key, []).append(metric)

    cells: list[CoverageCell] = []
    for key in sorted(groups):
        language, part, channel = key
        members = groups[key]
        sampled = [m for m in members if verify[m.delivery_id].scheduled_checks]
        complaint_only = [m for m in members
                          if verify[m.delivery_id].status == EVIDENCE_ONLY]
        unverified = [m for m in members
                      if verify[m.delivery_id].status == UNVERIFIED]
        inclusions = tuple(sorted({
            verify[m.delivery_id].inclusion for m in sampled
        }))
        weighted = None
        if sampled:
            weighted = sum(
                m.viewer_count / verify[m.delivery_id].inclusion for m in sampled
            )
        cells.append(CoverageCell(
            language=language,
            part=part,
            channel=channel,
            population=len(members),
            scheduled_sampled=len(sampled),
            complaint_only=len(complaint_only),
            unverified=len(unverified),
            scheduled_inclusions=inclusions,
            strength=_strength(len(sampled), len(members)),
            scheduled_weighted_viewers=weighted,
            population_viewers=sum(m.viewer_count for m in members),
        ))
    return cells


def comparable_scope(project: ProjectRecords,
                     bundle: MetricBundle) -> dict[str, dict[str, Any]]:
    """每个语种一行，给出可与其它语种比较的覆盖范围与强度说明。"""
    verify = verification_index(project, bundle)
    scope: dict[str, dict[str, Any]] = {}
    for language in sorted({m.language for m in bundle.metrics}):
        members = [m for m in bundle.metrics if m.language == language]
        sampled = [m for m in members if verify[m.delivery_id].scheduled_checks]
        parts_covered = sorted({
            project.segment(m.segment_id)["part"] for m in sampled
        })
        parts_missing = sorted({
            project.segment(m.segment_id)["part"] for m in members
        } - set(parts_covered))
        # 语种整体强度取该语种下最强单元格：只要有 0.5 计划抽样的渠道/赛段，
        # 跨语种比较时该语种即具备 strong 锚点，同时仍单列缺样本赛段。
        cell_ratios = [
            c.scheduled_sampled / c.population
            for c in coverage_report(project, bundle, parts=True)
            if c.language == language
        ]
        anchor = STRONG if cell_ratios and max(cell_ratios) >= 0.5 else PARTIAL
        # 整体可外推性仍受最弱单元格约束：存在无概率样本赛段时只能算局部覆盖。
        strength = PARTIAL if parts_missing else anchor
        scope[language] = {
            "population_deliveries": len(members),
            "scheduled_sampled": len(sampled),
            "complaint_only": sum(
                1 for m in members if verify[m.delivery_id].status == EVIDENCE_ONLY),
            "unverified": sum(
                1 for m in members if verify[m.delivery_id].status == UNVERIFIED),
            "inclusion": (sorted({verify[m.delivery_id].inclusion
                                  for m in sampled}) or [None])[0],
            "parts_with_probability_sample": parts_covered,
            "parts_without_probability_sample": parts_missing,
            "strength": strength,
        }
    return scope
