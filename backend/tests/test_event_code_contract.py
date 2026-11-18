"""event_code_contract 门禁测试：分流、版本化契约、失败闭、证明同源。"""

import importlib
import json
import sys

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """每用例一套独立 DATA_DIR / FIXTURES_DIR，并重载 app.* 让 db.py 重新读环境变量。"""
    data_dir = tmp_path / "data"
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("FIXTURES_DIR", str(fixtures_dir))
    for name in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        del sys.modules[name]
    import app.seed as seed

    seed.init_db()

    import app.services.ingest as ingest
    import app.modules.event_code_contract as ecc
    from app.db import connect

    def write_fixture(payload):
        (fixtures_dir / "ingest_batch.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    return ingest, ecc, connect, write_fixture


def _batch(batch_id="B-T1", codes=("PICKED", "LOST_PACKAGE", "TELEPORT")):
    hops = []
    for i, code in enumerate(codes, 1):
        hops.append(
            {
                "id": f"H-{batch_id}-{i}",
                "waybill_id": f"WB-{batch_id}",
                "waybill_title": "测试件",
                "event_ts": f"2026-11-18T0{i}:00:00Z",
                "location_code": "T-HUB",
                "event_code": code,
                "seq": i,
            }
        )
    return {"batch_id": batch_id, "cutoff_ts": "2026-11-18T09:00:00Z", "hops": hops}


def test_split_legal_to_primary_illegal_to_quarantine(env):
    ingest, ecc, connect, write_fixture = env
    write_fixture(_batch())
    res = ingest.run_batch()
    assert res["ok"], res
    rep = res["report"]
    assert (rep["input"], rep["written"], rep["violations"], rep["quarantined"]) == (3, 1, 2, 2)
    assert rep["contract_version"] == 1
    assert rep["violation_codes"] == ["LOST_PACKAGE", "TELEPORT"]

    conn = connect()
    try:
        primary = conn.execute(
            "SELECT event_code FROM hops WHERE batch_id='B-T1' AND channel='primary'"
        ).fetchall()
        assert [r["event_code"] for r in primary] == ["PICKED"]  # 违规点零残留写入正式线
        quars = conn.execute(
            "SELECT reason, member_json, batch_id FROM quarantine WHERE batch_id='B-T1'"
        ).fetchall()
        assert len(quars) == 2
        for q in quars:
            assert q["reason"] == ecc.REASON
            member = json.loads(q["member_json"])
            assert member["contract_version"] == 1
            assert "契约" in member["violation"]  # 隔离成员写明契约违规
        # 投影只来自合法点
        wb = conn.execute("SELECT status_proj FROM waybills WHERE id='WB-B-T1'").fetchone()
        assert wb["status_proj"] == "已揽收"
    finally:
        conn.close()


def test_contract_add_remove_produces_versions(env):
    _, ecc, connect, _ = env
    conn = connect()
    try:
        v1 = ecc.current_contract(conn)["version"]
        v2 = ecc.add_code(conn, "lost_package", "遗失")  # 归一化为大写
        assert v2 > v1
        assert ecc.get_contract(conn, v2)["items"]["LOST_PACKAGE"] == "遗失"
        assert "LOST_PACKAGE" not in ecc.get_contract(conn, v1)["items"]  # 旧版本快照不可变
        v3 = ecc.remove_code(conn, "LOST_PACKAGE")
        assert "LOST_PACKAGE" not in ecc.get_contract(conn, v3)["items"]
        with pytest.raises(ecc.ContractError):
            ecc.remove_code(conn, "NO_SUCH_CODE")
        with pytest.raises(ecc.ContractError):
            ecc.add_code(conn, "bad code!", "x")
    finally:
        conn.close()


def test_replay_does_not_duplicate_quarantine(env):
    ingest, ecc, connect, write_fixture = env
    write_fixture(_batch("B-RP"))
    ingest.run_batch()
    res2 = ingest.run_batch()
    assert res2["ok"]
    assert res2["report"]["written"] == 0
    assert res2["report"]["quarantined"] == 0
    assert res2["report"]["violations"] == 2  # 门禁口径不变：违规仍逐点判出
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine WHERE batch_id='B-RP'"
        ).fetchone()["c"]
        assert n == 2
    finally:
        conn.close()


def test_fail_closed_without_contract(env):
    ingest, ecc, connect, write_fixture = env
    conn = connect()
    try:
        conn.execute("DELETE FROM contract_items")
        conn.execute("DELETE FROM contract_versions")
        conn.commit()
    finally:
        conn.close()
    write_fixture(_batch("B-FC", codes=("PICKED",)))
    res = ingest.run_batch()
    assert not res["ok"]  # 无契约 → 失败闭，整批回滚
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM hops WHERE batch_id='B-FC'").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM quarantine WHERE batch_id='B-FC'").fetchone()["c"] == 0
    finally:
        conn.close()


def test_empty_contract_quarantines_everything(env):
    ingest, ecc, connect, write_fixture = env
    conn = connect()
    try:
        for code in list(ecc.current_contract(conn)["items"]):
            ecc.remove_code(conn, code)  # 删到空契约版本（仍存在 → 全不允许）
        conn.commit()
    finally:
        conn.close()
    write_fixture(_batch("B-EC", codes=("PICKED",)))
    res = ingest.run_batch()
    assert res["ok"]
    assert res["report"]["written"] == 0
    assert res["report"]["quarantined"] == 1


def test_contract_change_does_not_rewrite_history(env):
    ingest, ecc, connect, write_fixture = env
    write_fixture(_batch("B-HIST", codes=("PICKED",)))
    ingest.run_batch()
    conn = connect()
    try:
        ecc.remove_code(conn, "PICKED")  # 改契约只产生新版本
        conn.commit()
        hop = conn.execute(
            "SELECT channel, retracted FROM hops WHERE id='H-B-HIST-1'"
        ).fetchone()
        assert hop["channel"] == "primary" and hop["retracted"] == 0  # 历史正式点不动
        wb = conn.execute("SELECT status_proj FROM waybills WHERE id='WB-B-HIST'").fetchone()
        assert wb["status_proj"] == "已揽收"  # 历史投影不回溯改写
    finally:
        conn.close()


def test_proof_same_source_with_tables(env):
    ingest, ecc, connect, write_fixture = env
    write_fixture(_batch("B-PF"))
    ingest.run_batch()
    conn = connect()
    try:
        proof = ecc.proof_for_batch(conn, "B-PF")
        assert proof is not None
        assert proof["contract_version"] == 1
        assert set(proof["contract_items"]) >= {"PICKED"}  # 契约版本快照随证明出具
        assert proof["live_counts"] == {"written": 1, "violations": 2, "quarantined": 2}
        assert len(proof["hops"]) == 1 and len(proof["quarantine"]) == 2
        assert proof["report"]["violations"] == 2
        assert ecc.proof_for_batch(conn, "B-NONE") is None
    finally:
        conn.close()


def test_new_code_allows_future_batches_only(env):
    ingest, ecc, connect, write_fixture = env
    write_fixture(_batch("B-V1", codes=("LOST_PACKAGE",)))
    r1 = ingest.run_batch()
    assert r1["report"]["quarantined"] == 1  # v1 契约下违规
    conn = connect()
    try:
        ecc.add_code(conn, "LOST_PACKAGE", "遗失")
        conn.commit()
    finally:
        conn.close()
    write_fixture(_batch("B-V2", codes=("LOST_PACKAGE",)))
    r2 = ingest.run_batch()
    assert r2["report"]["written"] == 1  # 新契约版本下合法
    assert r2["report"]["contract_version"] == 2
    conn = connect()
    try:
        # 旧批次证明仍按其装载时 v1 快照出具
        p1 = ecc.proof_for_batch(conn, "B-V1")
        assert p1["contract_version"] == 1
        assert "LOST_PACKAGE" not in p1["contract_items"]
    finally:
        conn.close()


def test_routes(env):
    ingest, ecc, connect, write_fixture = env
    from fastapi.testclient import TestClient

    import app.main as main

    client = TestClient(main.app)
    c = client.get("/api/contract").json()
    assert c["current"]["version"] == 1
    assert "PICKED" in c["current"]["items"]

    r = client.post("/api/contract/codes", json={"event_code": "LOST_PACKAGE", "proj_text": "遗失"})
    assert r.status_code == 200 and r.json()["version"] == 2
    r = client.delete("/api/contract/codes/LOST_PACKAGE")
    assert r.status_code == 200 and r.json()["version"] == 3
    assert client.delete("/api/contract/codes/LOST_PACKAGE").status_code == 404
    assert client.post("/api/contract/codes", json={"event_code": "bad code", "proj_text": "x"}).status_code == 400

    assert client.get("/api/contract/proof", params={"batch_id": "B-NONE"}).status_code == 404
    write_fixture(_batch("B-API"))
    r = client.post("/api/ingest/run")
    assert r.status_code == 200 and r.json()["ok"]
    p = client.get("/api/contract/proof", params={"batch_id": "B-API"}).json()
    assert p["contract_version"] == 3
    assert p["live_counts"] == {"written": 1, "violations": 2, "quarantined": 2}
    q = client.get("/api/quarantine").json()
    assert any(row["batch_id"] == "B-API" for row in q)


# ---------------------------------------------------------------- 分流对照证明

def test_split_never_reverses_sides(env):
    """回归：登记码必须进正式线，未登记码必须进隔离（曾经的返回顺序反了）。"""
    ingest, ecc, connect, write_fixture = env
    write_fixture(_batch("B-REV", codes=("PICKED", "TELEPORT")))
    res = ingest.run_batch()
    assert res["ok"], res
    conn = connect()
    try:
        primary = conn.execute(
            "SELECT event_code FROM hops WHERE batch_id='B-REV' AND channel='primary'"
        ).fetchall()
        assert [r["event_code"] for r in primary] == ["PICKED"]
        quarantined = conn.execute(
            "SELECT member_json FROM quarantine WHERE batch_id='B-REV' AND reason=?",
            (ecc.REASON,),
        ).fetchall()
        assert [json.loads(r["member_json"])["event_code"] for r in quarantined] == ["TELEPORT"]
        # 半入：未登记码在正式线任何形态都不允许
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM hops WHERE batch_id='B-REV' AND event_code='TELEPORT'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_split_proof_binds_version_and_both_sides(env):
    ingest, ecc, connect, write_fixture = env
    write_fixture(_batch("B-SP", codes=("PICKED", "LOST_PACKAGE")))
    assert ingest.run_batch()["ok"]
    conn = connect()
    try:
        proof = ecc.verify_batch_split(conn, "B-SP")
        assert proof["ok"] is True
        assert proof["contract_version"] == 1 and proof["split_version"] == 1
        assert proof["counts"] == {"legal": 1, "violations": 1}
        assert [m["event_code"] for m in proof["legal"]] == ["PICKED"]
        assert [m["event_code"] for m in proof["violations"]] == ["LOST_PACKAGE"]
        # 证明随批次固定：契约改版后旧批次版本不漂移
        ecc.add_code(conn, "LOST_PACKAGE", "遗失")
        conn.commit()
        assert ecc.verify_batch_split(conn, "B-SP")["contract_version"] == 1
    finally:
        conn.close()


def test_verify_fails_on_version_mismatch(env):
    ingest, ecc, connect, write_fixture = env
    write_fixture(_batch("B-VM", codes=("PICKED", "TELEPORT")))
    assert ingest.run_batch()["ok"]
    conn = connect()
    try:
        bad = ecc.verify_batch_split(conn, "B-VM", expected_version=99)
        assert bad["ok"] is False
        assert {d["code"] for d in bad["discrepancies"]} == {"expected_version_mismatch"}
    finally:
        conn.close()


def test_verify_detects_cross_side_leakage(env):
    ingest, ecc, connect, write_fixture = env
    write_fixture(_batch("B-LK", codes=("PICKED", "TELEPORT")))
    assert ingest.run_batch()["ok"]
    conn = connect()
    try:
        # 未登记码半入正式线（同一隔离点又出现在正式线）
        conn.execute(
            """
            INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel, batch_id)
            VALUES ('H-B-LK-2', 'WB-B-LK', '2026-11-18T08:00:00Z', 'T-HUB', 'TELEPORT', 9, 'primary', 'B-LK')
            """
        )
        conn.commit()
        check = ecc.verify_batch_split(conn, "B-LK")
        assert check["ok"] is False
        codes = {d["code"] for d in check["discrepancies"]}
        assert "unregistered_code_in_primary" in codes
        assert "violation_member_half_in_primary" in codes
        # 登记码进隔离区
        conn.execute(
            "DELETE FROM hops WHERE id='H-B-LK-2'"
        )
        ecc.quarantine_violation(
            conn, "B-LK", ecc.current_contract(conn),
            {"id": "H-P2", "waybill_id": "WB-B-LK", "event_code": "PICKED"}, "手工误置",
        )
        conn.commit()
        check2 = ecc.verify_batch_split(conn, "B-LK")
        assert check2["ok"] is False
        assert "registered_code_in_quarantine" in {d["code"] for d in check2["discrepancies"]}
    finally:
        conn.close()


def test_replay_pins_version_and_proof_is_immutable(env):
    ingest, ecc, connect, write_fixture = env
    write_fixture(_batch("B-PIN", codes=("PICKED", "TELEPORT")))
    assert ingest.run_batch()["ok"]
    conn = connect()
    try:
        ecc.add_code(conn, "TELEPORT", "瞬移")  # 契约改版 → 新版本
        conn.commit()
    finally:
        conn.close()
    res = ingest.run_batch()  # 同批次重放必须沿用 v1，不因新版本翻案
    assert res["ok"], res
    assert res["report"]["contract_version"] == 1
    assert res["report"]["pinned"] is True
    assert res["report"]["written"] == 0
    assert res["report"]["quarantined"] == 0
    conn = connect()
    try:
        assert ecc.pinned_version(conn, "B-PIN") == 1
        assert ecc.verify_batch_split(conn, "B-PIN")["contract_version"] == 1
    finally:
        conn.close()


def test_replay_with_changed_members_fails_closed(env):
    ingest, ecc, connect, write_fixture = env
    write_fixture(_batch("B-MUT", codes=("PICKED",)))
    assert ingest.run_batch()["ok"]
    write_fixture(_batch("B-MUT", codes=("PICKED", "TELEPORT")))  # 同批次号换了成员
    res = ingest.run_batch()
    assert res["ok"] is False
    assert "分流对照证明已固定" in res["error"]
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM hops WHERE batch_id='B-MUT' AND event_code='TELEPORT'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_split_proof_endpoint(env):
    ingest, ecc, connect, write_fixture = env
    from fastapi.testclient import TestClient

    import app.main as main

    client = TestClient(main.app)
    assert client.get("/api/contract/split-proof", params={"batch_id": "B-NONE"}).status_code == 404
    write_fixture(_batch("B-SE", codes=("PICKED", "LOST_PACKAGE", "TELEPORT")))
    assert client.post("/api/ingest/run").json()["ok"]
    r = client.get("/api/contract/split-proof", params={"batch_id": "B-SE"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["contract_version"] == 1
    assert body["counts"] == {"legal": 1, "violations": 2}
    # 当次版本不一致 → 核证不通过
    bad = client.get(
        "/api/contract/split-proof", params={"batch_id": "B-SE", "contract_version": 2}
    ).json()
    assert bad["ok"] is False
    assert {d["code"] for d in bad["discrepancies"]} == {"expected_version_mismatch"}
    # 批次证明里也带分流对照
    p = client.get("/api/contract/proof", params={"batch_id": "B-SE"}).json()
    assert p["split_proof"]["ok"] is True


def test_repository_mixed_fixture_end_to_end(env):
    """对板夹具：同批混合登记码与未登记码，跑通对板（正式线 / 隔离 / 核证三方一致）。"""
    import shutil
    from pathlib import Path

    ingest, ecc, connect, _ = env
    from app.db import FIXTURES_DIR

    repo_fixture = Path(__file__).resolve().parents[2] / "fixtures" / "ingest_batch.json"
    shutil.copyfile(repo_fixture, FIXTURES_DIR / "ingest_batch.json")
    res = ingest.run_batch()
    assert res["ok"], res
    rep = res["report"]
    assert rep["batch_id"] == "B-DEMO-01"
    assert (rep["input"], rep["written"], rep["violations"], rep["quarantined"]) == (4, 2, 2, 2)
    assert rep["violation_codes"] == ["LOST_PACKAGE", "TELEPORT"]
    assert rep["split_proof_ok"] is True

    conn = connect()
    try:
        # 登记码进正式线
        primary = {
            r["event_code"]
            for r in conn.execute(
                "SELECT event_code FROM hops WHERE batch_id='B-DEMO-01' AND channel='primary'"
            ).fetchall()
        }
        assert primary == {"OUT_FOR_DELIVERY"}
        # 未登记码只在隔离区
        q = {
            json.loads(r["member_json"])["event_code"]
            for r in conn.execute(
                "SELECT member_json FROM quarantine WHERE batch_id='B-DEMO-01'"
            ).fetchall()
        }
        assert q == {"LOST_PACKAGE", "TELEPORT"}
        # 核证三方一致
        proof = ecc.verify_batch_split(conn, "B-DEMO-01")
        assert proof["ok"] is True
        assert proof["counts"] == {"legal": 2, "violations": 2}
        # 混入件的正式线只含登记码，未登记码零残留
        wb2 = [
            r["event_code"]
            for r in conn.execute(
                "SELECT event_code FROM hops WHERE waybill_id='WB-1002' AND batch_id='B-DEMO-01' "
                "AND channel='primary' AND retracted=0 ORDER BY event_ts"
            ).fetchall()
        ]
        assert wb2 == ["OUT_FOR_DELIVERY"]
    finally:
        conn.close()


