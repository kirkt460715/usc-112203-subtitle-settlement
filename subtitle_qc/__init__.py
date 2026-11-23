"""多语赛事转播字幕质检与交付争议复核引擎。

模块划分
--------
records     只读加载合成业务记录（含 catalog 校验）
metrics     计算观众实际经历的字幕延迟与信息缺口，区分四类相位
coverage    按语种/赛段/覆盖强度解释可核实范围，客诉不进入概率推断
defects     关键术语错误与普通迟到的合同口径分流
claims      反证、互斥责任主张、共同扣减上限与关账期规则
ledger      下账期更正的三类资金动作与可回查链路
reporting   汇总报告与同一项目入口的命令行
"""

__all__ = [
    "records",
    "metrics",
    "coverage",
    "defects",
    "claims",
    "ledger",
    "reporting",
]
