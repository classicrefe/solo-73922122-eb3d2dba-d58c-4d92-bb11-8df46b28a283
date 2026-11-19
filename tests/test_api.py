"""API 全链路测试：上传、配额、锁定、求解、发布、失效、并发交错、持久化。"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("SPLIT_DB_PATH", str(db_file))
    # 延迟导入，确保环境变量生效
    import importlib
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    main.db.close()


CASES_CSV = (
    "病例号,患者号,标签\n"
    "C1,P1,1\nC2,P1,0\nC3,P2,1\nC4,P2,0\n"
    "C5,P3,1\nC6,P3,0\nC7,P4,1\nC8,P4,0\n"
)
REL_CSV = "患者A,患者B\nP1,P2\nP3,P4\n"


def _upload_cases(client):
    r = client.post("/api/upload/cases",
                    files={"file": ("cases.csv", CASES_CSV.encode("utf-8"), "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()["dataset_version"]


def _set_quotas(client, q):
    r = client.put("/api/quotas", json={
        "g1_label0": q[0], "g1_label1": q[1],
        "g2_label0": q[2], "g2_label1": q[3]})
    assert r.status_code == 200, r.text


def test_health_and_empty_state(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    state = client.get("/api/state").json()
    assert state["dataset_version"] == 0
    assert state["input"]["cases"] == []


def test_static_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "病例切分平台" in r.text
    for asset in ("/app.js", "/style.css"):
        assert client.get(asset).status_code == 200


def test_upload_validation_errors_do_not_touch_source(client):
    bad = "病例号,患者号,标签\nC1,P1,1\nC1,P2,0\n,P3,1\nC2,P4,2\n"
    r = client.post("/api/upload/cases",
                    files={"file": ("bad.csv", bad.encode(), "text/csv")})
    assert r.status_code == 400
    assert len(r.json()["errors"]) == 3  # 重复病例号、病例号空、标签非法
    # 源数据仍为空版本0
    assert client.get("/api/state").json()["dataset_version"] == 0


def test_row_limit_enforced(client):
    big = "病例号,患者号,标签\n" + "\n".join(
        f"C{i},P{i},{i % 2}" for i in range(201))
    r = client.post("/api/upload/cases",
                    files={"file": ("big.csv", big.encode(), "text/csv")})
    assert r.status_code == 400 and "200" in r.json()["errors"][0]


def test_full_solve_publish_download_flow(client):
    _upload_cases(client)
    client.post("/api/upload/relatives",
                files={"file": ("r.csv", REL_CSV.encode(), "text/csv")})
    # 四个分量? 实际两个分量(P1+P2, P3+P4)，每分量含1个标签1、1个标签0
    _set_quotas(client, (2, 2, 2, 2))
    r = client.post("/api/solve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["status"] == "sat"
    token = body["preview_token"]
    assert token

    pub = client.post("/api/publish", json={"preview_token": token})
    assert pub.status_code == 200, pub.text
    mid = pub.json()["manifest_id"]

    dl = client.get(f"/api/manifests/{mid}/download")
    assert dl.status_code == 200
    text = dl.text.lstrip("﻿")
    lines = [ln for ln in text.strip().split("\n") if ln]
    assert len(lines) == 9  # 表头 + 8 病例
    assert "训练集" in text and "评估集" in text

    # 发布清单持久化了输入依据
    detail = client.get(f"/api/manifests/{mid}").json()
    assert len(detail["input"]["cases"]) == 8
    assert detail["expired"] is False


def test_unsat_returns_mus_without_preview_token(client):
    _upload_cases(client)
    _set_quotas(client, (0, 9, 4, 0))  # 标签1总共只有4个，组1要9 -> 无解
    r = client.post("/api/solve").json()
    assert r["result"]["status"] == "unsat"
    assert r["preview_token"] is None
    assert r["result"]["mus_verified"] is True
    kinds = {c["kind"] for c in r["result"]["conflicts"]}
    assert "quota" in kinds


def test_input_change_invalidates_preview_and_manifest(client):
    v = _upload_cases(client)
    _set_quotas(client, (2, 2, 2, 2))
    token = client.post("/api/solve").json()["preview_token"]
    mid = client.post("/api/publish", json={"preview_token": token}).json()["manifest_id"]

    # 输入变动：改锁定（即使之后改回，版本也不回退）
    r = client.put("/api/locks", json={"locks": [{"case_id": "C1", "group": 2}]})
    assert r.status_code == 200
    new_version = r.json()["dataset_version"]
    assert new_version > v

    # 旧预览不能发布
    stale = client.post("/api/publish", json={"preview_token": token})
    assert stale.status_code == 409
    # 旧清单标记失效且不能下载
    detail = client.get(f"/api/manifests/{mid}").json()
    assert detail["expired"] is True and detail["invalidated"] is True
    dl = client.get(f"/api/manifests/{mid}/download")
    assert dl.status_code == 409

    # 改回原样：版本继续递增，旧清单依然失效（不恢复）
    client.put("/api/locks", json={"locks": []})
    detail2 = client.get(f"/api/manifests/{mid}").json()
    assert detail2["expired"] is True
    assert client.get(f"/api/manifests/{mid}/download").status_code == 409


def test_solve_during_mutation_is_rejected_or_rebased(client):
    """模拟“计算期间输入变动”：先求解拿快照，再改输入，再用旧令牌发布 -> 409。"""
    _upload_cases(client)
    _set_quotas(client, (2, 2, 2, 2))
    token = client.post("/api/solve").json()["preview_token"]
    client.put("/api/quotas", json={"g1_label0": 4, "g1_label1": 4,
                                    "g2_label0": 0, "g2_label1": 0})
    r = client.post("/api/publish", json={"preview_token": token})
    assert r.status_code == 409
    # 重新求解得到新预览，可正常发布
    token2 = client.post("/api/solve").json()["preview_token"]
    assert token2 != token
    assert client.post("/api/publish", json={"preview_token": token2}).status_code == 200


def test_relatives_unknown_patient_rejected(client):
    _upload_cases(client)
    bad = "患者A,患者B\nP1,P999\n"
    r = client.post("/api/upload/relatives",
                    files={"file": ("r.csv", bad.encode(), "text/csv")})
    assert r.status_code == 400
    assert "P999" in r.json()["errors"][0]


def test_reupload_cases_clears_relatives_and_stale_locks(client):
    _upload_cases(client)
    client.post("/api/upload/relatives",
                files={"file": ("r.csv", REL_CSV.encode(), "text/csv")})
    client.put("/api/locks", json={"locks": [{"case_id": "C1", "group": 1}]})
    state = client.get("/api/state").json()
    assert len(state["input"]["relatives"]) == 2
    assert state["input"]["locks"] == {"C1": 1}

    # 重新上传（病例号集合变化）
    new_csv = CASES_CSV.replace("C1,P1,1", "C1,P1,1").replace("C8,P4,0", "C9,P9,0\n")
    r = client.post("/api/upload/cases",
                    files={"file": ("cases2.csv", new_csv.encode(), "text/csv")})
    assert r.status_code == 200
    state = client.get("/api/state").json()
    assert state["input"]["relatives"] == []
    assert "C8" not in state["input"]["locks"]


def test_persistence_across_restart(tmp_path, monkeypatch):
    db_file = tmp_path / "persist.db"
    monkeypatch.setenv("SPLIT_DB_PATH", str(db_file))
    import importlib
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c1:
        _upload_cases(c1)
        _set_quotas(c1, (2, 2, 2, 2))
        token = c1.post("/api/solve").json()["preview_token"]
        mid = c1.post("/api/publish", json={"preview_token": token}).json()["manifest_id"]
    main.db.close()

    # 重启：内存预览清空，但已发布清单与输入依据仍在
    importlib.reload(main)
    with TestClient(main.app) as c2:
        state = c2.get("/api/state").json()
        assert len(state["input"]["cases"]) == 8
        manifests = c2.get("/api/manifests").json()["manifests"]
        assert len(manifests) == 1 and manifests[0]["manifest_id"] == mid
        assert c2.get(f"/api/manifests/{mid}/download").status_code == 200
        # 旧预览令牌在重启后不可发布（令牌不在内存）
        r = c2.post("/api/publish", json={"preview_token": token})
        assert r.status_code == 409
    main.db.close()


def test_concurrent_mutations_keep_consistent_versions(client):
    _upload_cases(client)
    import concurrent.futures

    def mutate(i):
        return client.put("/api/locks",
                          json={"locks": [{"case_id": f"C{(i % 8) + 1}", "group": 1}]}).json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(mutate, range(16)))
    versions = sorted(r["dataset_version"] for r in results)
    # 16 次写全部成功且版本严格唯一递增（无丢失更新）
    assert versions == list(range(2, 18))
    assert client.get("/api/health").json()["version"] == 17


def test_lock_violation_unsat_lists_lock_and_quota(client):
    _upload_cases(client)
    client.put("/api/locks", json={"locks": [{"case_id": "C1", "group": 1}]})
    # C1 标签=1，组1标签1配额为0 -> 冲突；其余配额保持总量精确
    _set_quotas(client, (4, 0, 0, 4))
    r = client.post("/api/solve").json()
    assert r["result"]["status"] == "unsat"
    kinds = {c["kind"] for c in r["result"]["conflicts"]}
    assert "lock" in kinds and "quota" in kinds


# --------------------------------------------------------------------------- #
# 设备型号偏差罕见度分析
# --------------------------------------------------------------------------- #
def _publish_a_manifest(client):
    _upload_cases(client)
    client.post("/api/upload/relatives",
                files={"file": ("r.csv", REL_CSV.encode("utf-8"))})
    _set_quotas(client, (2, 2, 2, 2))
    token = client.post("/api/solve").json()["preview_token"]
    return client.post("/api/publish", json={"preview_token": token}).json()


def _device_csv(mapping):
    lines = ["病例号,设备型号"]
    lines += [f"{cid},{model}" for cid, model in mapping.items()]
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_manifest_list_carries_bias_eligibility(client):
    m = _publish_a_manifest(client)
    state = client.get("/api/state").json()
    row = next(x for x in state["manifests"] if x["manifest_id"] == m["manifest_id"])
    # 两个亲属连通分量（P1+P2、P3+P4），<=16，且清单有效
    assert row["component_count"] == 2
    assert row["bias_eligible"] is True
    assert row["bias_component_limit"] == 16


def test_device_bias_full_flow_and_read_only(client):
    m = _publish_a_manifest(client)
    mid = m["manifest_id"]
    version_before = client.get("/api/health").json()["version"]
    devices = {f"C{i}": ("A" if i % 2 else "B") for i in range(1, 9)}
    r = client.post(
        f"/api/manifests/{mid}/device-bias",
        files={"file": ("devices.csv", _device_csv(devices), "text/csv")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    res = body["result"]
    # 分析是只读操作：输入版本不变
    assert client.get("/api/health").json()["version"] == version_before
    assert body["device_row_count"] == 8
    assert res["legal_split_total"] >= 1
    assert 1 <= res["rare_count"] <= res["legal_split_total"]
    assert res["rare_ratio"]["numerator"] / res["rare_ratio"]["denominator"] <= 1
    # 精确分数字段齐全
    for key in ("current_bias", "current_bias_abs", "rare_ratio",
                "device_a_proportions"):
        assert key in res
    # 分量与清单依据一致
    assert res["component_count"] == 2
    # 当前清单本身是合法切分，必然计入罕见数
    assert res["rare_count"] >= 1


def test_device_bias_rejects_bad_csv_without_touching_data(client):
    m = _publish_a_manifest(client)
    mid = m["manifest_id"]
    version_before = client.get("/api/health").json()["version"]
    # 重复病例号 + 未知型号 + 缺失病例
    bad = "病例号,设备型号\nC1,A\nC1,B\nC2,C\n".encode()
    r = client.post(
        f"/api/manifests/{mid}/device-bias",
        files={"file": ("bad.csv", bad, "text/csv")},
    )
    assert r.status_code == 400
    joined = "；".join(r.json()["errors"])
    assert "重复" in joined and "A 或 B" in joined
    # 覆盖不全（缺 C3..C8）单独拒绝
    incomplete = _device_csv({f"C{i}": "A" for i in range(1, 5)})
    r2 = client.post(
        f"/api/manifests/{mid}/device-bias",
        files={"file": ("incomplete.csv", incomplete, "text/csv")},
    )
    assert r2.status_code == 400 and "未覆盖全部病例" in r2.json()["errors"][0]
    # 失败不改变版本、不影响清单有效性
    assert client.get("/api/health").json()["version"] == version_before
    detail = client.get(f"/api/manifests/{mid}").json()
    assert detail["expired"] is False


def test_device_bias_unknown_case_rejected(client):
    m = _publish_a_manifest(client)
    devices = {f"C{i}": "A" for i in range(1, 9)}
    devices["C999"] = "B"
    r = client.post(
        f"/api/manifests/{m['manifest_id']}/device-bias",
        files={"file": ("d.csv", _device_csv(devices), "text/csv")},
    )
    assert r.status_code == 400 and "C999" in r.json()["errors"][0]


def test_device_bias_on_invalidated_manifest_returns_409(client):
    m = _publish_a_manifest(client)
    mid = m["manifest_id"]
    # 输入变动使清单失效
    client.put("/api/locks", json={"locks": [{"case_id": "C1", "group": 2}]})
    devices = {f"C{i}": "A" for i in range(1, 9)}
    r = client.post(
        f"/api/manifests/{mid}/device-bias",
        files={"file": ("d.csv", _device_csv(devices), "text/csv")},
    )
    assert r.status_code == 409 and "失效" in r.json()["detail"]


def test_device_bias_manifest_not_found(client):
    _upload_cases(client)
    r = client.post(
        "/api/manifests/nonexistent/device-bias",
        files={"file": ("d.csv", _device_csv({"C1": "A"}), "text/csv")},
    )
    assert r.status_code == 404


def test_device_bias_rejected_when_invalidated_during_computation(client, monkeypatch):
    m = _publish_a_manifest(client)
    mid = m["manifest_id"]

    # 让枚举过程中的失效检查观察到“清单已失效”，模拟计算期间输入变动
    import app.main as main
    original = main.db.is_manifest_current

    def flaky(manifest_id):
        if manifest_id == mid:
            return False
        return original(manifest_id)

    monkeypatch.setattr(main.db, "is_manifest_current", flaky)
    devices = {f"C{i}": "A" for i in range(1, 9)}
    r = client.post(
        f"/api/manifests/{mid}/device-bias",
        files={"file": ("d.csv", _device_csv(devices), "text/csv")},
    )
    assert r.status_code == 409
    assert "计算期间" in r.json()["detail"] and "作废" in r.json()["detail"]
    # 输入与清单未被分析修改
    assert client.get(f"/api/manifests/{mid}").json()["invalidated"] is False


def test_device_bias_component_limit_enforced(client):
    # 构造 17 个互不相连的单病例分量并发布
    cases = "病例号,患者号,标签\n" + "\n".join(
        f"C{i:03d},P{i:03d},{i % 2}" for i in range(1, 18))
    r = client.post("/api/upload/cases",
                    files={"file": ("big.csv", cases.encode(), "text/csv")})
    assert r.status_code == 200
    client.put("/api/quotas", json={
        "g1_label0": 4, "g1_label1": 5, "g2_label0": 4, "g2_label1": 4})
    solved = client.post("/api/solve").json()
    assert solved["result"]["status"] == "sat"
    mid = client.post("/api/publish",
                      json={"preview_token": solved["preview_token"]}).json()["manifest_id"]
    state = client.get("/api/state").json()
    row = next(x for x in state["manifests"] if x["manifest_id"] == mid)
    assert row["component_count"] == 17 and row["bias_eligible"] is False
    devices = {f"C{i:03d}": "A" for i in range(1, 18)}
    r2 = client.post(
        f"/api/manifests/{mid}/device-bias",
        files={"file": ("d.csv", _device_csv(devices), "text/csv")},
    )
    assert r2.status_code == 400 and "17" in r2.json()["detail"]
