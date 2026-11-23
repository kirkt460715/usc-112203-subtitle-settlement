"""合同口径的缺陷判定：关键术语错误与普通迟到绝不混用。

两类合同罚则（contracts.jsonl，单位为结算货币分）
--------------------------------------------------
critical_penalty   关键术语严重错误：必须同时满足
                   1) 人工复核确认 wrong=true；
                   2) 该片段属于 critical 术语级别；
                   3) terminology.jsonl 中存在覆盖该语种/渠道的术语表回执，
                      且交付内容与当时已生效的纠正版本不一致。
regular_penalty    普通迟到与一般错译/漏译：上述关键术语口径不成立时，
                   只能按普通罚则提出，不得按全语种错误率套用 critical。

坏时长上限 max_per_bad_second：同一可见范围的同一坏时段，罚额按
ceil(坏毫秒/1000) * max_per_bad_second 封顶，避免短坏窗被整笔重罚。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .coverage import (
    UNVERIFIED,
    verification_index,
)
from .metrics import (
    DeliveryMetric,
    MetricBundle,
    MACHINE_ONLY_SCREEN,
    NATURAL_PAUSE,
    NO_VISIBLE_TIME,
    SCREEN_LATE,
    UPSTREAM_LATE,
)
from .records import ProjectRecords

CRITICAL_TERM = "critical_term_error"
LATENESS = "lateness"
OMISSION = "omission"
CONTENT_WRONG = "content_wrong"

# 责任相位到供应环节的映射（机器字幕/人工修订属上游，屏幕渲染属下游）。
UPSTREAM = "upstream"
DOWNSTREAM = "downstream"
JOINT = "joint_unresolved"


@dataclass(frozen=True)
class Defect:
    delivery_id: str
    kind: str
    critical: bool
    responsible_stage: str
    bad_window_ms: int
    base_penalty_cents: int
    capped_penalty_cents: int
    cap_applied: bool
    evidence_ids: tuple[str, ...]
    evidence_methods: tuple[str, ...]
    term_record_ids: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def cap_for_bad_window(contract: dict[str, Any], bad_window_ms: int) -> int:
    """按合同明示的 max_per_bad_second 计算坏时长封顶（分）。"""
    return math.ceil(bad_window_ms / 1000) * contract["max_per_bad_second"]


def _penalty(contract: dict[str, Any], critical: bool, bad_window_ms: int) -> tuple[int, int, bool]:
    base = contract["critical_penalty"] if critical else contract["regular_penalty"]
    cap = cap_for_bad_window(contract, bad_window_ms)
    return base, min(base, cap), base > cap


def term_evidence_for(project: ProjectRecords, metric: DeliveryMetric
                      ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回覆盖该交付的术语表（初始版、纠正版）。

    同一工作室给同一语种的各渠道供同一版术语表，terminology 回执只登记
    first 渠道时，replay 按 语种+片段 回退匹配；责任期限仍按各渠道实际
    收到纠正版的时间分开，不用全语种错误率一刀切。
    """
    exact = [
        t for t in project.table("terminology")
        if t["segment"] == metric.segment_id
        and t["language"] == metric.language
        and t["channel"] == metric.channel
    ]
    rows = exact or [
        t for t in project.table("terminology")
        if t["segment"] == metric.segment_id and t["language"] == metric.language
    ]
    initial = [t for t in rows if "supersedes" not in t]
    corrected = [t for t in rows if "supersedes" in t]
    return initial, corrected


def detect_defects(project: ProjectRecords, bundle: MetricBundle) -> list[Defect]:
    """对全部可核实交付做合同口径缺陷判定；不可测量/未抽查项不出罚单。"""
    verify = verification_index(project, bundle)
    # 被 supersedes 链引用的补播回执（如 R2）即使自身没有抽查行，也因被
    # 扣减单直接引用而可测量；证据沿同一播出凭证的原始版本继承。
    chain_evidence: dict[str, tuple[dict[str, Any], ...]] = {}
    for delivery in project.table("deliveries"):
        if delivery.get("supersedes"):
            root_id = delivery["supersedes"]
            chain_evidence[delivery["id"]] = (
                verify[root_id].scheduled_checks + verify[root_id].complaint_checks
            )
    defects: list[Defect] = []
    for metric in bundle.metrics:
        if NATURAL_PAUSE in metric.phases:
            continue  # 原音停顿无字幕义务
        v = verify[metric.delivery_id]
        inherited = chain_evidence.get(metric.delivery_id, ())
        checks = list(v.scheduled_checks + v.complaint_checks) + list(inherited)
        evidence_ids = tuple(c["id"] for c in checks)
        evidence_methods = tuple(c["method"] for c in checks)
        contract = project.contract_for(metric.language)

        # 没有任何人工复核证据的交付保持“不可核实”，绝不按合格处理也不产生罚单。
        if v.status == UNVERIFIED and not inherited:
            continue

        if NO_VISIBLE_TIME in metric.phases:
            # 漏译：回执无法确定任何可见时间，且人工复核确认 complete=false。
            confirmed_omission = any(not c["complete"] for c in checks)
            if confirmed_omission:
                bad_window = metric.audio_ms
                base, capped, cap_applied = _penalty(contract, False, bad_window)
                defects.append(Defect(
                    delivery_id=metric.delivery_id,
                    kind=OMISSION,
                    critical=False,
                    responsible_stage=UPSTREAM,
                    bad_window_ms=bad_window,
                    base_penalty_cents=base,
                    capped_penalty_cents=capped,
                    cap_applied=cap_applied,
                    evidence_ids=evidence_ids,
                    evidence_methods=evidence_methods,
                    term_record_ids=(),
                    reason="人工复核确认字幕从未上屏（complete=false 且 screen_ms=null），按整段发言时长计信息缺口",
                ))
            continue

        wrong_confirmed = any(c["wrong"] for c in checks)
        if wrong_confirmed:
            initial, corrected = term_evidence_for(project, metric)
            term_ids = tuple(t["id"] for t in initial + corrected)
            if corrected and metric.glossary == "critical":
                # 关键术语错误成立的唯一路径：有纠正版术语表，而交付仍被确认 wrong。
                bad_window = _content_bad_window(metric)
                base, capped, cap_applied = _penalty(contract, True, bad_window)
                stage = _term_stage(metric, corrected[0])
                defects.append(Defect(
                    delivery_id=metric.delivery_id,
                    kind=CRITICAL_TERM,
                    critical=True,
                    responsible_stage=stage,
                    bad_window_ms=bad_window,
                    base_penalty_cents=base,
                    capped_penalty_cents=capped,
                    cap_applied=cap_applied,
                    evidence_ids=evidence_ids,
                    evidence_methods=evidence_methods,
                    term_record_ids=term_ids,
                    reason=(f"critical 片段且纠正版术语表 {corrected[0]['id']} 已由"
                            f"{metric.channel} 渠道接收，人工复核仍确认 wrong=true"),
                ))
            else:
                # 级别为 critical 但没有术语表回执：critical 口径证据不足，
                # 只能按一般错译处理，禁止直接按全语种 critical 率扣款。
                bad_window = _content_bad_window(metric)
                base, capped, cap_applied = _penalty(contract, False, bad_window)
                defects.append(Defect(
                    delivery_id=metric.delivery_id,
                    kind=CONTENT_WRONG,
                    critical=False,
                    responsible_stage=_content_stage(metric),
                    bad_window_ms=bad_window,
                    base_penalty_cents=base,
                    capped_penalty_cents=capped,
                    cap_applied=cap_applied,
                    evidence_ids=evidence_ids,
                    evidence_methods=evidence_methods,
                    term_record_ids=term_ids,
                    reason=("人工复核确认 wrong=true，但无覆盖该语种/渠道的术语表纠正回执，"
                            "critical 口径不成立，按普通错译处理"
                            if metric.glossary == "critical"
                            else "人工复核确认 wrong=true，按普通错译处理"),
                ))

        if SCREEN_LATE in metric.phases:
            bad_window = metric.late_bad_window_ms or 0
            base, capped, cap_applied = _penalty(contract, False, bad_window)
            stage = _lateness_stage(metric)
            defects.append(Defect(
                delivery_id=metric.delivery_id,
                kind=LATENESS,
                critical=False,
                responsible_stage=stage,
                bad_window_ms=bad_window,
                base_penalty_cents=base,
                capped_penalty_cents=capped,
                cap_applied=cap_applied,
                evidence_ids=evidence_ids,
                evidence_methods=evidence_methods,
                term_record_ids=(),
                reason=_lateness_reason(metric),
            ))
    return defects


def _content_bad_window(metric: DeliveryMetric) -> int:
    """错译坏窗口：观众实际看到错误字幕的可见跨度。"""
    if metric.screen_ms is None or metric.screen_end_ms is None:
        return metric.audio_ms
    return max(0, metric.screen_end_ms - metric.screen_ms)


def _term_stage(metric: DeliveryMetric, corrected_term: dict[str, Any]) -> str:
    """术语错误的责任期限按各渠道实际收到纠正版的时间分开计算。"""
    received = datetime.fromisoformat(metric.received)
    corrected_at = datetime.fromisoformat(corrected_term["received"])
    if received <= corrected_at:
        # 交付发生在渠道收到纠正之前：错误源自上游术语表。
        return UPSTREAM
    # 渠道已拿到纠正仍渲染旧术语：下游责任。
    return DOWNSTREAM


def _content_stage(metric: DeliveryMetric) -> str:
    if UPSTREAM_LATE in metric.phases and SCREEN_LATE in metric.phases:
        return JOINT
    if UPSTREAM_LATE in metric.phases:
        return UPSTREAM
    if MACHINE_ONLY_SCREEN in metric.phases or SCREEN_LATE in metric.phases:
        return DOWNSTREAM
    return UPSTREAM


def _lateness_stage(metric: DeliveryMetric) -> str:
    if UPSTREAM_LATE in metric.phases and SCREEN_LATE in metric.phases:
        # 两个环节都能独立造成迟到：先标 joint，由反证组在 claims 层拆分。
        return JOINT
    if UPSTREAM_LATE in metric.phases:
        return UPSTREAM
    return DOWNSTREAM


def _lateness_reason(metric: DeliveryMetric) -> str:
    parts = [f"观众屏幕时刻 {metric.screen_ms}ms 晚于截止 {metric.deadline_ms}ms"]
    if UPSTREAM_LATE in metric.phases:
        parts.append(f"人工修订 {metric.edit_ms}ms 本身迟到")
    if MACHINE_ONLY_SCREEN in metric.phases:
        parts.append(f"机器字幕 {metric.machine_ms}ms 先上屏、修订 {metric.edit_ms}ms 后落地")
    return "；".join(parts)


def defects_for_delivery(project: ProjectRecords, bundle: MetricBundle,
                         delivery_id: str) -> list[Defect]:
    return [d for d in detect_defects(project, bundle) if d.delivery_id == delivery_id]


def confirmed_amount_for_delivery(defects: list[Defect], delivery_id: str
                                  ) -> dict[str, Any]:
    """同一播出凭证的可确认罚额与坏窗口合并视图。

    同一坏时段不重复扣款：同一条交付上重叠的坏窗口只计一次；
    迟到窗口与内容错误窗口若不重叠则分别按各自口径计罚后求和，
    critical 与 regular 的罚率口径始终不互换。
    """
    mine = [d for d in defects if d.delivery_id == delivery_id]
    by_kind: dict[str, list[Defect]] = {}
    for defect in mine:
        by_kind.setdefault(defect.kind, []).append(defect)

    merged: dict[str, dict[str, Any]] = {}
    total = 0
    for kind, group in by_kind.items():
        window = max(d.bad_window_ms for d in group)  # 同类窗口重叠，取最大
        penalty = max(d.capped_penalty_cents for d in group)
        merged[kind] = {
            "bad_window_ms": window,
            "capped_penalty_cents": penalty,
            "critical": group[0].critical,
            "evidence_ids": sorted({e for d in group for e in d.evidence_ids}),
        }
        total += penalty
    return {
        "delivery_id": delivery_id,
        "total_cents": total,
        "windows": merged,
        "defects": mine,
    }
