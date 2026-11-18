from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import connect
from app.modules import event_code_contract as ecc

router = APIRouter(prefix="/api/contract", tags=["contract"])


class CodeIn(BaseModel):
    event_code: str = Field(min_length=1, max_length=64)
    proj_text: str = Field(min_length=1, max_length=50)


@router.get("")
def get_contract():
    """当前契约（版本 + 允许码 → 投影文案映射）与版本历史。"""
    conn = connect()
    try:
        ecc.ensure_tables(conn)
        ecc.seed_default(conn)
        conn.commit()
        return {"current": ecc.current_contract(conn), "versions": ecc.list_versions(conn)}
    finally:
        conn.close()


@router.post("/codes")
def add_code(body: CodeIn):
    """新增/更新允许码：产生新契约版本，旧版本快照不动。"""
    conn = connect()
    try:
        ecc.ensure_tables(conn)
        ecc.seed_default(conn)
        conn.execute("BEGIN IMMEDIATE")
        version = ecc.add_code(conn, body.event_code, body.proj_text)
        conn.commit()
        return {"ok": True, "version": version, "current": ecc.current_contract(conn)}
    except ecc.ContractError as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/codes/{event_code}")
def remove_code(event_code: str):
    """删除允许码：产生新契约版本；历史正式点不回溯改写。"""
    conn = connect()
    try:
        ecc.ensure_tables(conn)
        ecc.seed_default(conn)
        conn.execute("BEGIN IMMEDIATE")
        version = ecc.remove_code(conn, event_code)
        conn.commit()
        return {"ok": True, "version": version, "current": ecc.current_contract(conn)}
    except ecc.ContractError as e:
        conn.rollback()
        raise HTTPException(404, str(e))
    finally:
        conn.close()


@router.get("/proof")
def contract_proof(batch_id: str):
    """按批次查询契约证明（只读）：计数与契约版本快照、隔离列表、正式时间线同源。"""
    conn = connect()
    try:
        ecc.ensure_tables(conn)
        proof = ecc.proof_for_batch(conn, batch_id)
        if proof is None:
            raise HTTPException(404, "批次不存在")
        return proof
    finally:
        conn.close()
