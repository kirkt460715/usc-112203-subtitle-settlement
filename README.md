# 多语赛事转播的字幕体验指标与交付争议

同一片段的 first 和 replay 为各自播出记录。勘误的 supersedes 指向同一播出凭证。截止时间与金额按各合同明示单位。

records 保存可离线使用的合成业务记录。每行一个 UTF-8 JSON 对象，id 在各数据表内唯一，跨表键沿用业务记录中的标识。catalog.json 保存行数及原始文件校验值。

| 记录 | 业务含义 |
| --- | --- |
| segments.jsonl | 每句音频的真实长度及术语级别。speech=false 为无发言。 |
| deliveries.jsonl | 时间字段以原音出现时为零点，以毫秒计。null 是回执未确定可见时间。 |
| checks.jsonl | 计划抽样包含纳入概率，客诉不是概率样本。 |
| contracts.jsonl | 同一内容在同一可见范围的坏时长扣减有上限。明示采用结算货币分与毫秒。 |
| claims.jsonl | 反证与争议款，期间 1 已关账，期间 2 开放。 |
| terminology.jsonl | 同一术语表覆盖多个语言渠道。反馈时间由各频道接收设备提供。 |
| shared_limits.jsonl | 同一 delivery group 的扣减共用额度，争议占用与已清算金额按期提供。 |
| counterclaims.jsonl | possible_world 指责任主张互斥组；同时发生的独立主张以不同组给出。 |

## 核对资料

运行环境为 Python 3.11，使用标准库，无需下载依赖。用 `python3 -m unittest discover -s tests -v` 核对发行资料的完整性与约定的输入关系。

## 复核引擎

`subtitle_qc/` 在上述记录上实现观众体验度量与采购争议复核（规则与可回查链路见
`subtitle_qc/DESIGN.md`）：

- `python3 -m subtitle_qc.reporting --records records --out reports --json` 全项目复核；
- `python3 -m subtitle_qc.reporting --records records --delivery T14-es-first` 沿单条播出凭证回看版本链、证据、判责与资金动作；
- 引擎只读 `records/`，加载时按 catalog 校验 sha256 与行数，不回写业务数据。
