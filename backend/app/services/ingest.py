import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules import event_code_contract as ecc


def run_batch() -> dict:
    """装载入口门禁：按当前契约版本逐点分流。

    契约内合法点 → 正式时间线(hops primary) + 契约投影文案；
    不在契约内的点 → 隔离区（写明契约违规），绝不半写入正式线。
    失败闭：无可用契约版本 → 整批回滚报错；任何异常 → 回滚，不留半批。
    """
    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return {"ok": False, "error": "缺少 ingest_batch.json"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    cutoff = payload.get("cutoff_ts")
    hops = payload.get("hops", [])

    conn = connect()
    try:
        ecc.ensure_tables(conn)  # 幂等；在事务外执行，避免 DDL 隐式提交
        conn.execute("BEGIN IMMEDIATE")
        contract = ecc.current_contract(conn)  # 无契约 → ContractError → 回滚（失败闭）
        legal, violations = ecc.split(contract, hops)

        written = 0
        for hop, proj in legal:
            _ensure_waybill(conn, hop)
            if _insert_primary(conn, batch_id, hop):
                written += 1
                _project(conn, hop, proj)

        quarantined = 0
        for hop, detail in violations:
            _ensure_waybill(conn, hop)
            if ecc.quarantine_violation(conn, batch_id, contract, hop, detail):
                quarantined += 1

        report = {
            "batch_id": batch_id,
            "cutoff_ts": cutoff,
            "contract_version": contract["version"],
            "input": len(hops),
            "written": written,
            "violations": len(violations),
            "quarantined": quarantined,
            "violation_codes": sorted({(h.get("event_code") or "") for h, _ in violations}),
        }
        conn.execute(
            """
            INSERT OR REPLACE INTO ingest_batches(id, cutoff_ts, created_at, report_json, contract_version)
            VALUES (?,?,?,?,?)
            """,
            (batch_id, cutoff, _now(), json.dumps(report, ensure_ascii=False), contract["version"]),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _ensure_waybill(conn, hop: dict) -> None:
    wid = hop["waybill_id"]
    exists = conn.execute("SELECT 1 FROM waybills WHERE id=?", (wid,)).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
            (wid, hop.get("waybill_title") or wid, "未知", _now()),
        )


def _insert_primary(conn, batch_id: str, hop: dict) -> bool:
    conn.execute(
        """
        INSERT OR IGNORE INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel, batch_id)
        VALUES (?,?,?,?,?,?, 'primary', ?)
        """,
        (
            hop["id"],
            hop["waybill_id"],
            hop["event_ts"],
            hop["location_code"],
            hop["event_code"],
            hop.get("seq"),
            batch_id,
        ),
    )
    return conn.execute("SELECT changes()").fetchone()[0] == 1


def _project(conn, hop: dict, proj: str) -> None:
    wid = hop["waybill_id"]
    conn.execute("UPDATE waybills SET status_proj=? WHERE id=?", (proj, wid))
    conn.execute(
        "INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at) VALUES (?,?,?,?,?)",
        (wid, hop["id"], hop["event_code"], proj, _now()),
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
