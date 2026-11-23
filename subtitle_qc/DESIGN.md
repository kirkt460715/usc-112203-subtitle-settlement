# 字幕质检与交付争议复核引擎

面向海外多语赛事转播：按语种支付字幕制作费，用持续到达的回执在**同一个项目入口**
复核交付，计算观众实际经历的字幕延迟与信息缺口，并把质检结论接到采购扣减、
争议款与跨渠道共同上限上。只依赖 Python 3.11 标准库。

## 1. 数据与入口

```
records/                 离线合成业务记录（catalog.json 含行数与 sha256）
subtitle_qc/             复核引擎（只读 records，不回写业务数据）
tests/                   unittest，含发行资料完整性与全部合同规则
reports/                 运行后生成的 markdown / JSON 报告
```

| 命令 | 用途 |
| --- | --- |
| `python3 -m unittest discover -s tests -v` | 全量核对（36 个用例） |
| `python3 -m subtitle_qc.reporting --records records --out reports --json` | 全项目复核，下月用同一命令处理新回执 |
| `python3 -m subtitle_qc.reporting --records records --delivery T14-es-first` | 沿一条播出凭证回看版本链、证据、判责、资金 |
| `python3 -m subtitle_qc.reporting --records records --language es --period 2` | 按语种/账期过滤 |

加载时 `records.py` 校验每个 jsonl 的 sha256、行数、表内 id 唯一及跨表引用，
资料被替换会直接抛 `RecordError`，引擎不会在脏数据上出结论。

## 2. 观众实际经历：四种相位严格分开（metrics.py）

所有时间以原音出现为零点、单位毫秒。`machine_ms / edit_ms / screen_ms`
**互不保证先后**——本地信号重放会让机器字幕先上屏、人工修订后落地，
因此引擎按时刻独立打相位标签，而不是假设流水线顺序：

| 相位 | 判定 | 业务含义 |
| --- | --- | --- |
| `natural_pause` | `segments.speech=false` | 原音本就停顿，无字幕义务，任何延迟不计供应商 |
| `upstream_late` | `edit_ms > deadline_ms` | 上游转写/人工修订迟到 |
| `machine_only_screen` | `screen_ms < edit_ms` | 同一句下游重放：观众先看到机器字幕 |
| `screen_late` | `screen_ms > deadline_ms` | 纠错真正出现在观众屏幕上的时间迟到 |
| `no_visible_time` | `screen_ms=null` | 回执未确定可见时间，保持不可核实，不算合格 |

信息缺口 = 前置延迟 `max(0, screen_ms)` + 尾部缺口
`max(0, audio_ms − (screen_end_ms − screen_ms))`。
first 与 replay 是各自独立的播出记录；`supersedes` 链（如 `T14-es-first` →
`T14-es-first-R2`）的每个版本都保留自己的观众数与度量，原始指标和补播后指标
都能回查。

## 3. 可核实范围：客诉不是概率样本（coverage.py）

- `scheduled` 检查带 `inclusion`（es=0.5，pt/ja=1/3），是唯一可外推的概率样本，
  报告按 1/inclusion 给加权观众量；
- `complaint` 检查 `inclusion=null`，只作证据线索，**不进入总体推断**；
- 173/289 条交付没有任何抽查，标 `unverified`，绝不按完全合格计入；
- 覆盖强度按 语种 × 渠道 × 赛段（opening/match/decider）给单元格，并单列
  “缺概率样本的赛段”（如 ja/pt 的 match 赛段），跨语种比较必须连同强度读。

## 4. 合同口径：关键术语错误与普通迟到不混用（defects.py）

`critical_term_error`（300 分）必须**同时**满足：人工复核 `wrong=true`、
片段术语级别 `critical`、且 `terminology.jsonl` 存在覆盖该语种/渠道的纠正版
术语表而交付仍旧。缺术语表回执的 critical 片段（T07/T21/T28/T35/T42）
只能按 `content_wrong` 普通罚则（100 分）处理，禁止按全语种错误率套 critical。

- 同一工作室给各渠道供同一版术语表：terminology 只登记 first 渠道时，
  replay 按 语种+片段 回退匹配；但责任期限按**各渠道实际收到纠正版的时间**
  分开（es 17:11、pt 17:15），不做全语种一刀切；
- 罚额按 `ceil(坏毫秒/1000) × max_per_bad_second` 做坏时长封顶；
- 漏译（`screen_ms=null` 且 `complete=false`）按整段发言时长计信息缺口；
  无任何抽查证据的漏译不出罚单，只保留不可核实标记；
- 补播回执（R2）自身没有抽查行时，沿 supersedes 链继承原始凭证的证据。

## 5. 判责、反证与关账期（claims.py）

1. **关账期冻结**：期间 1（`shared_limits.closed=true`）单据只读，已付金额
   保留，不按新证据回改旧单据；未决余额随争议款带入开放期。
2. **同一坏时段不重复扣款**：一张扣减单只覆盖其证据能回查的缺陷；同交付
   重叠坏窗口合并计一次，不同缺陷窗口不重叠时分别计罚后求和，签发额不超过
   主张额。
3. **互斥/并存主张**：`counterclaims.possible_world` 相同的主张互斥
   （origin-es 下 A1 terms-A 与 A2 es-render 均 pending → 全部留争议）；
   不同 world（A3 origin-pt，confirmed）可独立成立。
4. **测不出来不划账**：`screen_ms=null` 且无证据的项保持不可核实，不整笔划给
   任何一方。
5. **跨渠道共同上限**：每个 group 按期有 ceiling，先扣争议预留与已清算，
   剩余额度对可独立确认项排队签发；额度不足时未签部分留争议，不允许两家
   渠道分别用掉同一额度。

本数据上的结论：X1 关账冻结（已付 200、余额 100 入争议）；X2 因互斥反证
留争议 300；X3（T07-pt-first，无术语表回执）按普通口径可结算 100，
且不属于 G-EU 组、不占用共同上限。

## 6. 下个开放账期的三类资金动作（ledger.py）

| 动作 | 含义 |
| --- | --- |
| `new_issue` | 开放期可独立确认扣减的签发（受共同上限剩余额度约束） |
| `release_hold` | 争议预留保留/释放，金额不超过本单主张且未付部分 |
| `reduce_prior_payment` | 新证据证明前期多付时，仅在下个开放账期冲减；已关期原单据不动 |
| `already_occupied` | 已被争议预留或其它凭证占用的额度，只留痕，不能再签发 |

## 7. 可回查链路

- 沿质检样本：`--delivery <id>` 给出 supersedes 版本链、checks 证据、
  缺陷口径与责任相位、每张扣减单的判责理由（trace 逐行可读）；
- 沿扣减单：decision 的 trace 引用 check id、术语表 id、补播 delivery id 与
  共同上限组，能追到“是谁在哪次播送纠正了观众真正看到的内容”；
- 已证实但无扣减单的缺陷只进 `unclaimed_findings`，报告披露但不自动扣款。
