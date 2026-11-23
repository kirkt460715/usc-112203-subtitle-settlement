"""只读数据访问层：加载 records/ 下的合成业务记录并做完整性校验。

设计原则：
- 所有记录按原文件 JSON 反序列化，引擎不在加载阶段改写任何业务字段；
- catalog.json 给出的行数与 sha256 是发行资料的校验依据；
- 跨表键（delivery、segment、supersedes 等）的业务关系由上层模块解释。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

TABLES = (
    "segments",
    "deliveries",
    "checks",
    "contracts",
    "claims",
    "terminology",
    "shared_limits",
    "counterclaims",
)


class RecordError(ValueError):
    """发行资料不完整或被篡改时抛出。"""


@dataclass(frozen=True)
class ProjectRecords:
    """一个复核项目的全部输入记录，按表名与业务 id 建索引。"""

    root: Path
    catalog: dict[str, dict[str, Any]]
    rows: dict[str, list[dict[str, Any]]]

    def table(self, name: str) -> list[dict[str, Any]]:
        return self.rows[name]

    def by_id(self, name: str) -> dict[str, dict[str, Any]]:
        return {row["id"]: row for row in self.rows[name]}

    def segment(self, segment_id: str) -> dict[str, Any]:
        return self.by_id("segments")[segment_id]

    def delivery(self, delivery_id: str) -> dict[str, Any]:
        return self.by_id("deliveries")[delivery_id]

    def contract_for(self, language: str) -> dict[str, Any]:
        for contract in self.rows["contracts"]:
            if contract["language"] == language:
                return contract
        raise RecordError(f"语言 {language} 没有对应合同")

    def checks_for(self, delivery_id: str) -> list[dict[str, Any]]:
        return [c for c in self.rows["checks"] if c["delivery"] == delivery_id]

    def claims_for(self, delivery_id: str) -> list[dict[str, Any]]:
        return [c for c in self.rows["claims"] if c["delivery"] == delivery_id]

    def replacements_for(self, delivery_id: str) -> list[dict[str, Any]]:
        """沿 supersedes 链找出同一播出凭证的后续回执（按接收顺序）。"""
        out = []
        current = delivery_id
        by_supersede: dict[str, list[dict[str, Any]]] = {}
        for d in self.rows["deliveries"]:
            if d.get("supersedes"):
                by_supersede.setdefault(d["supersedes"], []).append(d)
        while current in by_supersede:
            nxt = sorted(by_supersede[current], key=lambda d: d["received"])
            out.extend(nxt)
            current = nxt[0]["id"]
        return out


def load_project(root: str | Path) -> ProjectRecords:
    """加载并校验一个项目目录（默认即仓库内的 records/）。"""
    root = Path(root)
    catalog_path = root / "catalog.json"
    if not catalog_path.exists():
        raise RecordError(f"缺少 catalog.json：{catalog_path}")
    catalog = json.loads(catalog_path.read_bytes())

    rows: dict[str, list[dict[str, Any]]] = {}
    for name in TABLES:
        meta = catalog.get(name)
        if meta is None:
            raise RecordError(f"catalog.json 缺少表 {name} 的校验信息")
        path = root / f"{name}.jsonl"
        if not path.exists():
            raise RecordError(f"缺少数据文件 {path}")
        raw = path.read_bytes()
        digest = sha256(raw).hexdigest()
        if digest != meta["sha256"]:
            raise RecordError(f"{name}.jsonl 校验值与 catalog 不一致，资料可能已被替换")
        table_rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if len(table_rows) != meta["rows"]:
            raise RecordError(f"{name}.jsonl 行数 {len(table_rows)} 与 catalog 声明 {meta['rows']} 不符")
        ids = [row["id"] for row in table_rows]
        if len(set(ids)) != len(ids):
            raise RecordError(f"{name}.jsonl 存在重复 id")
        rows[name] = table_rows

    _validate_cross_refs(rows)
    return ProjectRecords(root=root, catalog=catalog, rows=rows)


def _validate_cross_refs(rows: dict[str, list[dict[str, Any]]]) -> None:
    """核对发行资料明示的跨表关系，避免静默错配。"""
    segment_ids = {s["id"] for s in rows["segments"]}
    delivery_ids = {d["id"] for d in rows["deliveries"]}

    for d in rows["deliveries"]:
        if d["segment"] not in segment_ids:
            raise RecordError(f"delivery {d['id']} 指向未知 segment {d['segment']}")
        if d.get("supersedes") and d["supersedes"] not in delivery_ids:
            raise RecordError(f"delivery {d['id']} 的 supersedes 指向未知凭证")
    for c in rows["checks"]:
        if c["delivery"] not in delivery_ids:
            raise RecordError(f"check {c['id']} 指向未知 delivery")
        if c["method"] not in {"scheduled", "complaint"}:
            raise RecordError(f"check {c['id']} 的 method 非法")
    for claim in rows["claims"]:
        if claim["delivery"] not in delivery_ids:
            raise RecordError(f"claim {claim['id']} 指向未知 delivery")
        for ev in claim["evidence"]:
            if not any(ch["id"] == ev for ch in rows["checks"]) and ev not in delivery_ids:
                raise RecordError(f"claim {claim['id']} 的证据 {ev} 无法回查")
    for term in rows["terminology"]:
        if term["segment"] not in segment_ids:
            raise RecordError(f"terminology {term['id']} 指向未知 segment")
        if term.get("supersedes") and not any(t["id"] == term["supersedes"] for t in rows["terminology"]):
            raise RecordError(f"terminology {term['id']} 的 supersedes 无法回查")
    for limit in rows["shared_limits"]:
        for channel in limit["channels"]:
            if channel not in delivery_ids:
                raise RecordError(f"shared_limit {limit['id']} 含未知渠道 {channel}")
    for cc in rows["counterclaims"]:
        if cc["delivery"] not in delivery_ids:
            raise RecordError(f"counterclaim {cc['id']} 指向未知 delivery")
        for claim_id in cc["claims"]:
            if not any(x["id"] == claim_id for x in rows["claims"]):
                raise RecordError(f"counterclaim {cc['id']} 引用未知 claim {claim_id}")
