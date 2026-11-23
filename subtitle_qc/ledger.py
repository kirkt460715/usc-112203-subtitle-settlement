"""下一开放账期的资金更正计划。

三类更正动作严格分开，供应商可沿自己的回执与所用合同核对
--------------------------------------------------------
reduce_prior_payment   减少上期支付：仅当新证据把已确认的责任边界/人群范围
                       改向，且对应账期仍开放时才允许；已关账期只能登记、不动钱。
release_hold           释放争议预留：互斥责任世界出现 confirmed 反证后，
                       把争议款转为可签发或退回。
already_occupied       已被其它凭证占用的部分：共同上限/已支付池子里没有
                       剩余额度，只能留痕，不能再签发或重复扣款。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .claims import (
    Adjudication,
    FROZEN_CLOSED,
    HOLD_DISPUTE,
    SETTLE,
)
from .records import ProjectRecords

REDUCE_PRIOR = "reduce_prior_payment"
RELEASE_HOLD = "release_hold"
ALREADY_OCCUPIED = "already_occupied"
NEW_ISSUE = "new_issue"


@dataclass(frozen=True)
class LedgerEntry:
    claim_id: str
    delivery_id: str
    group: str | None
    action: str
    amount_cents: int
    period: int
    applies_to_period: int
    frozen: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def build_next_period_plan(project: ProjectRecords,
                           adjudication: Adjudication) -> list[LedgerEntry]:
    """基于本期复核结果，生成下一开放账期可执行/留痕的更正条目。"""
    entries: list[LedgerEntry] = []
    open_periods = sorted(
        lim["period"] for lim in project.table("shared_limits") if not lim["closed"]
    )
    next_period = (max(open_periods) if open_periods
                   else max(s["period"] for s in project.table("shared_limits")) + 1)
    group_of_channel = {
        ch: lim["group"]
        for lim in project.table("shared_limits")
        for ch in lim["channels"]
    }

    for settlement in adjudication.settlements.values():
        for decision in settlement.decisions:
            group = group_of_channel.get(decision.delivery_id)
            if decision.status == FROZEN_CLOSED:
                # 已关账期：新证据只能在开放期登记，绝不改旧单据。
                entries.append(LedgerEntry(
                    claim_id=decision.claim_id,
                    delivery_id=decision.delivery_id,
                    group=group,
                    action=ALREADY_OCCUPIED,
                    amount_cents=decision.paid_cents,
                    period=next_period,
                    applies_to_period=decision.period,
                    frozen=True,
                    note=("关账期已支付部分保持不动；若新证据改变责任边界，"
                          "只能在开放期以争议更正处理，不回改旧单据"),
                ))
                unpaid = decision.claimed_cents - decision.paid_cents
                if unpaid > 0:
                    entries.append(LedgerEntry(
                        claim_id=decision.claim_id,
                        delivery_id=decision.delivery_id,
                        group=group,
                        action=RELEASE_HOLD,
                        amount_cents=unpaid,
                        period=next_period,
                        applies_to_period=decision.period,
                        frozen=False,
                        note="关账期未付余额随争议款带入开放期，待互斥反证拆开后释放",
                    ))
            elif decision.status == HOLD_DISPUTE:
                # 争议预留只覆盖本单实际主张且未付的部分；同交付上未被本单
                # 主张的缺陷在 unclaimed_findings 里，不占用争议额度。
                held = min(decision.allowed_cents,
                           max(0, decision.claimed_cents - decision.paid_cents))
                entries.append(LedgerEntry(
                    claim_id=decision.claim_id,
                    delivery_id=decision.delivery_id,
                    group=group,
                    action=RELEASE_HOLD,
                    amount_cents=held,
                    period=next_period,
                    applies_to_period=decision.period,
                    frozen=False,
                    note="争议预留保留；收到明确分工反证后按确认结果释放或退回",
                ))
                if decision.prior_period_overpayment_cents:
                    entries.append(LedgerEntry(
                        claim_id=decision.claim_id,
                        delivery_id=decision.delivery_id,
                        group=group,
                        action=REDUCE_PRIOR,
                        amount_cents=decision.prior_period_overpayment_cents,
                        period=next_period,
                        applies_to_period=decision.period - 1,
                        frozen=False,
                        note="开放期新证据证明前期多付，在下个账期冲减；已关期原单据保持不变",
                    ))
            elif decision.status == SETTLE:
                entries.append(LedgerEntry(
                    claim_id=decision.claim_id,
                    delivery_id=decision.delivery_id,
                    group=group,
                    action=NEW_ISSUE,
                    amount_cents=decision.issuable_cents,
                    period=next_period,
                    applies_to_period=decision.period,
                    frozen=False,
                    note="开放期可独立确认的扣减，按共同上限剩余额度签发",
                ))
                if decision.prior_period_overpayment_cents:
                    entries.append(LedgerEntry(
                        claim_id=decision.claim_id,
                        delivery_id=decision.delivery_id,
                        group=group,
                        action=REDUCE_PRIOR,
                        amount_cents=decision.prior_period_overpayment_cents,
                        period=next_period,
                        applies_to_period=decision.period - 1,
                        frozen=False,
                        note="开放期新证据证明前期多付，在下个账期冲减；已关期原单据保持不变",
                    ))

    # 共同上限组：登记已被占用的额度，防止下期被两家分别用掉。
    for settlement in adjudication.settlements.values():
        for group, ledger in settlement.group_ledgers.items():
            occupied = ledger["ceiling_cents"] - ledger["headroom_cents"]
            entries.append(LedgerEntry(
                claim_id=f"@{ledger['limit_id']}",
                delivery_id=",".join(ledger["channels"]),
                group=group,
                action=ALREADY_OCCUPIED,
                amount_cents=occupied,
                period=next_period,
                applies_to_period=settlement.period,
                frozen=ledger["closed"],
                note=(f"组 {group} 共同上限 {ledger['ceiling_cents']} 分中已被"
                      f"争议预留 {ledger['claim_holds_cents']} 分与已清算 "
                      f"{ledger['already_paid_cents'] + ledger['issued_this_period_cents']} 分占用"),
            ))
    return entries


def correction_summary(entries: list[LedgerEntry]) -> dict[str, int]:
    """按动作汇总下期更正金额，供采购一页看清三类资金去向。"""
    summary = {REDUCE_PRIOR: 0, RELEASE_HOLD: 0, ALREADY_OCCUPIED: 0, NEW_ISSUE: 0}
    for entry in entries:
        summary[entry.action] += entry.amount_cents
    return summary
