"""设备型号偏差的罕见度分析（纯计算模块，不触碰数据库）。

背景：已发布的训练/评估清单可能偏向某种扫描设备（型号仅 A/B）。数据员需要
判断“当前清单的两组 A 占比之差”在**清单原切分规则**下有多罕见，以决定是否重切。

原切分规则（与 app.splitter 完全一致，均为硬约束）：
1. 同一患者的所有病例同组，亲属关系（传递闭包）相连的病例同组——即先求亲属
   连通分量，分量整体进组；
2. 两组 × 两标签（0/1）的病例数必须精确等于清单原配额（四格精确配额）；
3. 被锁定的病例（及其所在连通分量）必须进入锁定组。

在以上规则下枚举**所有合法切分**（每个未锁定连通分量只有进组 1 / 进组 2 两种
选择，分量数 ≤ 16 时至多 2^16 = 65536 种，可精确穷举，不做抽样）：

- 现值：当前清单两组的 A 占比之差（按病例数计算，组1减组2，带符号）；
- 罕见数：所有合法切分中，偏差绝对值不小于现值绝对值的切分数量（当前清单本身
  是合法切分，故该数至少为 1）；
- 精确比例 = 罕见数 / 合法切分总数，用分数精确表示。

所有占比与差值均使用 fractions.Fraction 精确运算，避免浮点比较误差。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Dict, List, Optional, Sequence

from .splitter import Case, RelativePair, build_components

# 网页只允许对亲属连通分量不超过该值的有效清单做分析（保证可精确穷举）
MAX_BIAS_COMPONENTS = 16
# DFS 中每隔多少个叶子调用一次取消/失效检查
_CANCEL_CHECK_INTERVAL = 1024


class BiasError(Exception):
    """分析输入本身的问题（区别于计算期间清单失效的 StaleVersionError）。"""


@dataclass(frozen=True)
class ComponentInfo:
    root: str
    members: List[str]
    label_counts: Dict[int, int]            # {0: n, 1: n}
    device_counts: Dict[str, int]           # {"A": n, "B": n}
    locked_group: Optional[int]             # None=未锁定，否则 1|2


@dataclass
class _Counters:
    legal_total: int = 0
    rare_count: int = 0
    leaves_seen: int = 0


def _frac_to_json(frac: Fraction) -> Dict[str, object]:
    return {
        "numerator": frac.numerator,
        "denominator": frac.denominator,
        "decimal": round(float(frac), 6),
    }


def _build_component_infos(
    cases: Sequence[Case],
    relative_pairs: Sequence[RelativePair],
    devices: Dict[str, str],
    locks: Dict[str, int],
) -> List[ComponentInfo]:
    components, _merges = build_components(cases, relative_pairs)
    case_index = {c.case_id: c for c in cases}
    infos: List[ComponentInfo] = []
    for root in sorted(components):  # 固定顺序，保证枚举确定性
        members = components[root]
        label_counts = {0: 0, 1: 0}
        device_counts = {"A": 0, "B": 0}
        lock_groups = set()
        for cid in members:
            label_counts[case_index[cid].label] += 1
            device_counts[devices[cid]] += 1
            if cid in locks:
                lock_groups.add(locks[cid])
        if len(lock_groups) > 1:
            raise BiasError(
                f"连通分量（代表病例 {root}，含 {len(members)} 个病例："
                f"{', '.join(members[:6])}{' …' if len(members) > 6 else ''}）内的锁定"
                f"分组互相冲突（同时要求进入组 {sorted(lock_groups)}），不存在合法切分。"
            )
        infos.append(
            ComponentInfo(
                root=root,
                members=members,
                label_counts=label_counts,
                device_counts=device_counts,
                locked_group=next(iter(lock_groups)) if lock_groups else None,
            )
        )
    return infos


def analyze_device_bias(
    input_data: dict,
    assignments: Dict[str, int],
    devices: Dict[str, str],
    should_cancel: Optional[Callable[[], None]] = None,
) -> dict:
    """执行设备偏差罕见度分析。

    :param input_data: 已发布清单持久化的输入依据（cases/relatives/quotas/locks）
    :param assignments: 已发布清单的病例 -> 分组(1|2) 映射
    :param devices: 病例号 -> 设备型号("A"|"B")，须覆盖全部病例
    :param should_cancel: 可选回调；DFS 周期性调用，抛异常即中止分析（用于计算期间
                          清单失效的 409 语义），不会写入或修改任何数据
    """
    cases = [Case(**c) for c in input_data["cases"]]
    relatives = [RelativePair(**r) for r in input_data.get("relatives", [])]
    locks = {k: int(v) for k, v in input_data.get("locks", {}).items()}
    quotas: Dict[tuple, int] = {
        (g, l): int(input_data["quotas"][f"g{g}_label{l}"])
        for g in (1, 2) for l in (0, 1)
    }

    if not cases:
        raise BiasError("清单没有病例，无法分析。")

    components = _build_component_infos(cases, relatives, devices, locks)
    component_count = len(components)
    if component_count > MAX_BIAS_COMPONENTS:
        raise BiasError(
            f"该清单的亲属连通分量为 {component_count} 个，超过 "
            f"{MAX_BIAS_COMPONENTS} 个的分析上限，无法在精确穷举范围内给出结论。"
        )

    case_ids = {c.case_id for c in cases}
    if set(devices) != case_ids:
        # 正常由上层 CSV 校验拦截，这里做防御性检查
        raise BiasError("设备型号数据与清单病例集合不一致。")
    if set(assignments) != case_ids:
        raise BiasError("清单分组数据与病例集合不一致。")

    # ------------------------------------------------------------------ #
    # 现值：当前清单两组的 A 占比之差（组1减组2，带符号，精确分数）
    # ------------------------------------------------------------------ #
    group_sizes = {1: 0, 2: 0}
    group_a = {1: 0, 2: 0}
    for cid, grp in assignments.items():
        if grp not in (1, 2):
            raise BiasError(f"清单中病例 {cid} 的分组非法：{grp!r}")
        group_sizes[grp] += 1
        if devices[cid] == "A":
            group_a[grp] += 1
    if group_sizes[1] == 0 or group_sizes[2] == 0:
        raise BiasError(
            "当前清单的训练集（组1）或评估集（组2）为空，无法计算两组 A 占比之差，"
            "请拒绝该设备数据并检查清单配额。"
        )

    prop1 = Fraction(group_a[1], group_sizes[1])
    prop2 = Fraction(group_a[2], group_sizes[2])
    current_bias = prop1 - prop2
    current_abs = abs(current_bias)
    total_device_a = group_a[1] + group_a[2]
    n1, n2 = group_sizes[1], group_sizes[2]

    # ------------------------------------------------------------------ #
    # 锁定分量先固定占位，剩余四格配额由未锁定分量枚举填充
    # ------------------------------------------------------------------ #
    used = {(g, l): 0 for g in (1, 2) for l in (0, 1)}
    free_components: List[ComponentInfo] = []
    for comp in components:
        if comp.locked_group is None:
            free_components.append(comp)
            continue
        g = comp.locked_group
        for lab, cnt in comp.label_counts.items():
            used[(g, lab)] += cnt

    for key, target in quotas.items():
        if used[key] > target:
            raise BiasError(
                "清单的锁定约束与四格配额互相冲突，不存在任何合法切分，无法分析。"
            )

    counters = _Counters()

    def _leaf(a_in_group1: int) -> None:
        """所有分量已放置且四格配额恰好匹配：这是一个合法切分。"""
        counters.legal_total += 1
        bias = Fraction(a_in_group1, n1) - Fraction(total_device_a - a_in_group1, n2)
        if abs(bias) >= current_abs:
            counters.rare_count += 1

    def _dfs(
        idx: int,
        used_local: Dict[tuple, int],
        a_in_group1: int,
    ) -> None:
        if should_cancel is not None and counters.leaves_seen >= _CANCEL_CHECK_INTERVAL:
            counters.leaves_seen = 0
            should_cancel()
        if idx == len(free_components):
            counters.leaves_seen += 1
            if used_local == quotas:
                _leaf(a_in_group1)
            return
        comp = free_components[idx]
        # 该分量进组 1 / 组 2 两种选择；放不下四格剩余配额的分支直接剪枝
        for grp in (1, 2):
            fit = True
            for lab, cnt in comp.label_counts.items():
                if used_local[(grp, lab)] + cnt > quotas[(grp, lab)]:
                    fit = False
                    break
            if not fit:
                continue
            for lab, cnt in comp.label_counts.items():
                used_local[(grp, lab)] += cnt
            _dfs(
                idx + 1,
                used_local,
                a_in_group1 + (comp.device_counts["A"] if grp == 1 else 0),
            )
            for lab, cnt in comp.label_counts.items():
                used_local[(grp, lab)] -= cnt

    # 锁定分量占位的 A 数即枚举起点的组1 A 计数
    initial_a_group1 = sum(
        comp.device_counts["A"] for comp in components if comp.locked_group == 1
    )
    _dfs(0, used, initial_a_group1)

    if counters.legal_total == 0:
        # 已发布清单本身就是合法切分，正常不会走到；防御性报错而非给出错误结论
        raise BiasError(
            "按清单原规则枚举不到任何合法切分（清单数据可能已损坏），无法给出结论。"
        )

    ratio = Fraction(counters.rare_count, counters.legal_total)
    single = counters.legal_total == 1
    if single:
        judgement = (
            "合法切分仅有一种，当前偏差无法判断是否异常：没有其他切分可作为对照，"
            "罕见度恒为 1/1，不能据此认定设备偏差罕见或常见。"
        )
    elif counters.rare_count == counters.legal_total:
        judgement = (
            f"全部 {counters.legal_total} 种合法切分的偏差绝对值都不小于现值，"
            "当前偏差在原切分规则下并不罕见。"
        )
    else:
        judgement = (
            f"在 {counters.legal_total} 种合法切分中，仅 {counters.rare_count} 种的"
            f"偏差绝对值不小于现值（精确比例 {ratio.numerator}/{ratio.denominator}），"
            "可据此判断当前设备偏差的罕见程度。"
        )

    return {
        "component_count": component_count,
        "free_component_count": len(free_components),
        "case_count": len(cases),
        "group_sizes": {"1": n1, "2": n2},
        "group_a_counts": {"1": group_a[1], "2": group_a[2]},
        "device_a_proportions": {
            "1": _frac_to_json(prop1),
            "2": _frac_to_json(prop2),
        },
        "current_bias": _frac_to_json(current_bias),
        "current_bias_abs": _frac_to_json(current_abs),
        "legal_split_total": counters.legal_total,
        "rare_count": counters.rare_count,
        "rare_ratio": _frac_to_json(ratio),
        "single_legal_split": single,
        "judgement": judgement,
        "components": [
            {
                "root": comp.root,
                "case_count": len(comp.members),
                "label_counts": {str(k): v for k, v in comp.label_counts.items()},
                "device_counts": dict(comp.device_counts),
                "locked_group": comp.locked_group,
            }
            for comp in components
        ],
    }
