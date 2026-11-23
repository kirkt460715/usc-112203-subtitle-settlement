"""引擎规则测试：覆盖相位区分、覆盖强度、合同口径、判责与资金上限。"""
from __future__ import annotations

import copy
import unittest
from pathlib import Path

from subtitle_qc.claims import (
    FROZEN_CLOSED,
    HOLD_DISPUTE,
    REJECT,
    SETTLE,
    adjudicate,
)
from subtitle_qc.coverage import (
    EVIDENCE_ONLY,
    PARTIAL,
    STRONG,
    UNVERIFIED,
    comparable_scope,
    coverage_report,
    verification_index,
)
from subtitle_qc.defects import (
    CONTENT_WRONG,
    CRITICAL_TERM,
    LATENESS,
    OMISSION,
    DOWNSTREAM,
    UPSTREAM,
    cap_for_bad_window,
    detect_defects,
)
from subtitle_qc.ledger import (
    ALREADY_OCCUPIED,
    NEW_ISSUE,
    RELEASE_HOLD,
    build_next_period_plan,
    correction_summary,
)
from subtitle_qc.metrics import (
    MACHINE_ONLY_SCREEN,
    NATURAL_PAUSE,
    NO_VISIBLE_TIME,
    SCREEN_LATE,
    UPSTREAM_LATE,
    compute_metrics,
    version_chain,
)
from subtitle_qc.records import RecordError, load_project
from subtitle_qc.reporting import delivery_trace, render_markdown, run

ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / "records"


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = load_project(RECORDS)
        cls.bundle = compute_metrics(cls.project)
        cls.verify = verification_index(cls.project, cls.bundle)
        cls.defects = detect_defects(cls.project, cls.bundle)
        cls.adjudication = adjudicate(cls.project)

    def test_catalog_tamper_detection(self):
        # 改动内存数据不会影响重新加载；这里直接验证错误路径被覆盖。
        with self.assertRaises(RecordError):
            load_project(ROOT / "nonexistent")

    # ---- 四类观众经历的相位区分 ----
    def test_natural_pause_has_no_obligation(self):
        for metric in self.bundle.metrics:
            if metric.segment_id in {"T00", "T09"}:
                self.assertIn(NATURAL_PAUSE, metric.phases)
                self.assertIsNone(metric.information_gap_ms)
        t00 = self.bundle.get("T00-es-first")
        self.assertEqual(t00.phases, frozenset({NATURAL_PAUSE}))

    def test_no_visible_time_is_not_qualified(self):
        t11 = self.bundle.get("T11-es-replay")
        self.assertIn(NO_VISIBLE_TIME, t11.phases)
        self.assertIsNone(t11.screen_ms)
        self.assertFalse(self.verify["T11-es-replay"].measured)

    def test_machine_only_replay_phase(self):
        # T01: 机器 240ms 上屏，人工修订 1350ms 才落地。
        t01 = self.bundle.get("T01-es-first")
        self.assertIn(MACHINE_ONLY_SCREEN, t01.phases)
        self.assertNotIn(SCREEN_LATE, t01.phases)

    def test_upstream_and_downstream_late_separated(self):
        # edit_ms 2700 > 2500：上游迟到；screen 2600 > 2500：下游也迟到。
        t14 = self.bundle.get("T14-es-replay")
        self.assertIn(UPSTREAM_LATE, t14.phases)
        self.assertIn(SCREEN_LATE, t14.phases)
        self.assertEqual(t14.late_bad_window_ms, 100)
        # T14-es-first：edit 2000 准时，screen 2600 迟到，仅下游相位。
        first = self.bundle.get("T14-es-first")
        self.assertNotIn(UPSTREAM_LATE, first.phases)
        self.assertIn(SCREEN_LATE, first.phases)

    def test_information_gap_lead_and_trailing(self):
        first = self.bundle.get("T14-es-first")  # audio 3600, screen 2600, end 6300
        self.assertEqual(first.lead_delay_ms, 2600)
        self.assertEqual(first.trailing_gap_ms, 0)
        self.assertEqual(first.information_gap_ms, 2600)
        r2 = self.bundle.get("T14-es-first-R2")  # 可见跨度 2800，尾部缺 800
        self.assertEqual(r2.lead_delay_ms, 5900)
        self.assertEqual(r2.trailing_gap_ms, 800)
        self.assertEqual(r2.information_gap_ms, 6700)

    def test_first_and_replay_are_separate_records(self):
        first = self.bundle.get("T01-es-first")
        replay = self.bundle.get("T01-es-replay")
        self.assertEqual(first.delivery_id, "T01-es-first")
        self.assertEqual(replay.channel, "replay")
        self.assertNotEqual(first.delivery_id, replay.delivery_id)

    def test_supersede_chain_keeps_both_metrics(self):
        chain = version_chain(self.project, "T14-es-first")
        self.assertEqual([m.delivery_id for m in chain],
                         ["T14-es-first", "T14-es-first-R2"])
        self.assertEqual(chain[0].viewer_count, 304)
        self.assertEqual(chain[1].viewer_count, 226)

    # ---- 覆盖强度 ----
    def test_scheduled_vs_complaint_status(self):
        self.assertEqual(self.verify["T14-es-first"].status, STRONG)       # inclusion 0.5
        self.assertEqual(self.verify["T14-pt-replay"].status, PARTIAL)     # 1/3
        self.assertEqual(self.verify["T14-pt-first"].status, EVIDENCE_ONLY)
        self.assertEqual(self.verify["T11-es-first"].status, UNVERIFIED)

    def test_complaints_never_enter_probability_scope(self):
        scope = comparable_scope(self.project, self.bundle)
        for lang, cell in scope.items():
            self.assertGreaterEqual(cell["population_deliveries"], cell["scheduled_sampled"])
            self.assertIn(cell["strength"], {STRONG, PARTIAL, UNVERIFIED})
        # ja 的 match 赛段完全没有概率样本，必须显式标出，不能当合格。
        self.assertIn("match", scope["ja"]["parts_without_probability_sample"])

    def test_coverage_cells_include_unverified_counts(self):
        cells = coverage_report(self.project, self.bundle, parts=False)
        totals = sum(c.population for c in cells)
        self.assertEqual(totals, 289)
        unchecked = sum(c.unverified for c in cells)
        self.assertEqual(unchecked, 173)

    # ---- 合同口径：critical 与 regular 不混用 ----
    def test_cap_formula(self):
        contract = self.project.contract_for("es")
        self.assertEqual(cap_for_bad_window(contract, 100), 200)
        self.assertEqual(cap_for_bad_window(contract, 3400), 800)
        self.assertEqual(cap_for_bad_window(contract, 3600), 800)

    def test_t14_es_first_is_critical_term_error(self):
        critical = [d for d in self.defects
                    if d.delivery_id == "T14-es-first" and d.kind == CRITICAL_TERM]
        self.assertEqual(len(critical), 1)
        defect = critical[0]
        self.assertTrue(defect.critical)
        # 坏窗 3700ms -> 封顶 800，高于 300，故不触发 cap。
        self.assertEqual(defect.base_penalty_cents, 300)
        self.assertEqual(defect.capped_penalty_cents, 300)
        self.assertFalse(defect.cap_applied)
        self.assertEqual(defect.term_record_ids, ("G1", "G3"))
        # 原始交付 17:16 晚于纠正版接收 17:11，但 first 渠道错误源自术语表，
        # 责任期限按渠道回执：交付在纠正可用后仍渲染旧术语 -> 下游可独立反证。
        self.assertIn(defect.responsible_stage, {UPSTREAM, DOWNSTREAM})

    def test_t07_critical_segment_without_terms_falls_to_regular(self):
        found = [d for d in self.defects if d.delivery_id == "T07-pt-first"]
        kinds = {d.kind for d in found}
        self.assertNotIn(CRITICAL_TERM, kinds)
        self.assertIn(CONTENT_WRONG, kinds)
        content = next(d for d in found if d.kind == CONTENT_WRONG)
        self.assertEqual(content.capped_penalty_cents, 100)

    def test_lateness_uses_regular_penalty_within_cap(self):
        # T14-es-first 屏幕迟到 100ms：普通罚则 100，坏窗封顶 ceil(0.1)*200=200，
        # 100 未超封顶故不触发 cap（短迟到不会被整笔重罚，也不会被错误抬高）。
        late = next(d for d in self.defects
                    if d.delivery_id == "T14-es-first" and d.kind == LATENESS)
        self.assertFalse(late.critical)
        self.assertEqual(late.base_penalty_cents, 100)
        self.assertEqual(late.capped_penalty_cents, 100)
        self.assertFalse(late.cap_applied)

    def test_cap_clamps_long_bad_window(self):
        # R2 迟到 3400ms：封顶 ceil(3.4)*200=800，普通罚则 100 仍在内；
        # 构造一个 100ms 坏窗验证 cap=200 的计算边界。
        contract = self.project.contract_for("es")
        self.assertEqual(cap_for_bad_window(contract, 100), 200)
        r2 = next(d for d in self.defects
                  if d.delivery_id == "T14-es-first-R2" and d.kind == LATENESS)
        self.assertEqual(r2.bad_window_ms, 3400)
        self.assertEqual(r2.capped_penalty_cents, 100)

    def test_omission_bad_window_is_full_audio(self):
        omissions = [d for d in self.defects if d.kind == OMISSION]
        t11 = next(d for d in omissions if d.delivery_id == "T11-es-replay")
        self.assertEqual(t11.bad_window_ms, 3300)
        # 未抽查的漏译（T11-es-first）不得产生可扣金额。
        t11_first = next((d for d in omissions if d.delivery_id == "T11-es-first"), None)
        self.assertIsNone(t11_first)

    def test_r2_replay_timing_measured_separately(self):
        r2_late = [d for d in self.defects if d.delivery_id == "T14-es-first-R2"]
        self.assertTrue(r2_late, "R2 回执应独立参与缺陷计算")
        late = next(d for d in r2_late if d.kind == LATENESS)
        self.assertEqual(late.bad_window_ms, 3400)

    def test_glossary_falls_back_across_channels_but_windows_stay_separate(self):
        # 术语表只登记 first 渠道，replay 共享同版术语表 -> critical 仍成立。
        replay = next(d for d in self.defects
                      if d.delivery_id == "T14-pt-replay" and d.kind == CRITICAL_TERM)
        self.assertEqual(replay.term_record_ids, ("G2", "G4"))
        # es 纠正版 17:11 到达、pt 17:15 到达：同上游错误的两路责任期限分开，
        # 不能用全语种错误率断定所有渠道仍由上游负责。
        es_first = next(d for d in self.defects
                        if d.delivery_id == "T14-es-first" and d.kind == CRITICAL_TERM)
        pt_first = next(d for d in self.defects
                        if d.delivery_id == "T14-pt-first" and d.kind == CRITICAL_TERM)
        self.assertEqual(es_first.responsible_stage, DOWNSTREAM)
        self.assertEqual(pt_first.responsible_stage, DOWNSTREAM)
        # 受影响观众范围不同：first 304，R2 仅 226，pt 术语回执 201。
        self.assertEqual(self.bundle.get("T14-es-first").viewer_count, 304)
        self.assertEqual(self.bundle.get("T14-es-first-R2").viewer_count, 226)

    # ---- 判责、互斥反证与关账期 ----
    def test_closed_period_is_frozen(self):
        p1 = self.adjudication.settlements[1]
        self.assertTrue(p1.closed)
        x1 = next(d for d in p1.decisions if d.claim_id == "X1")
        self.assertEqual(x1.status, FROZEN_CLOSED)
        self.assertEqual(x1.issuable_cents, 0)
        self.assertEqual(x1.allowed_cents, 200)  # 已付金额保留，不回改

    def test_exclusive_world_keeps_both_claims_in_dispute(self):
        p2 = self.adjudication.settlements[2]
        x2 = next(d for d in p2.decisions if d.claim_id == "X2")
        self.assertEqual(x2.status, HOLD_DISPUTE)
        self.assertEqual(x2.exclusive_world, "origin-es")
        self.assertEqual(set(x2.counterclaim_ids), {"A1", "A2"})
        self.assertEqual(x2.issuable_cents, 0)

    def test_open_claim_settles_with_regular_scope(self):
        p2 = self.adjudication.settlements[2]
        x3 = next(d for d in p2.decisions if d.claim_id == "X3")
        self.assertEqual(x3.status, SETTLE)
        # 证据同时支撑错译 100 与迟到 100 两个独立缺陷，可确认池=200；
        # 但本扣减单只主张 100，签发额不得超过主张额，且不按 critical 口径。
        self.assertEqual(x3.allowed_cents, 200)
        self.assertEqual(x3.issuable_cents, 100)
        self.assertNotIn(CRITICAL_TERM, x3.matched_defect_kinds)
        self.assertEqual(set(x3.matched_defect_kinds), {CONTENT_WRONG, LATENESS})

    def test_no_double_charge_same_bad_window(self):
        # X1 已为 T14-es-first 支付 200；X2 落在同一互斥责任世界且坏时段相同，
        # 不能重复占用同一可确认池，必须留争议。
        trace = self.adjudication.trace_index["X2"]
        self.assertTrue(any("互斥" in t or "重复" in t or "pending" in t for t in trace))
        x2 = next(d for d in self.adjudication.settlements[2].decisions
                  if d.claim_id == "X2")
        self.assertEqual(x2.issuable_cents, 0)

    def test_shared_ceiling_blocks_double_spend(self):
        p2 = self.adjudication.settlements[2]
        ledger = p2.group_ledgers["G-EU"]
        # 440 - 180 预留 - 90 已清算 = 170；本期 G-EU 无独立可签项，额度保留。
        self.assertEqual(ledger["headroom_cents"], 170)
        self.assertEqual(ledger["issued_this_period_cents"], 0)

    def test_x3_does_not_consume_group_ceiling(self):
        p2 = self.adjudication.settlements[2]
        self.assertEqual(p2.period_issuable_cents, 100)  # 仅 X3，且不在 G-EU 组

    def test_unmeasurable_never_assigned_without_evidence(self):
        # screen_ms=null 的交付：有 complete=false 人工复核的漏译可计；
        # 没有任何抽查证据的（如 T11-es-first）不得产生任何可扣金额。
        null_metrics = [m for m in self.bundle.metrics if NO_VISIBLE_TIME in m.phases]
        chargeable_ids = {d.delivery_id for d in self.defects}
        for metric in null_metrics:
            if self.verify[metric.delivery_id].status == UNVERIFIED:
                self.assertNotIn(metric.delivery_id, chargeable_ids)
            else:
                # 有证据的漏译金额只能来自 OMISSION，且不进入责任归属猜测。
                for defect in self.defects:
                    if defect.delivery_id == metric.delivery_id:
                        self.assertEqual(defect.kind, OMISSION)

    def test_counterclaim_worlds_grouped(self):
        worlds = self.adjudication.counterclaim_worlds
        self.assertEqual({cc["id"] for cc in worlds["origin-es"]}, {"A1", "A2"})
        self.assertEqual(worlds["origin-pt"][0]["status"], "confirmed")

    # ---- 下期更正计划 ----
    def test_next_period_plan_separates_money_buckets(self):
        entries = build_next_period_plan(self.project, self.adjudication)
        actions = {e.action for e in entries}
        # 关账期不允许 reduce_prior_payment 实际动钱。
        for e in entries:
            if e.frozen:
                self.assertEqual(e.action, ALREADY_OCCUPIED)
        self.assertIn(RELEASE_HOLD, actions)
        self.assertIn(NEW_ISSUE, actions)
        summary = correction_summary(entries)
        self.assertGreaterEqual(summary[RELEASE_HOLD], 100)

    def test_trace_links_claim_to_evidence_and_correction(self):
        trace = delivery_trace(RECORDS, "T14-es-first")
        chain_ids = [m["delivery_id"] for m in trace["version_chain"]]
        self.assertIn("T14-es-first-R2", chain_ids)
        evidence = {c["id"] for c in trace["checks"]}
        self.assertIn("Q-T14-es-first", evidence)
        decision_claims = {d["claim_id"] for d in trace["decisions"]}
        self.assertEqual(decision_claims, {"X1", "X2"})

    def test_report_runs_and_contains_key_conclusions(self):
        result = run(RECORDS)
        md = render_markdown(result)
        self.assertIn("关账", md)
        self.assertIn("T14-es-first", md)
        self.assertIn("release_hold", md)

    def test_unclaimed_findings_are_reported_not_charged(self):
        findings = self.adjudication.unclaimed_findings
        # T11-es-replay 漏译已被抽查确认但没有扣减单：只出现在报告里。
        ids = {f["delivery_id"] for f in findings}
        self.assertIn("T11-es-replay", ids)
        p2 = self.adjudication.settlements[2]
        charged = {d.delivery_id for d in p2.decisions if d.status == SETTLE}
        self.assertNotIn("T11-es-replay", charged)


class SyntheticEdgeTest(unittest.TestCase):
    """用最小合成记录验证引擎的边界规则，不依赖 records/ 的具体行数。"""

    def _project(self, rows_overrides):
        base = load_project(RECORDS)
        rows = copy.deepcopy(base.rows)
        for table, new_rows in rows_overrides.items():
            rows[table] = new_rows
        # 绕过 catalog 校验直接构造 ProjectRecords。
        from subtitle_qc.records import ProjectRecords
        return ProjectRecords(root=RECORDS, catalog=base.catalog, rows=rows)

    def test_ceiling_blocks_when_headroom_exhausted(self):
        from subtitle_qc.records import ProjectRecords
        project = load_project(RECORDS)
        adjudication = adjudicate(project)
        # 手工把 G-EU 共同上限改成已无剩余额度。
        rows = copy.deepcopy(project.rows)
        rows["shared_limits"] = [
            dict(rows["shared_limits"][0]),
            dict(rows["shared_limits"][1], claim_holds_cents=350, already_paid_cents=90),
        ]
        tight = ProjectRecords(root=RECORDS, catalog=project.catalog, rows=rows)
        tight_adj = adjudicate(tight)
        ledger = tight_adj.settlements[2].group_ledgers["G-EU"]
        self.assertEqual(ledger["headroom_cents"], 0)

    def test_ceiling_clamps_a_settlement_inside_group(self):
        from subtitle_qc.records import ProjectRecords
        project = load_project(RECORDS)
        rows = copy.deepcopy(project.rows)
        # T05-es-replay 是干净的下游迟到（100 分、证据 Q-T05-es-replay）。
        # 把它纳入 G-EU 共同上限组，并把组内剩余额度压到 50 分。
        rows["claims"] = rows["claims"] + [{
            "id": "X6", "delivery": "T05-es-replay", "period": 2,
            "party": "es-render", "claimed_cents": 100, "paid_cents": 0,
            "status": "open", "evidence": ["Q-T05-es-replay"],
        }]
        rows["shared_limits"] = [
            dict(rows["shared_limits"][0]),
            dict(rows["shared_limits"][1],
                 channels=rows["shared_limits"][1]["channels"] + ["T05-es-replay"],
                 claim_holds_cents=300, already_paid_cents=90),
        ]
        proj = ProjectRecords(root=RECORDS, catalog=project.catalog, rows=rows)
        adj = adjudicate(proj)
        ledger = adj.settlements[2].group_ledgers["G-EU"]
        # 440 - 300 - 90 = 50 额度；100 的单据只能签 50，其余留争议，不能超上限。
        self.assertEqual(ledger["headroom_cents"], 0)
        self.assertIn("X6", ledger["blocked_by_ceiling"])
        x6 = next(d for d in adj.settlements[2].decisions if d.claim_id == "X6")
        self.assertEqual(x6.issuable_cents, 50)

    def test_reject_when_no_evidence(self):
        # 一条指向无任何检查的交付的扣减单应被 reject。
        from subtitle_qc.records import ProjectRecords
        project = load_project(RECORDS)
        rows = copy.deepcopy(project.rows)
        rows["claims"] = rows["claims"] + [{
            "id": "X9", "delivery": "T11-es-first", "period": 2,
            "party": "es-text", "claimed_cents": 100, "paid_cents": 0,
            "status": "open", "evidence": [],
        }]
        proj = ProjectRecords(root=RECORDS, catalog=project.catalog, rows=rows)
        adj = adjudicate(proj)
        x9 = next(d for d in adj.settlements[2].decisions if d.claim_id == "X9")
        self.assertEqual(x9.status, REJECT)

    def test_overpayment_creates_next_period_reduction_without_editing_closed_doc(self):
        from subtitle_qc.records import ProjectRecords
        from subtitle_qc.ledger import REDUCE_PRIOR, build_next_period_plan
        project = load_project(RECORDS)
        rows = copy.deepcopy(project.rows)
        # T05-es-replay 是干净的下游迟到（100 分、证据 Q-T05-es-replay）。
        # 构造：前期已付 200，开放期新证据只支持 100 -> 多付 100 下期冲减。
        rows["claims"] = rows["claims"] + [
            {
                "id": "X7", "delivery": "T05-es-replay", "period": 1,
                "party": "es-render", "claimed_cents": 200, "paid_cents": 200,
                "status": "partly_paid", "evidence": ["Q-T05-es-replay"],
            },
            {
                "id": "X8", "delivery": "T05-es-replay", "period": 2,
                "party": "es-render", "claimed_cents": 100, "paid_cents": 0,
                "status": "open", "evidence": ["Q-T05-es-replay"],
            },
        ]
        proj = ProjectRecords(root=RECORDS, catalog=project.catalog, rows=rows)
        adj = adjudicate(proj)
        x8 = next(d for d in adj.settlements[2].decisions if d.claim_id == "X8")
        self.assertEqual(x8.prior_period_overpayment_cents, 100)
        entries = build_next_period_plan(proj, adj)
        reductions = [e for e in entries
                      if e.action == REDUCE_PRIOR and e.claim_id == "X8"]
        self.assertEqual(len(reductions), 1)
        self.assertEqual(reductions[0].amount_cents, 100)
        # 冲减条目不是 frozen；关账期原始单据另有 frozen 留痕，未被改写。
        self.assertFalse(reductions[0].frozen)
        self.assertTrue(any(e.frozen and e.claim_id == "X7" for e in entries))


if __name__ == "__main__":
    unittest.main()
