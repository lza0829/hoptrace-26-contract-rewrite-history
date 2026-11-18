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
