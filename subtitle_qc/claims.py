"""扣减单复核：反证、互斥责任主张、关账期与跨渠道共同上限。

关键规则
--------
1. 关账期（shared_limits.closed=true，期间 1）冻结：不修改已关期单据、
   不新签扣款；新回执只能在开放账期（期间 2）形成更正。
2. 同一播出凭证的同一坏时段不得重复扣款：同一 delivery 的多张扣减单共享
   一个可确认罚额，后到的重复主张只能留在争议款。
3. counterclaims.possible_world 相同的主张互斥（同一责任世界只能成立一个），
   不同 possible_world 的独立主张可同时成立。
4. 跨渠道共同上限 shared_limits：保留争议占用后，只能对其余可独立确认的
   金额签发，额度不允许两个渠道分别用掉。
5. 不允许因为临时测不出来就把整笔划给另一方；screen_ms=null 的项保持不可核实。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .coverage import verification_index
from .defects import (
    CRITICAL_TERM,
    JOINT,
    confirmed_amount_for_delivery,
    detect_defects,
)
from .metrics import NO_VISIBLE_TIME, compute_metrics
from .records import ProjectRecords

# 复核结论
SETTLE = "settle"                 # 可在本开放账期签发
HOLD_DISPUTE = "hold_dispute"     # 留在争议款
FROZEN_CLOSED = "frozen_closed"   # 关账期冻结，只读
REJECT = "reject"                 # 口径不成立（如 critical 证据不足却按 critical 主张）

# 资金动作（下一开放账期如何结清，详见 ledger.py）
PAY_NOW = "issue_payment"
KEEP_HOLD = "keep_hold"
NO_ACTION = "no_action"


@dataclass
class ClaimDecision:
    claim_id: str
    delivery_id: str
    period: int
    party: str
    status: str
    claimed_cents: int
    paid_cents: int
    allowed_cents: int
    issuable_cents: int
    action: str
    matched_defect_kinds: list[str]
    exclusive_world: str | None
    counterclaim_ids: list[str]
    trace: list[str]
    reasons: list[str]
    prior_period_overpayment_cents: int = 0  # 新证据证明前期多付、需在下期冲减的金额


@dataclass
class PeriodSettlement:
    period: int
    closed: bool
    decisions: list[ClaimDecision]
    group_ledgers: dict[str, dict[str, Any]] = field(default_factory=dict)
    period_issuable_cents: int = 0
    period_held_cents: int = 0


@dataclass
class Adjudication:
    settlements: dict[int, PeriodSettlement]
    trace_index: dict[str, list[str]]
    unclaimed_findings: list[dict[str, Any]]
    counterclaim_worlds: dict[str, list[dict[str, Any]]]


def adjudicate(project: ProjectRecords) -> Adjudication:
    """按账期复核全部扣减单，并给出可回查的判责链路。"""
    bundle = compute_metrics(project)
    defects = detect_defects(project, bundle)
    verify = verification_index(project, bundle)
    defects_by_delivery: dict[str, list[Any]] = {}
    for defect in defects:
        defects_by_delivery.setdefault(defect.delivery_id, []).append(defect)

    worlds = _counterclaim_worlds(project)
    closed_periods = {lim["period"] for lim in project.table("shared_limits") if lim["closed"]}

    decisions_by_period: dict[int, list[ClaimDecision]] = {}
    trace_index: dict[str, list[str]] = {}
    for claim in project.table("claims"):
        period = claim["period"]
        decision = _adjudicate_claim(
            project, claim, defects_by_delivery, worlds, period in closed_periods, verify
        )
        decisions_by_period.setdefault(period, []).append(decision)
        trace_index[claim["id"]] = decision.trace

    settlements = {
        period: _settle_period(project, period, period in closed_periods, decisions)
        for period, decisions in sorted(decisions_by_period.items())
    }

    unclaimed = _unclaimed_findings(project, bundle, defects)
    return Adjudication(
        settlements=settlements,
        trace_index=trace_index,
        unclaimed_findings=unclaimed,
        counterclaim_worlds=worlds,
    )


def _counterclaim_worlds(project: ProjectRecords) -> dict[str, list[dict[str, Any]]]:
    worlds: dict[str, list[dict[str, Any]]] = {}
    for cc in project.table("counterclaims"):
        worlds.setdefault(cc["possible_world"], []).append(cc)
    return worlds


def _adjudicate_claim(project, claim, defects_by_delivery, worlds,
                      is_closed, verify) -> ClaimDecision:
    delivery_id = claim["delivery"]
    trace = [
        f"claim {claim['id']} -> delivery {delivery_id} (period {claim['period']}, party {claim['party']})",
    ]
    for ev in claim["evidence"]:
        trace.append(f"  evidence: {ev}")
    reasons: list[str] = []
    all_delivery_defects = defects_by_delivery.get(delivery_id, [])
    # 一张扣减单只能覆盖其证据能回查的缺陷：证据可以是 check id，
    # 也可以是被 supersedes 链引用的补播 delivery id（其证据沿链继承）。
    claim_evidence = set(claim["evidence"])
    chain_ids = {delivery_id}
    chain_ids.update(d["id"] for d in project.replacements_for(delivery_id))
    matched = [
        d for d in all_delivery_defects
        if claim_evidence.intersection(d.evidence_ids)
        or (claim_evidence & chain_ids and d.delivery_id in chain_ids)
    ]
    counter_ids = [cc["id"] for cc in project.table("counterclaims")
                   if claim["id"] in cc["claims"]]
    exclusive_world = next(
        (cc["possible_world"] for cc in project.table("counterclaims")
         if claim["id"] in cc["claims"]), None
    )

    if is_closed:
        # 关账期：单据只读，已付金额保留，未决金额随争议款带到开放期处理。
        reasons.append("期间已关账：单据冻结，不按新证据改写已关期金额")
        trace.append("  period closed -> frozen_closed; paid 金额保留，余额转开放期争议处理")
        return ClaimDecision(
            claim_id=claim["id"], delivery_id=delivery_id, period=claim["period"],
            party=claim["party"], status=FROZEN_CLOSED,
            claimed_cents=claim["claimed_cents"], paid_cents=claim["paid_cents"],
            allowed_cents=claim["paid_cents"], issuable_cents=0,
            action=NO_ACTION, matched_defect_kinds=[d.kind for d in matched],
            exclusive_world=exclusive_world, counterclaim_ids=counter_ids,
            trace=trace, reasons=reasons,
        )

    if not matched:
        metric = compute_metrics(project).get(delivery_id)
        if NO_VISIBLE_TIME in metric.phases:
            reasons.append("回执未确定观众可见时间，保持不可核实，不得整笔划给任何一方")
            trace.append("  screen_ms=null -> hold_dispute（不可核实）")
        else:
            reasons.append("没有可回查的人工复核缺陷支撑该扣减单")
            trace.append("  no matching defect -> reject")
        return ClaimDecision(
            claim_id=claim["id"], delivery_id=delivery_id, period=claim["period"],
            party=claim["party"], status=REJECT,
            claimed_cents=claim["claimed_cents"], paid_cents=0,
            allowed_cents=0, issuable_cents=0, action=NO_ACTION,
            matched_defect_kinds=[], exclusive_world=exclusive_world,
            counterclaim_ids=counter_ids, trace=trace, reasons=reasons,
        )

    # 同一坏时段不重复扣款：合并该交付的坏窗口，重叠窗口只计一次，
    # 不同缺陷（迟到/错译）窗口不重叠时分别计罚后求和。
    # 扣减单只覆盖其实际主张的缺陷；同交付上未被任何单据主张的缺陷进
    # unclaimed_findings，不自动并入本单金额。
    confirmation = confirmed_amount_for_delivery(matched, delivery_id)
    allowed = confirmation["total_cents"]
    kinds = sorted(confirmation["windows"])
    for kind, window in confirmation["windows"].items():
        trace.append(
            f"  {kind}: 坏窗 {window['bad_window_ms']}ms -> "
            f"{window['capped_penalty_cents']} 分（critical={window['critical']}，"
            f"证据 {','.join(window['evidence_ids'])}）"
        )
    trace.append(f"  合并后本扣减单可确认池={allowed} 分（坏时段不重复扣款）")
    prior_paid = _prior_paid_on_delivery(project, delivery_id, claim["period"])
    overpayment = max(0, prior_paid - allowed)
    if prior_paid:
        trace.append(f"  同一播出凭证前期已付 {prior_paid} 分，同一坏时段不重复扣款")
        if overpayment:
            reasons.append(
                f"新证据把可确认池核为 {allowed} 分，前期已付 {prior_paid} 分，"
                f"多付 {overpayment} 分只能在下个开放账期冲减，不回改已关期单据"
            )
            trace.append(f"  overpayment={overpayment} -> reduce_prior_payment（开放期冲减）")

    # critical 口径检查：若供应商按 critical 主张但证据只支持普通口径，必须降级。
    critical_supported = any(d.kind == CRITICAL_TERM for d in matched)
    if claim["claimed_cents"] > allowed and not critical_supported:
        reasons.append(
            f"主张 {claim['claimed_cents']} 分（critical 口径）证据不足，"
            f"术语表纠正回执缺失，按普通口径 {allowed} 分核定"
        )
        trace.append("  critical 口径不成立 -> 降级为 regular")

    # 互斥责任世界：同一 possible_world 内存在多个 pending 主张时全部留争议。
    if exclusive_world is not None:
        world_members = worlds[exclusive_world]
        pending = [cc for cc in world_members if cc["status"] == "pending"]
        if len(pending) > 1:
            reasons.append(
                f"互斥责任世界 {exclusive_world} 内有 {len(pending)} 个待决反证"
                f"（{', '.join(cc['id'] for cc in pending)}），责任边界未拆开前不得结算"
            )
            trace.append("  exclusive possible_world 未决 -> hold_dispute")
            for cc in world_members:
                trace.append(
                    f"  counterclaim {cc['id']}: party={cc['party']} "
                    f"viewers={cc['viewers']} status={cc['status']}"
                )
            return ClaimDecision(
                claim_id=claim["id"], delivery_id=delivery_id, period=claim["period"],
                party=claim["party"], status=HOLD_DISPUTE,
                claimed_cents=claim["claimed_cents"], paid_cents=0,
                allowed_cents=allowed, issuable_cents=0, action=KEEP_HOLD,
                matched_defect_kinds=kinds, exclusive_world=exclusive_world,
                counterclaim_ids=counter_ids, trace=trace, reasons=reasons,
                prior_period_overpayment_cents=overpayment,
            )

    # 同交付重复扣款检查：若该交付在更早账期已有支付，开放期只允许补差额。
    remaining = max(0, allowed - prior_paid)
    if prior_paid:
        reasons.append(f"同一交付已在前期支付 {prior_paid} 分，坏时段不重复扣款，剩余可确认 {remaining} 分")
        trace.append(f"  prior paid {prior_paid} -> remaining {remaining}")

    issuable = min(claim["claimed_cents"], remaining)
    if issuable <= 0:
        trace.append("  可确认池已被前期凭证占用 -> hold_dispute")
        return ClaimDecision(
            claim_id=claim["id"], delivery_id=delivery_id, period=claim["period"],
            party=claim["party"], status=HOLD_DISPUTE,
            claimed_cents=claim["claimed_cents"], paid_cents=0,
            allowed_cents=allowed, issuable_cents=0, action=KEEP_HOLD,
            matched_defect_kinds=kinds, exclusive_world=exclusive_world,
            counterclaim_ids=counter_ids, trace=trace, reasons=reasons,
            prior_period_overpayment_cents=overpayment,
        )

    status = SETTLE
    if any(d.responsible_stage == JOINT for d in matched) and not exclusive_world:
        # 上下游都能独立造成、又没有明确分工反证：先可确认的部分仍需看上限，
        # 但责任未拆开的金额留争议。
        status = HOLD_DISPUTE
        reasons.append("上下游均可独立造成该坏时段且无分工反证，金额留争议款")
        trace.append("  joint_unresolved -> hold_dispute")
        return ClaimDecision(
            claim_id=claim["id"], delivery_id=delivery_id, period=claim["period"],
            party=claim["party"], status=status,
            claimed_cents=claim["claimed_cents"], paid_cents=0,
            allowed_cents=allowed, issuable_cents=0, action=KEEP_HOLD,
            matched_defect_kinds=kinds, exclusive_world=exclusive_world,
            counterclaim_ids=counter_ids, trace=trace, reasons=reasons,
            prior_period_overpayment_cents=overpayment,
        )

    trace.append(f"  可独立确认 -> settle，本期待签发 {issuable} 分")
    return ClaimDecision(
        claim_id=claim["id"], delivery_id=delivery_id, period=claim["period"],
        party=claim["party"], status=status,
        claimed_cents=claim["claimed_cents"], paid_cents=0,
        allowed_cents=allowed, issuable_cents=issuable, action=PAY_NOW,
        matched_defect_kinds=kinds, exclusive_world=exclusive_world,
        counterclaim_ids=counter_ids, trace=trace, reasons=reasons,
        prior_period_overpayment_cents=overpayment,
    )


def _prior_paid_on_delivery(project: ProjectRecords, delivery_id: str,
                            current_period: int) -> int:
    return sum(
        c["paid_cents"] for c in project.table("claims")
        if c["delivery"] == delivery_id and c["period"] < current_period
    )


def _settle_period(project: ProjectRecords, period: int, is_closed: bool,
                   decisions: list[ClaimDecision]) -> PeriodSettlement:
    group_ledgers: dict[str, dict[str, Any]] = {}
    for limit in project.table("shared_limits"):
        if limit["period"] != period:
            continue
        headroom = (limit["ceiling_cents"]
                    - limit["claim_holds_cents"]
                    - limit["already_paid_cents"])
        group_ledgers[limit["group"]] = {
            "limit_id": limit["id"],
            "ceiling_cents": limit["ceiling_cents"],
            "claim_holds_cents": limit["claim_holds_cents"],
            "already_paid_cents": limit["already_paid_cents"],
            "headroom_cents": headroom,
            "channels": list(limit["channels"]),
            "closed": limit["closed"],
            "issued_this_period_cents": 0,
            "blocked_by_ceiling": [],
        }

    issuable_total = 0
    held_total = 0
    # 先处理可独立确认的结算，再在共同上限内排队，额度不允许渠道分别用掉。
    for decision in decisions:
        if decision.status != SETTLE or decision.issuable_cents <= 0:
            held_total += _held_amount(decision)
            continue
        group = _group_for_delivery(project, decision.delivery_id)
        if group is None or group not in group_ledgers:
            # 不在任何共同上限组（如 T07-pt-first）：按合同独立签发。
            decision.issuable_cents = decision.issuable_cents
            issuable_total += decision.issuable_cents
            decision.trace.append(
                f"  不属于任何共同扣减组，独立签发 {decision.issuable_cents} 分"
            )
            continue
        ledger = group_ledgers[group]
        if ledger["headroom_cents"] >= decision.issuable_cents:
            ledger["headroom_cents"] -= decision.issuable_cents
            ledger["issued_this_period_cents"] += decision.issuable_cents
            issuable_total += decision.issuable_cents
            decision.trace.append(
                f"  共同上限组 {group} 剩余额度充足，签发 {decision.issuable_cents} 分，"
                f"组内剩余 {ledger['headroom_cents']} 分"
            )
        else:
            available = max(0, ledger["headroom_cents"])
            blocked = decision.issuable_cents - available
            if available:
                ledger["headroom_cents"] = 0
                ledger["issued_this_period_cents"] += available
                issuable_total += available
                decision.issuable_cents = available
            else:
                decision.status = HOLD_DISPUTE
                decision.action = KEEP_HOLD
                decision.issuable_cents = 0
            ledger["blocked_by_ceiling"].append(decision.claim_id)
            held_total += blocked
            decision.trace.append(
                f"  共同上限组 {group} 额度不足，{blocked} 分留争议款，不得两家分别占用"
            )

    return PeriodSettlement(
        period=period, closed=is_closed, decisions=decisions,
        group_ledgers=group_ledgers,
        period_issuable_cents=issuable_total, period_held_cents=held_total,
    )


def _held_amount(decision: ClaimDecision) -> int:
    if decision.status in (HOLD_DISPUTE, FROZEN_CLOSED):
        return max(0, decision.claimed_cents - decision.paid_cents)
    return 0


def _group_for_delivery(project: ProjectRecords, delivery_id: str) -> str | None:
    for limit in project.table("shared_limits"):
        if delivery_id in limit["channels"]:
            return limit["group"]
    return None


def _unclaimed_findings(project: ProjectRecords, bundle, defects) -> list[dict[str, Any]]:
    """已被证据确认但没有任何扣减单主张的缺陷：只报告，不自动扣款。"""
    claimed = {c["delivery"] for c in project.table("claims")}
    out = []
    for defect in defects:
        if defect.delivery_id in claimed or defect.capped_penalty_cents <= 0:
            continue
        metric = bundle.get(defect.delivery_id)
        out.append({
            "delivery_id": defect.delivery_id,
            "kind": defect.kind,
            "critical": defect.critical,
            "responsible_stage": defect.responsible_stage,
            "bad_window_ms": defect.bad_window_ms,
            "potential_cents": defect.capped_penalty_cents,
            "viewer_count": metric.viewer_count,
            "evidence_ids": list(defect.evidence_ids),
            "reason": defect.reason,
        })
    return sorted(out, key=lambda x: (x["delivery_id"], x["kind"]))
