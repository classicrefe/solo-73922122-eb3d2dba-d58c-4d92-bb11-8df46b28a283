"""输入数据结构、CSV 解析与校验。"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .splitter import Case, RelativePair

MAX_CASE_ROWS = 200
MAX_RELATIVE_ROWS = 200

CASE_HEADER_ALIASES = {
    "病例号": "case_id",
    "case_id": "case_id",
    "caseid": "case_id",
    "id": "case_id",
    "患者号": "patient_id",
    "patient_id": "patient_id",
    "patientid": "patient_id",
    "标签": "label",
    "label": "label",
}

REL_HEADER_ALIASES = {
    "患者a": "patient_a",
    "患者_a": "patient_a",
    "patient_a": "patient_a",
    "患者b": "patient_b",
    "患者_b": "patient_b",
    "patient_b": "patient_b",
}

DEVICE_HEADER_ALIASES = {
    "病例号": "case_id",
    "case_id": "case_id",
    "caseid": "case_id",
    "id": "case_id",
    "设备型号": "device",
    "设备": "device",
    "device": "device",
    "device_model": "device",
    "model": "device",
}

# 设备型号只允许 A / B（大小写不敏感，统一转大写）
VALID_DEVICES = {"A", "B"}
MAX_DEVICE_ROWS = 200


class ValidationError(Exception):
    def __init__(self, errors: List[str]):
        super().__init__("；".join(errors))
        self.errors = errors


@dataclass
class ActiveInput:
    """当前生效的完整输入，整体作为一个版本持久化。"""

    cases: List[Case] = field(default_factory=list)
    relatives: List[RelativePair] = field(default_factory=list)
    # (group, label) -> 精确数量
    quotas: Dict[Tuple[int, int], int] = field(default_factory=dict)
    # case_id -> group(1|2)
    locks: Dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "cases": [
                {"case_id": c.case_id, "patient_id": c.patient_id, "label": c.label}
                for c in self.cases
            ],
            "relatives": [
                {"patient_a": r.patient_a, "patient_b": r.patient_b}
                for r in self.relatives
            ],
            "quotas": {f"g{g}_label{l}": self.quotas.get((g, l), 0)
                       for g in (1, 2) for l in (0, 1)},
            "locks": dict(sorted(self.locks.items())),
        }

    @classmethod
    def from_json(cls, data: dict) -> "ActiveInput":
        return cls(
            cases=[Case(**c) for c in data.get("cases", [])],
            relatives=[RelativePair(**r) for r in data.get("relatives", [])],
            quotas={
                (1, 0): data.get("quotas", {}).get("g1_label0", 0),
                (1, 1): data.get("quotas", {}).get("g1_label1", 0),
                (2, 0): data.get("quotas", {}).get("g2_label0", 0),
                (2, 1): data.get("quotas", {}).get("g2_label1", 0),
            },
            locks={k: int(v) for k, v in data.get("locks", {}).items()},
        )


def _normalize_header(name: str) -> str:
    return name.strip().lstrip("﻿").lower().replace(" ", "")


def _read_csv_rows(raw: bytes) -> Tuple[List[str], List[List[str]]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ValidationError([f"文件编码无法识别（需要 UTF-8 或 GB18030）：{exc}"])
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader]
    if not rows:
        raise ValidationError(["CSV 文件为空。"])
    header = [_normalize_header(h) for h in rows[0]]
    body = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    return header, body


def parse_cases_csv(raw: bytes) -> List[Case]:
    header, body = _read_csv_rows(raw)
    errors: List[str] = []
    try:
        cols = [CASE_HEADER_ALIASES.get(h) for h in header]
    except KeyError:  # 兜底，正常不会走到
        cols = []
    missing = [
        name for name in ("case_id", "patient_id", "label") if name not in (cols or [])
    ]
    if missing:
        raise ValidationError([
            "病例 CSV 表头缺少列：" + "、".join(missing)
            + f"。当前表头：{', '.join(header)}"
        ])
    if len(body) > MAX_CASE_ROWS:
        raise ValidationError([f"病例数据最多 {MAX_CASE_ROWS} 行，当前 {len(body)} 行。"])
    if not body:
        raise ValidationError(["病例 CSV 没有数据行。"])

    idx = {name: cols.index(name) for name in ("case_id", "patient_id", "label")}
    cases: List[Case] = []
    seen: Dict[str, int] = {}
    for line_no, row in enumerate(body, start=2):
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        case_id = row[idx["case_id"]].strip()
        patient_id = row[idx["patient_id"]].strip()
        label_raw = row[idx["label"]].strip()
        if not case_id:
            errors.append(f"第 {line_no} 行：病例号为空")
            continue
        if not patient_id:
            errors.append(f"第 {line_no} 行：患者号为空（病例 {case_id}）")
            continue
        if label_raw not in ("0", "1"):
            errors.append(f"第 {line_no} 行：标签必须是 0 或 1，实际为 “{label_raw}”（病例 {case_id}）")
            continue
        if case_id in seen:
            errors.append(f"第 {line_no} 行：病例号 {case_id} 与第 {seen[case_id]} 行重复")
            continue
        seen[case_id] = line_no
        cases.append(Case(case_id=case_id, patient_id=patient_id, label=int(label_raw)))
    if errors:
        raise ValidationError(errors)
    return cases


def parse_relatives_csv(raw: bytes, known_patients: List[str]) -> List[RelativePair]:
    header, body = _read_csv_rows(raw)
    cols = [REL_HEADER_ALIASES.get(h) for h in header]
    missing = [
        name for name in ("patient_a", "patient_b") if name not in (cols or [])
    ]
    if missing:
        raise ValidationError([
            "亲属 CSV 表头缺少列：" + "、".join(missing)
            + f"。当前表头：{', '.join(header)}"
        ])
    if len(body) > MAX_RELATIVE_ROWS:
        raise ValidationError([f"亲属数据最多 {MAX_RELATIVE_ROWS} 行，当前 {len(body)} 行。"])
    if not body:
        raise ValidationError(["亲属 CSV 没有数据行。"])

    ia, ib = cols.index("patient_a"), cols.index("patient_b")
    known = set(known_patients)
    pairs: List[RelativePair] = []
    seen = set()
    errors: List[str] = []
    for line_no, row in enumerate(body, start=2):
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        a, b = row[ia].strip(), row[ib].strip()
        if not a or not b:
            errors.append(f"第 {line_no} 行：患者号不能为空")
            continue
        if a == b:
            errors.append(f"第 {line_no} 行：亲属对两端不能是同一患者（{a}）")
            continue
        for p in (a, b):
            if p not in known:
                errors.append(f"第 {line_no} 行：患者 {p} 不存在于病例数据中")
        key = tuple(sorted((a, b)))
        if key in seen:
            continue  # 完全重复的亲属对直接去重
        seen.add(key)
        pairs.append(RelativePair(patient_a=a, patient_b=b))
    if errors:
        raise ValidationError(errors)
    return pairs


def validate_quotas(payload: dict, total_by_label: Dict[int, int]) -> Dict[Tuple[int, int], int]:
    """仅做类型/范围校验；配额是否可行交给 z3 判定（无解时输出冲突集）。"""
    quotas: Dict[Tuple[int, int], int] = {}
    errors: List[str] = []
    for g in (1, 2):
        for lab in (0, 1):
            key = f"g{g}_label{lab}"
            val = payload.get(key)
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                errors.append(f"组{g} 标签{lab} 的配额必须是非负整数，收到：{val!r}")
                continue
            quotas[(g, lab)] = val
    if errors:
        raise ValidationError(errors)
    # 总数明显不可能时提前给出可读提示，但不阻断——仍由求解器给出 MUS
    return quotas


def validate_locks(payload: list, known_case_ids: List[str]) -> Dict[str, int]:
    known = set(known_case_ids)
    locks: Dict[str, int] = {}
    errors: List[str] = []
    for item in payload:
        if not isinstance(item, dict) or "case_id" not in item or "group" not in item:
            errors.append(f"锁定项格式错误：{item!r}")
            continue
        cid = str(item["case_id"]).strip()
        grp = item["group"]
        if cid not in known:
            errors.append(f"锁定了不存在的病例：{cid}")
            continue
        if grp not in (1, 2):
            errors.append(f"病例 {cid} 的锁定分组必须是 1 或 2，收到：{grp!r}")
            continue
        locks[cid] = grp
    if errors:
        raise ValidationError(errors)
    return locks


def parse_devices_csv(raw: bytes, known_case_ids: List[str]) -> Dict[str, str]:
    """解析“病例号,设备型号”CSV。

    校验规则（任一不满足整体拒绝，不改动任何已入库数据）：
    - 表头必须包含“病例号”和“设备型号”两列；
    - 行数上限 MAX_DEVICE_ROWS；
    - 病例号不能为空、不能重复；
    - 设备型号只能是 A 或 B（大小写不敏感，空值/其他值均拒绝）；
    - 病例必须全部存在于清单依据的病例集合中，且清单的每个病例都必须出现
      （未知、缺失都会拒绝）。

    返回 case_id -> "A"|"B" 的完整映射。
    """
    header, body = _read_csv_rows(raw)
    cols = [DEVICE_HEADER_ALIASES.get(h) for h in header]
    missing = [name for name in ("case_id", "device") if name not in (cols or [])]
    if missing:
        raise ValidationError([
            "设备 CSV 表头缺少列：" + "、".join(missing)
            + f"。当前表头：{', '.join(header)}"
        ])
    if len(body) > MAX_DEVICE_ROWS:
        raise ValidationError(
            [f"设备数据最多 {MAX_DEVICE_ROWS} 行，当前 {len(body)} 行。"]
        )

    ic, idv = cols.index("case_id"), cols.index("device")
    known = set(known_case_ids)
    devices: Dict[str, str] = {}
    seen: Dict[str, int] = {}
    errors: List[str] = []
    for line_no, row in enumerate(body, start=2):
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        cid = row[ic].strip()
        device_raw = row[idv].strip()
        if not cid:
            errors.append(f"第 {line_no} 行：病例号为空")
            continue
        if cid in seen:
            errors.append(f"第 {line_no} 行：病例号 {cid} 与第 {seen[cid]} 行重复")
            continue
        if not device_raw:
            errors.append(f"第 {line_no} 行：设备型号为空（病例 {cid}）")
            continue
        device = device_raw.upper()
        if device not in VALID_DEVICES:
            errors.append(
                f"第 {line_no} 行：设备型号只能是 A 或 B，实际为 “{device_raw}”（病例 {cid}）"
            )
            continue
        if cid not in known:
            errors.append(f"第 {line_no} 行：病例 {cid} 不在该清单的病例范围内")
            continue
        seen[cid] = line_no
        devices[cid] = device
    if errors:
        raise ValidationError(errors)

    missing_cases = sorted(known - set(devices))
    if missing_cases:
        preview = "、".join(missing_cases[:10])
        more = f" …… 共 {len(missing_cases)} 个" if len(missing_cases) > 10 else ""
        raise ValidationError([
            f"设备 CSV 未覆盖全部病例，缺失 {len(missing_cases)} 个：{preview}{more}"
        ])
    return devices
