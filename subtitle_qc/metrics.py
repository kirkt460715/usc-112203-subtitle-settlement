"""观众实际经历的字幕延迟与信息缺口计算。

时间字段语义（deliveries.jsonl，均以原音出现时刻为零点、单位毫秒）
------------------------------------------------------------------
machine_ms   机器字幕可用时刻（上游转写第一道产物）
edit_ms      人工修订可用时刻（上游供稿最终时刻）
screen_ms    字幕真正出现在观众屏幕上的时刻；null 表示回执未确定可见时间
screen_end_ms 该条字幕在观众屏幕上消失的时刻

必须区分的四种经历（对应业务方争论的责任边界）
----------------------------------------------
natural_pause        原音本来就没有发言（segments.speech=false），无字幕义务
upstream_late        人工修订本身晚于合同截止时间（edit_ms > deadline）
machine_only_screen  机器字幕先上屏、人工修订当时尚未落地
                     （screen_ms < edit_ms）：同一句的下游信号重放
screen_late          观众真正看到字幕的时刻晚于截止时间（screen_ms > deadline）
machine/edit/screen 三个时刻互不保证先后顺序，因此相位按各时刻独立判定，
同一条交付可同时带多个相位标签；是否重复扣款由 claims 层按合同处理。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .records import ProjectRecords

NATURAL_PAUSE = "natural_pause"
UPSTREAM_LATE = "upstream_late"
MACHINE_ONLY_SCREEN = "machine_only_screen"
SCREEN_LATE = "screen_late"
ON_TIME = "on_time"
NO_VISIBLE_TIME = "no_visible_time"


@dataclass(frozen=True)
class DeliveryMetric:
    """单条播出凭证（first/replay 各自独立）的观众体验度量。"""

    delivery_id: str
    segment_id: str
    language: str
    channel: str
    viewer_count: int
    publisher: str
    translator: str
    audio_ms: int
    has_speech: bool
    glossary: str
    deadline_ms: int
    machine_ms: int | None
    edit_ms: int | None
    screen_ms: int | None
    screen_end_ms: int | None
    lead_delay_ms: int | None
    trailing_gap_ms: int | None
    information_gap_ms: int | None
    late_bad_window_ms: int | None
    phases: frozenset[str]
    supersedes: str | None
    received: str

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items()}
        data["phases"] = sorted(self.phases)
        return data


@dataclass(frozen=True)
class MetricBundle:
    metrics: list[DeliveryMetric]
    by_id: dict[str, DeliveryMetric] = field(default_factory=dict)

    def get(self, delivery_id: str) -> DeliveryMetric:
        return self.by_id[delivery_id]


def compute_metrics(project: ProjectRecords) -> MetricBundle:
    """为全部 289 条交付凭证计算体验度量，first 与 replay 各算各的。"""
    metrics: list[DeliveryMetric] = []
    for delivery in project.table("deliveries"):
        segment = project.segment(delivery["segment"])
        contract = project.contract_for(delivery["language"])
        metrics.append(_one(delivery, segment, contract))
    return MetricBundle(metrics=metrics, by_id={m.delivery_id: m for m in metrics})


def _one(delivery: dict[str, Any], segment: dict[str, Any],
         contract: dict[str, Any]) -> DeliveryMetric:
    deadline = contract["deadline_ms"]
    phases: set[str] = set()
    screen = delivery["screen_ms"]
    edit = delivery["edit_ms"]
    machine = delivery["machine_ms"]
    end = delivery["screen_end_ms"]

    lead = trailing = gap = bad_window = None

    if not segment["speech"]:
        # 原音本身停顿：不存在字幕义务，任何“延迟”都不能计入供应商。
        phases.add(NATURAL_PAUSE)
    elif screen is None:
        # 回执未确定观众可见时间：不得按完全合格处理，也无法测量延迟。
        phases.add(NO_VISIBLE_TIME)
    else:
        lead = max(0, screen)
        visible_span = end - screen
        trailing = max(0, segment["audio_ms"] - visible_span)
        gap = lead + trailing

        if edit is not None and edit > deadline:
            phases.add(UPSTREAM_LATE)
        # 同一句的下游重放：机器字幕先到屏幕，人工修订当时还没落地。
        if screen < edit:
            phases.add(MACHINE_ONLY_SCREEN)
        if screen > deadline:
            phases.add(SCREEN_LATE)
            bad_window = screen - deadline
        if not phases:
            phases.add(ON_TIME)

    return DeliveryMetric(
        delivery_id=delivery["id"],
        segment_id=delivery["segment"],
        language=delivery["language"],
        channel=delivery["channel"],
        viewer_count=delivery["viewer_count"],
        publisher=delivery["publisher"],
        translator=delivery["translator"],
        audio_ms=segment["audio_ms"],
        has_speech=segment["speech"],
        glossary=segment["glossary"],
        deadline_ms=deadline,
        machine_ms=machine,
        edit_ms=edit,
        screen_ms=screen,
        screen_end_ms=end,
        lead_delay_ms=lead,
        trailing_gap_ms=trailing,
        information_gap_ms=gap,
        late_bad_window_ms=bad_window,
        phases=frozenset(phases),
        supersedes=delivery.get("supersedes"),
        received=delivery["received"],
    )


def version_chain(project: ProjectRecords, delivery_id: str) -> list[DeliveryMetric]:
    """沿 supersedes 返回同一播出凭证的原始版本与全部后续回执（含自身）。"""
    bundle = compute_metrics(project)
    chain_ids = [delivery_id]
    chain_ids.extend(d["id"] for d in project.replacements_for(delivery_id))
    return [bundle.get(i) for i in chain_ids]
