"""病例切分核心：z3 约束求解 + 不可再缩减冲突集（MUS）提取。

规则（均为硬约束，不使用近似比例）：
1. 同一患者的所有病例必须同组；
2. 患者亲属关系沿“同一患者 + 亲属对”传递，传递相连的病例必须同组；
3. 每个病例属于训练集（组 1）或评估集（组 2）之一；
4. 两组各标签（0/1）的病例数必须精确等于配额；
5. 被锁定的病例必须进入指定组。

无解时返回不可再缩减的冲突条件集合：其中每条约束都是导致无解的必要条件
（删除任意一条后其余约束即可满足），并标注冲突来源属于配额、锁定还是亲属关系。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import z3

GROUP_NAMES = {1: "训练集", 2: "评估集"}


@dataclass(frozen=True)
class Case:
    case_id: str
    patient_id: str
    label: int


@dataclass(frozen=True)
class RelativePair:
    patient_a: str
    patient_b: str


@dataclass(frozen=True)
class ConstraintInfo:
    cid: str
    kind: str  # kinship | lock | quota
    description: str
    detail: Dict[str, object] = field(default_factory=dict)


class SplitterError(Exception):
    """输入数据本身的问题（在进入求解器之前校验）。"""


# --------------------------------------------------------------------------- #
# 并查集：把同一患者、亲属关系传递相连的病例合并为连通分量，
# 同时保留每一次实际合并对应的可解释约束（供无解冲突输出使用）。
# --------------------------------------------------------------------------- #
def build_components(
    cases: Sequence[Case], relative_pairs: Sequence[RelativePair]
) -> Tuple[Dict[str, List[str]], List[Tuple[str, str, str, dict]]]:
    parent: Dict[str, str] = {c.case_id: c.case_id for c in cases}

    def find(x: str) -> str:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != x:
            parent[x], x = root, parent[x]
        return root

    merges: List[Tuple[str, str, str, dict]] = []

    def union(a: str, b: str, reason: str, detail: dict) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # 固定方向，保证结果确定性
        if ra > rb:
            ra, rb = rb, ra
        parent[rb] = ra
        merges.append((rb, ra, reason, detail))

    # 1) 同一患者的病例天然同组
    by_patient: Dict[str, List[str]] = {}
    for c in cases:
        by_patient.setdefault(c.patient_id, []).append(c.case_id)
    for patient_id, ids in sorted(by_patient.items()):
        anchor = ids[0]
        for other in ids[1:]:
            union(
                anchor,
                other,
                "same_patient",
                {"patient_id": patient_id},
            )

    # 2) 亲属对在患者层面合并（取每位患者的一个代表病例）
    representative = {patient_id: ids[0] for patient_id, ids in by_patient.items()}
    for pair in sorted(relative_pairs, key=lambda p: (p.patient_a, p.patient_b)):
        ca = representative.get(pair.patient_a)
        cb = representative.get(pair.patient_b)
        if ca is None or cb is None:
            # 调用方应已校验，这里防御性跳过
            continue
        union(
            ca,
            cb,
            "relative",
            {"patient_a": pair.patient_a, "patient_b": pair.patient_b},
        )

    components: Dict[str, List[str]] = {}
    for c in cases:
        components.setdefault(find(c.case_id), []).append(c.case_id)
    for ids in components.values():
        ids.sort()
    return components, merges


def _check_unsat(solver: z3.Solver, handler_by_id: Dict[str, z3.BoolRef], cids) -> bool:
    return solver.check(*[handler_by_id[h] for h in cids]) == z3.unsat


def _delete_chunk(
    solver: z3.Solver,
    handler_by_id: Dict[str, z3.BoolRef],
    kept: List[str],
    chunk: List[str],
) -> List[str]:
    """尝试从 kept 中整体删除 chunk。

    - 删除后仍 unsat：整块约束都不是必要的，直接丢弃；
    - 删除后变 sat：块内至少含一条必要约束，二分递归定位，保留必要项。
    返回应继续保留的约束（顺序保持稳定）。
    """
    chunk_set = set(chunk)
    if _check_unsat(solver, handler_by_id, [c for c in kept if c not in chunk_set]):
        return [c for c in kept if c not in chunk_set]
    if len(chunk) == 1:
        return kept  # 该条是必要约束
    mid = len(chunk) // 2
    kept = _delete_chunk(solver, handler_by_id, kept, chunk[:mid])
    kept = _delete_chunk(solver, handler_by_id, kept, chunk[mid:])
    return kept


def _shrink_to_mus(
    solver: z3.Solver,
    handler_by_id: Dict[str, z3.BoolRef],
    initial_core: set,
) -> set:
    """从 z3 给出的 unsat core 出发，得到不可再缩减的子集（MUS）。

    小 core 用逐条试探（每条最多一次 check）；大 core 先按块删除再二分定位，
    显著减少求解器调用次数。最终逐条后置验证：MUS 整体 unsat，且删除其中
    任意一条后不再 unsat。
    """
    mus = sorted(initial_core)
    if len(mus) <= 24:
        for cid in list(mus):
            if _check_unsat(solver, handler_by_id, [c for c in mus if c != cid]):
                mus.remove(cid)
    else:
        chunk_size = 32
        i = 0
        while i < len(mus):
            chunk = mus[i:i + chunk_size]
            new_mus = _delete_chunk(solver, handler_by_id, mus, chunk)
            if len(new_mus) == len(mus):
                i += len(chunk)  # 整块都必要，前进
            mus = new_mus
            # 块被部分/整体删除后，i 不变，新的元素滑入当前窗口
    # 终检：整体 unsat；删任意一条后非 unsat（必要条件全部保留）
    assert _check_unsat(solver, handler_by_id, mus)
    for cid in mus:
        assert not _check_unsat(
            solver, handler_by_id, [c for c in mus if c != cid]
        )
    return set(mus)


def solve_split(
    cases: Sequence[Case],
    relative_pairs: Sequence[RelativePair],
    quotas: Dict[Tuple[int, int], int],
    locks: Dict[str, int],
) -> dict:
    """执行切分求解。

    返回::

        {"status": "sat", "assignments": {case_id: 1|2}, "stats": {...},
         "constraint_count": n, "component_count": m}
        {"status": "unsat", "conflicts": [ConstraintInfo...],
         "mus_verified": True, ...}
    """
    cases = sorted(cases, key=lambda c: c.case_id)
    if not cases:
        raise SplitterError("没有可切分的病例，请先上传病例 CSV。")

    components, merges = build_components(cases, relative_pairs)
    case_index = {c.case_id: c for c in cases}

    unknown_locks = sorted(set(locks) - set(case_index))
    if unknown_locks:
        raise SplitterError(f"锁定了不存在的病例：{', '.join(unknown_locks)}")
    for cid, grp in locks.items():
        if grp not in (1, 2):
            raise SplitterError(f"病例 {cid} 的锁定分组必须是 1 或 2。")
    for (grp, lab), qty in quotas.items():
        if grp not in (1, 2) or lab not in (0, 1):
            raise SplitterError("配额键必须是 (组 1/2, 标签 0/1)。")
        if not isinstance(qty, int) or qty < 0:
            raise SplitterError(f"{GROUP_NAMES[grp]}标签{lab}的配额必须是非负整数。")

    solver = z3.Solver()
    # 每个病例一个布尔变量：True=评估集(组2)，False=训练集(组1)
    b = {c.case_id: z3.Bool(f"in_group2_{i}_{c.case_id}") for i, c in enumerate(cases)}

    infos: Dict[str, ConstraintInfo] = {}
    handler_by_id: Dict[str, z3.BoolRef] = {}
    counter = 0

    def track(cond: z3.BoolRef, info: ConstraintInfo) -> None:
        nonlocal counter
        handler = z3.Bool(f"handler_{counter}")
        counter += 1
        solver.add(z3.Implies(handler, cond))
        infos[info.cid] = info
        handler_by_id[info.cid] = handler

    # 1) 亲属 / 同一患者连通约束（每一条都可单独出现在冲突集中）
    for child, root, reason, detail in merges:
        if reason == "same_patient":
            desc = (
                f"同一患者 {detail['patient_id']} 的病例 {child} 与 {root} 必须分在同一组"
            )
        else:
            desc = (
                f"患者 {detail['patient_a']} 与 {detail['patient_b']} 为亲属，"
                f"病例 {child} 与 {root} 必须分在同一组"
            )
        track(
            b[child] == b[root],
            ConstraintInfo(
                cid=f"kin:{counter}",
                kind="kinship",
                description=desc,
                detail={**detail, "case_a": child, "case_b": root, "reason": reason},
            ),
        )

    # 2) 锁定约束
    for cid in sorted(locks):
        grp = locks[cid]
        track(
            b[cid] == (grp == 2),
            ConstraintInfo(
                cid=f"lock:{cid}",
                kind="lock",
                description=f"病例 {cid} 被锁定到{GROUP_NAMES[grp]}",
                detail={"case_id": cid, "group": grp},
            ),
        )

    # 3) 精确配额约束（两组 × 两标签，共四条）
    for grp in (1, 2):
        for lab in (0, 1):
            qty = quotas[(grp, lab)]
            same_label = [c for c in cases if c.label == lab]
            terms = [z3.If(b[c.case_id] == (grp == 2), 1, 0) for c in same_label]
            info = ConstraintInfo(
                cid=f"quota:g{grp}:l{lab}",
                kind="quota",
                description=(
                    f"{GROUP_NAMES[grp]}中标签 {lab} 的病例数量必须恰好为 {qty}"
                ),
                detail={"group": grp, "label": lab, "target": qty},
            )
            if terms:
                track(z3.Sum(terms) == qty, info)
            elif qty > 0:
                # 该标签根本没有病例，却要求正数配额：恒假约束，同样可被 MUS 追踪
                track(z3.BoolVal(False), info)

    assumptions = [handler_by_id[cid] for cid in sorted(infos)]
    result = solver.check(*assumptions)

    if result == z3.unknown:
        raise SplitterError(f"求解器无法判定（{solver.reason_unknown()}）。")

    if result == z3.sat:
        model = solver.model()
        assignments: Dict[str, int] = {}
        for c in cases:
            assignments[c.case_id] = (
                2 if z3.is_true(model.eval(b[c.case_id], model_completion=True)) else 1
            )
        # 独立复核配额与连通性，防止模型解读错误
        counts = {(g, l): 0 for g in (1, 2) for l in (0, 1)}
        for c in cases:
            counts[(assignments[c.case_id], c.label)] += 1
        if counts != dict(quotas):
            raise SplitterError(f"内部错误：模型配额不符 {counts} != {quotas}")
        for child, root, _reason, _detail in merges:
            if assignments[child] != assignments[root]:
                raise SplitterError("内部错误：模型违反连通约束")
        for cid, grp in locks.items():
            if assignments[cid] != grp:
                raise SplitterError(f"内部错误：模型违反锁定 {cid}")
        return {
            "status": "sat",
            "assignments": assignments,
            "stats": {
                "case_count": len(cases),
                "patient_count": len({c.patient_id for c in cases}),
                "component_count": len(components),
                "counts": {f"g{g}_label{l}": counts[(g, l)] for g in (1, 2) for l in (0, 1)},
            },
            "constraint_count": len(infos),
        }

    # unsat：提取不可再缩减冲突集
    core_handler_objs = set(solver.unsat_core())
    initial_core = {
        cid for cid, h in handler_by_id.items() if h in core_handler_objs
    }
    if not initial_core:  # 理论上不会发生
        initial_core = set(infos)
    mus = _shrink_to_mus(solver, handler_by_id, initial_core)
    conflicts = [_info_to_json(infos[cid]) for cid in sorted(mus)]
    kinds = sorted({infos[cid].kind for cid in mus})
    return {
        "status": "unsat",
        "conflicts": conflicts,
        "mus_verified": True,
        "stats": {
            "case_count": len(cases),
            "patient_count": len({c.patient_id for c in cases}),
            "component_count": len(components),
        },
        "constraint_count": len(infos),
        "conflict_kinds": kinds,
        "summary": (
            f"无解：以下 {len(conflicts)} 条约束互相冲突"
            f"（已验证不可再缩减，删除其中任意一条后其余约束即可满足）。"
            f"冲突来源类型：{ '、'.join(_kind_name(k) for k in kinds) }。"
        ),
    }


def _kind_name(kind: str) -> str:
    return {"quota": "配额", "lock": "锁定", "kinship": "亲属/同患者关系"}.get(kind, kind)


def _info_to_json(info: ConstraintInfo) -> dict:
    return {
        "cid": info.cid,
        "kind": info.kind,
        "kind_name": _kind_name(info.kind),
        "description": info.description,
        "detail": info.detail,
    }
