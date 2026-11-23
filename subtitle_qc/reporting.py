"""项目复核入口：同一命令在下个月用相同方式继续处理新到回执。

用法
----
python3 -m subtitle_qc.reporting --records records --out reports [--json]
python3 -m subtitle_qc.reporting --records records --delivery T14-es-first
python3 -m subtitle_qc.reporting --records records --language es --period 2
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .claims import FROZEN_CLOSED, HOLD_DISPUTE, REJECT, SETTLE, adjudicate
from .coverage import comparable_scope, coverage_report, verification_index
from .defects import detect_defects
from .ledger import build_next_period_plan, correction_summary
from .metrics import compute_metrics, version_chain
from .records import load_project

STATUS_CN = {
    SETTLE: "可结算",
    HOLD_DISPUTE: "留争议款",
    FROZEN_CLOSED: "关账冻结",
    REJECT: "口径不成立",
}


def run(records_dir: str | Path) -> dict[str, Any]:
    """运行完整复核，返回可序列化的结果字典（也供测试直接断言）。"""
    project = load_project(records_dir)
    bundle = compute_metrics(project)
    verify = verification_index(project, bundle)
    defects = detect_defects(project, bundle)
    adjudication = adjudicate(project)
    entries = build_next_period_plan(project, adjudication)

    return {
        "coverage_scope": comparable_scope(project, bundle),
        "coverage_cells": [c.__dict__ for c in coverage_report(project, bundle)],
        "metrics": [m.to_dict() for m in bundle.metrics],
        "verification": {k: v.__dict__ for k, v in verify.items()},
        "defects": [d.to_dict() for d in defects],
        "settlements": {
            str(period): {
                "period": s.period,
                "closed": s.closed,
                "issuable_cents": s.period_issuable_cents,
                "held_cents": s.period_held_cents,
                "group_ledgers": s.group_ledgers,
                "decisions": [d.__dict__ for d in s.decisions],
            }
            for period, s in adjudication.settlements.items()
        },
        "unclaimed_findings": adjudication.unclaimed_findings,
        "counterclaim_worlds": adjudication.counterclaim_worlds,
        "next_period_plan": [e.to_dict() for e in entries],
        "next_period_summary": correction_summary(entries),
    }


def delivery_trace(records_dir: str | Path, delivery_id: str) -> dict[str, Any]:
    """沿一条质检样本/扣减单回看：版本链、缺陷、判责理由、资金动作。"""
    project = load_project(records_dir)
    chain = version_chain(project, delivery_id)
    adjudication = adjudicate(project)
    defects = [d.to_dict() for d in detect_defects(project, compute_metrics(project))
               if d.delivery_id in {m.delivery_id for m in chain}]
    decisions = []
    for settlement in adjudication.settlements.values():
        for d in settlement.decisions:
            if d.delivery_id == delivery_id:
                decisions.append(d.__dict__)
    entries = [e.to_dict() for e in build_next_period_plan(project, adjudication)
               if e.delivery_id == delivery_id]
    return {
        "delivery_id": delivery_id,
        "version_chain": [m.to_dict() for m in chain],
        "checks": [c for c in project.table("checks") if c["delivery"] == delivery_id],
        "defects": defects,
        "decisions": decisions,
        "next_period_entries": entries,
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 字幕交付复核报告")
    lines.append("")
    lines.append("## 1. 可核实样本覆盖（客诉不外推）")
    for lang, scope in sorted(result["coverage_scope"].items()):
        lines.append(
            f"- {lang}: 总体 {scope['population_deliveries']} 条，计划抽样 "
            f"{scope['scheduled_sampled']} 条（inclusion={scope['inclusion']}，"
            f"强度 {scope['strength']}），仅客诉 {scope['complaint_only']} 条，"
            f"无任何抽查 {scope['unverified']} 条；有概率样本的赛段="
            f"{','.join(scope['parts_with_probability_sample']) or '无'}，"
            f"缺概率样本的赛段={','.join(scope['parts_without_probability_sample'])}"
        )
    lines.append("")
    lines.append("## 2. 账期复核结论")
    for period in sorted(result["settlements"], key=int):
        s = result["settlements"][period]
        flag = "已关账（只读）" if s["closed"] else "开放"
        lines.append(f"### 期间 {period}（{flag}）")
        lines.append(f"- 本期待签发 {s['issuable_cents']} 分；留争议 {s['held_cents']} 分")
        for group, ledger in s["group_ledgers"].items():
            lines.append(
                f"- 共同上限组 {group}: 上限 {ledger['ceiling_cents']}，争议预留 "
                f"{ledger['claim_holds_cents']}，已清算 {ledger['already_paid_cents']}，"
                f"本期签发 {ledger['issued_this_period_cents']}，剩余额度 "
                f"{ledger['headroom_cents']} 分"
            )
        for d in s["decisions"]:
            lines.append(
                f"- {d['claim_id']}（{d['delivery_id']} / {d['party']}）："
                f"{STATUS_CN[d['status']]}，主张 {d['claimed_cents']}，可确认 "
                f"{d['allowed_cents']}，本期签发 {d['issuable_cents']}；"
                f"缺陷口径={','.join(d['matched_defect_kinds']) or '无'}"
            )
            for reason in d["reasons"]:
                lines.append(f"    - {reason}")
    lines.append("")
    lines.append("## 3. 下一开放账期更正计划")
    summary = result["next_period_summary"]
    lines.append(
        f"- 新签发 {summary['new_issue']}；释放争议预留 {summary['release_hold']}；"
        f"减少上期支付 {summary['reduce_prior_payment']}；"
        f"已被其它凭证占用 {summary['already_occupied']}（单位：分）"
    )
    for e in result["next_period_plan"]:
        lines.append(
            f"- [{e['action']}] {e['claim_id']} / {e['delivery_id']} "
            f"{e['amount_cents']} 分（frozen={e['frozen']}）：{e['note']}"
        )
    lines.append("")
    lines.append("## 4. 已证实但暂无扣减单的缺陷（只报告，不自动扣款）")
    for finding in result["unclaimed_findings"]:
        lines.append(
            f"- {finding['delivery_id']} {finding['kind']} 潜在 "
            f"{finding['potential_cents']} 分，证据 {','.join(finding['evidence_ids'])}"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多语赛事字幕交付复核入口")
    parser.add_argument("--records", default="records", help="records 目录路径")
    parser.add_argument("--out", default="reports", help="报告输出目录")
    parser.add_argument("--json", action="store_true", help="同时输出 JSON 全量结果")
    parser.add_argument("--delivery", help="只回查某条播出凭证的判责链路")
    parser.add_argument("--language", help="按语种过滤")
    parser.add_argument("--period", type=int, help="按账期过滤")
    args = parser.parse_args(argv)

    if args.delivery:
        result = delivery_trace(args.records, args.delivery)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=list))
        return 0

    result = run(args.records)
    if args.language:
        result["metrics"] = [m for m in result["metrics"] if m["language"] == args.language]
        result["defects"] = [d for d in result["defects"]
                             if d["delivery_id"].split("-")[1] == args.language]
    if args.period is not None:
        result["settlements"] = {
            k: v for k, v in result["settlements"].items() if int(k) == args.period
        }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "reconciliation.md"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    if args.json:
        (out_dir / "reconciliation.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=list),
            encoding="utf-8",
        )
    print(render_markdown(result))
    print(f"报告已写入 {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
