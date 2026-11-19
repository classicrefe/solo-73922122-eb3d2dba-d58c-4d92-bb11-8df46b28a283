"""设备偏差罕见度分析核心测试：枚举正确性、精确比例、锁定、失效中止与边界。"""
import pytest

from app.bias import MAX_BIAS_COMPONENTS, BiasError, analyze_device_bias
from app.models import ValidationError, parse_devices_csv


def _base_input():
    # 4 个单病例分量：标签1×2（c1,c2）、标签0×2（c3,c4）
    return {
        "cases": [
            {"case_id": "c1", "patient_id": "p1", "label": 1},
            {"case_id": "c2", "patient_id": "p2", "label": 1},
            {"case_id": "c3", "patient_id": "p3", "label": 0},
            {"case_id": "c4", "patient_id": "p4", "label": 0},
        ],
        "relatives": [],
        "quotas": {"g1_label0": 1, "g1_label1": 1, "g2_label0": 1, "g2_label1": 1},
        "locks": {},
    }


ASSIGN_HALF = {"c1": 1, "c3": 1, "c2": 2, "c4": 2}


def test_enumeration_counts_all_four_legal_splits():
    r = analyze_device_bias(
        _base_input(), ASSIGN_HALF,
        {"c1": "A", "c2": "A", "c3": "B", "c4": "B"},
    )
    assert r["component_count"] == 4
    assert r["legal_split_total"] == 4
    # 现值偏差为 0，所有切分 |bias| 都 >= 0
    assert r["rare_count"] == 4
    assert r["rare_ratio"]["numerator"] == 1
    assert r["rare_ratio"]["denominator"] == 1
    assert r["single_legal_split"] is False


def test_rare_count_uses_absolute_value_and_exact_fraction():
    # 设备 A/A/A/B（c1,c2,c3=A；c4=B）。四种合法切分的偏差分布为
    # {+1/2: 2, -1/2: 2}，不存在零偏差切分。
    devices_aaab = {"c1": "A", "c2": "A", "c3": "A", "c4": "B"}
    # 当前组1=c1(A),c3(A) 全 A，组2=c2(A),c4(B) 一 A 一 B，现值 bias=+1/2
    r = analyze_device_bias(_base_input(), ASSIGN_HALF, devices_aaab)
    assert r["legal_split_total"] == 4
    assert (r["current_bias"]["numerator"], r["current_bias"]["denominator"]) == (1, 2)
    # 四种切分的 |bias| 都是 1/2，故全部计入罕见数，精确比例 4/4=1/1
    assert r["rare_count"] == 4
    assert (r["rare_ratio"]["numerator"], r["rare_ratio"]["denominator"]) == (1, 1)
    assert r["current_bias_abs"]["numerator"] == 1

    # 设备 A/B/A/B 时偏差分布为 {0: 2, +1: 1, -1: 1}。
    devices_abab = {"c1": "A", "c2": "B", "c3": "A", "c4": "B"}
    assign_zero = {"c1": 1, "c2": 2, "c3": 2, "c4": 1}
    r0 = analyze_device_bias(_base_input(), assign_zero, devices_abab)
    assert r0["current_bias"]["numerator"] == 0
    assert r0["rare_count"] == 4  # 阈值为 0，四种切分全部计入
    # 最大偏差：组1={c1(标签1,A),c3(标签0,A)} 两个 A，组2 两个 B -> bias=±1
    assign_plus_one = {"c1": 1, "c3": 1, "c2": 2, "c4": 2}
    r2 = analyze_device_bias(_base_input(), assign_plus_one, devices_abab)
    assert abs(r2["current_bias"]["decimal"]) == 1.0
    assert r2["rare_count"] == 2
    assert (r2["rare_ratio"]["numerator"], r2["rare_ratio"]["denominator"]) == (1, 2)


def test_signed_bias_direction():
    # 组1=c1(B),c3(B) 两个 B（k=0），组2=c2(A),c4(A) 两个 A：
    # bias = 0/2 - 2/2 = -1
    devices = {"c1": "B", "c2": "A", "c3": "B", "c4": "A"}
    assign_g1 = {"c1": 1, "c3": 1, "c2": 2, "c4": 2}
    r = analyze_device_bias(_base_input(), assign_g1, devices)
    assert r["current_bias"]["numerator"] == -1
    assert r["current_bias"]["denominator"] == 1
    assert r["current_bias"]["decimal"] == -1.0
    assert r["current_bias_abs"]["decimal"] == 1.0
    assert r["rare_count"] == 2  # |bias|=1 的切分共两种（k=0 与 k=3）


def test_single_legal_split_cannot_judge_anomaly():
    locked = dict(_base_input(),
                  locks={"c1": 1, "c3": 1, "c2": 2, "c4": 2})
    r = analyze_device_bias(
        locked, ASSIGN_HALF,
        {"c1": "A", "c2": "A", "c3": "A", "c4": "B"},
    )
    assert r["legal_split_total"] == 1
    assert r["rare_count"] == 1
    assert r["single_legal_split"] is True
    assert "无法判断是否异常" in r["judgement"]


def test_kinship_component_moves_as_a_whole():
    # 标签分布不变（标签1：c1,c2；标签0：c3,c4），用亲属对把 p1,p2 合并，
    # 于是 c1(1) 与 c2(1) 同属分量X；c3(0)、c4(0) 各为单例。
    data = _base_input()
    data["relatives"] = [{"patient_a": "p1", "patient_b": "p2"}]
    devices = {"c1": "A", "c2": "A", "c3": "B", "c4": "B"}
    # 组2 配额 1个标签1+1个标签0：可行 {X 之一?}——X 含两个标签1，
    # 组2 标签1配额为1，X 不能整体进组2；故 X 必须整体进组1，
    # 组2 只能从 c3,c4（都是标签0）里取，无法满足标签1配额 -> 无解。
    # 改为组2 标签1配额 2：组2={X 全部或其一+...}，这里直接验证分量整体移动：
    data["quotas"] = {"g1_label0": 2, "g1_label1": 0,
                      "g2_label0": 0, "g2_label1": 2}
    assign = {"c1": 2, "c2": 2, "c3": 1, "c4": 1}
    r = analyze_device_bias(data, assign, devices)
    assert r["component_count"] == 3
    # 只有 X 整体进组2、c3/c4 整体留组1 一种合法切分
    assert r["legal_split_total"] == 1
    comp_sizes = sorted(c["case_count"] for c in r["components"])
    assert comp_sizes == [1, 1, 2]
    # 两个单例必须同组（都在组1），X 两病例必须同组（都在组2）
    locked_info = {c["root"]: c["case_count"] for c in r["components"]}
    assert sum(locked_info.values()) == 4


def test_lock_conflict_inside_component_rejected():
    data = _base_input()
    data["cases"][1] = {"case_id": "c2", "patient_id": "p1", "label": 0}
    data["locks"] = {"c1": 1, "c2": 2}  # 同分量冲突锁定
    with pytest.raises(BiasError, match="锁定分组互相冲突"):
        analyze_device_bias(
            data, ASSIGN_HALF,
            {"c1": "A", "c2": "A", "c3": "B", "c4": "B"},
        )


def test_empty_group_rejected():
    data = dict(_base_input(), quotas={
        "g1_label0": 2, "g1_label1": 2, "g2_label0": 0, "g2_label1": 0})
    assign_all_g1 = {"c1": 1, "c2": 1, "c3": 1, "c4": 1}
    with pytest.raises(BiasError, match="组.*为空"):
        analyze_device_bias(
            data, assign_all_g1,
            {"c1": "A", "c2": "A", "c3": "B", "c4": "B"},
        )


def test_component_limit_enforced():
    data = {
        "cases": [{"case_id": f"c{i}", "patient_id": f"p{i}", "label": i % 2}
                  for i in range(MAX_BIAS_COMPONENTS + 1)],
        "relatives": [],
        "quotas": {"g1_label0": 5, "g1_label1": 4, "g2_label0": 4, "g2_label1": 4},
        "locks": {},
    }
    n = MAX_BIAS_COMPONENTS + 1
    assign = {f"c{i}": (1 if i < 9 else 2) for i in range(n)}
    devices = {f"c{i}": "A" for i in range(n)}
    with pytest.raises(BiasError, match=str(n)):
        analyze_device_bias(data, assign, devices)


def test_sixteen_components_enumerated_exactly():
    # 16 个单例（标签0/1 各8），组1 各取4 -> C(8,4)^2 = 4900
    data = {
        "cases": [{"case_id": f"c{i}", "patient_id": f"p{i}", "label": i % 2}
                  for i in range(16)],
        "relatives": [],
        "quotas": {"g1_label0": 4, "g1_label1": 4, "g2_label0": 4, "g2_label1": 4},
        "locks": {},
    }
    assign = {f"c{i}": (1 if i < 8 else 2) for i in range(16)}
    devices = {f"c{i}": ("A" if i % 2 else "B") for i in range(16)}
    r = analyze_device_bias(data, assign, devices)
    assert r["legal_split_total"] == 4900
    assert 1 <= r["rare_count"] <= 4900


def test_should_cancel_propagates_and_aborts():
    data = {
        "cases": [{"case_id": f"c{i}", "patient_id": f"p{i}", "label": i % 2}
                  for i in range(16)],
        "relatives": [],
        "quotas": {"g1_label0": 4, "g1_label1": 4, "g2_label0": 4, "g2_label1": 4},
        "locks": {},
    }
    assign = {f"c{i}": (1 if i < 8 else 2) for i in range(16)}
    devices = {f"c{i}": "A" for i in range(16)}

    class Stale(Exception):
        pass

    def cancel():
        raise Stale("manifest invalidated during computation")

    with pytest.raises(Stale):
        analyze_device_bias(data, assign, devices, should_cancel=cancel)


# --------------------------------------------------------------------------- #
# 设备 CSV 解析校验
# --------------------------------------------------------------------------- #
KNOWN = ["c1", "c2", "c3"]


def test_parse_devices_csv_ok_and_uppercase():
    raw = "病例号,设备型号\nc1,a\nc2,B\nc3,A\n".encode()
    assert parse_devices_csv(raw, KNOWN) == {"c1": "A", "c2": "B", "c3": "A"}


def test_parse_devices_csv_rejects_duplicate_missing_unknown_bad_model():
    bad = "病例号,设备型号\nc1,A\nc1,B\nc2,\nc3,C\nc9,A\n".encode()
    with pytest.raises(ValidationError) as ei:
        parse_devices_csv(bad, KNOWN)
    msg = "；".join(ei.value.errors)
    assert "重复" in msg and "设备型号为空" in msg
    assert "A 或 B" in msg and "c9" in msg


def test_parse_devices_csv_rejects_incomplete_coverage():
    raw = "病例号,设备型号\nc1,A\nc2,B\n".encode()  # 缺 c3
    with pytest.raises(ValidationError, match="未覆盖全部病例"):
        parse_devices_csv(raw, KNOWN)


def test_parse_devices_csv_bad_header():
    raw = "病例号,厂家\nc1,A\n".encode()
    with pytest.raises(ValidationError, match="表头缺少列"):
        parse_devices_csv(raw, KNOWN)


def test_parse_devices_csv_row_limit():
    rows = "\n".join(f"c{i},A" for i in range(201))
    with pytest.raises(ValidationError, match="200"):
        parse_devices_csv(("病例号,设备型号\n" + rows).encode(),
                          [f"c{i}" for i in range(201)])
