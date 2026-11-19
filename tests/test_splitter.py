"""求解器核心测试：连通性、精确配额、锁定、MUS 不可再缩减性。"""
import z3

from app.splitter import Case, RelativePair, build_components, solve_split


def C(cid, pid, label):
    return Case(case_id=cid, patient_id=pid, label=label)


def test_components_merge_same_patient_and_transitive_relatives():
    cases = [C("c1", "p1", 0), C("c2", "p1", 1), C("c3", "p2", 0),
             C("c4", "p3", 1), C("c5", "p4", 0)]
    rels = [RelativePair("p1", "p2"), RelativePair("p2", "p3")]
    components, _ = build_components(cases, rels)
    roots = {}
    for root, members in components.items():
        for m in members:
            roots[m] = root
    # p1(两病例)-p2-p3 传递连通
    assert roots["c1"] == roots["c2"] == roots["c3"] == roots["c4"]
    # p4 独立
    assert roots["c5"] != roots["c1"]


def test_sat_exact_quota_and_kinship_grouping():
    # 两个连通分量：{c1,c2}(p1) 与 {c3,c4}(p2)，各含一个0、一个1
    cases = [C("c1", "p1", 0), C("c2", "p1", 1),
             C("c3", "p2", 0), C("c4", "p2", 1)]
    quotas = {(1, 0): 1, (1, 1): 1, (2, 0): 1, (2, 1): 1}
    r = solve_split(cases, [], quotas, {})
    assert r["status"] == "sat"
    a = r["assignments"]
    # 同患者同组
    assert a["c1"] == a["c2"]
    assert a["c3"] == a["c4"]
    # 两组各一个分量
    assert a["c1"] != a["c3"]


def test_unsat_quota_exceeds_total_gives_quota_mus():
    # 只有 2 个标签1病例，却要求组1有3个
    cases = [C("c1", "p1", 1), C("c2", "p2", 1), C("c3", "p3", 0)]
    quotas = {(1, 0): 0, (1, 1): 3, (2, 0): 1, (2, 1): 0}
    r = solve_split(cases, [], quotas, {})
    assert r["status"] == "unsat"
    kinds = {c["kind"] for c in r["conflicts"]}
    assert "quota" in kinds
    assert r["mus_verified"] is True


def test_unsat_lock_vs_quota_conflict():
    # 3 个标签1病例；锁定 c1 到组1，但组1标签1配额为0
    cases = [C("c1", "p1", 1), C("c2", "p2", 1), C("c3", "p3", 1)]
    quotas = {(1, 0): 0, (1, 1): 0, (2, 0): 0, (2, 1): 3}
    r = solve_split(cases, [], quotas, {"c1": 1})
    assert r["status"] == "unsat"
    kinds = {c["kind"] for c in r["conflicts"]}
    assert kinds == {"lock", "quota"}
    # 冲突说明中能看到具体病例与配额值
    text = " ".join(c["description"] for c in r["conflicts"])
    assert "c1" in text and "恰好为 0" in text


def test_unsat_kinship_in_mus():
    # 唯一一对亲属把 p1,p2 绑死；配额要求两个标签1分别进两组
    cases = [C("c1", "p1", 1), C("c2", "p2", 1)]
    rels = [RelativePair("p1", "p2")]
    quotas = {(1, 0): 0, (1, 1): 1, (2, 0): 0, (2, 1): 1}
    r = solve_split(cases, rels, quotas, {})
    assert r["status"] == "unsat"
    kinds = {c["kind"] for c in r["conflicts"]}
    assert "kinship" in kinds


def test_mus_is_irredundant():
    """独立复核：放宽 MUS 中的配额约束后，整体应恢复可满足。"""
    # 分量 {c1,c2}(p1-p2 亲属，两个标签1) 与 {c3}(标签0，锁定到组2)
    cases = [C("c1", "p1", 1), C("c2", "p2", 1), C("c3", "p3", 0)]
    rels = [RelativePair("p1", "p2")]
    # c3 锁定组2，但组2标签0配额为0 -> 锁定与配额冲突，无解
    quotas = {(1, 0): 0, (1, 1): 2, (2, 0): 0, (2, 1): 0}
    r = solve_split(cases, rels, quotas, {"c3": 2})
    assert r["status"] == "unsat"
    conflict_ids = {c["cid"] for c in r["conflicts"]}
    assert len(conflict_ids) >= 2
    kinds = {c["kind"] for c in r["conflicts"]}
    assert "lock" in kinds and "quota" in kinds
    # 放宽单条配额（组2标签0: 0 -> 1）后恢复 sat：c1,c2 进组1，c3 进组2
    relaxed = dict(quotas)
    relaxed[(2, 0)] = 1
    r2 = solve_split(cases, rels, relaxed, {"c3": 2})
    assert r2["status"] == "sat"
    assert r2["assignments"]["c3"] == 2
    assert r2["assignments"]["c1"] == r2["assignments"]["c2"] == 1


def test_zero_quota_for_missing_label_is_unsat_with_tracked_constraint():
    cases = [C("c1", "p1", 0)]
    quotas = {(1, 0): 1, (1, 1): 0, (2, 0): 0, (2, 1): 0}
    r = solve_split(cases, [], quotas, {})
    assert r["status"] == "sat"
    # 要求不存在的标签给出正数配额 -> 恒假，冲突集含该配额
    bad = dict(quotas)
    bad[(2, 1)] = 1
    r2 = solve_split(cases, [], bad, {})
    assert r2["status"] == "unsat"
    assert any(c["detail"].get("target") == 1 for c in r2["conflicts"])


def test_all_locks_respected_and_verified():
    cases = [C(f"c{i}", f"p{i}", i % 2) for i in range(1, 7)]
    quotas = {(1, 0): 2, (1, 1): 1, (2, 0): 1, (2, 1): 2}
    locks = {"c1": 2, "c6": 1}
    r = solve_split(cases, [], quotas, locks)
    assert r["status"] == "sat"
    assert r["assignments"]["c1"] == 2
    assert r["assignments"]["c6"] == 1
    counts = {(g, l): 0 for g in (1, 2) for l in (0, 1)}
    for c in cases:
        counts[(r["assignments"][c.case_id], c.label)] += 1
    assert counts == quotas
