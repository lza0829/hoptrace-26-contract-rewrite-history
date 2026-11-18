"""event_code_contract — 事件码契约门禁。

落库形态（两种择一，本题选「契约表 + 隔离表」）：
  contract_versions / contract_items  允许 event_code 契约（含到投影文案的映射），
                                      按版本保存、版本不可变，页面可增删（增删即产生新版本）；
  quarantine                          契约违规点隔离区，reason=event_code_contract_violation，
                                      member_json 写明契约违规细节与契约版本。

门禁路径（两种择一，本题选「装载入口按当前契约版本逐点分流」）：
  装载时逐点对照当前契约版本：契约内 → 正式时间线(hops primary)；
  不在契约内 → 隔离并写明契约违规，绝不半写入正式线。
  失败闭：无可用契约版本 → 整批失败回滚；空契约版本 → 全部按违规隔离。
  改契约只产生新版本，不回溯改写历史正式点；历史批次的证明按其装载时契约版本快照出具。
"""

import json
import re
from datetime import datetime, timezone

REASON = "event_code_contract_violation"

# 首次启动的自举默认契约（仅用于生成契约 v1）；之后契约以数据库为准，页面可增删。
DEFAULT_CONTRACT = {
    "PICKED": "已揽收",
    "IN_TRANSIT": "运输中",
    "ARRIVED_HUB": "到站",
    "OUT_FOR_DELIVERY": "派送中",
    "DELIVERED": "已签收",
    "EXCEPTION": "异常",
}

_CODE_RE = re.compile(r"^[A-Z0-9_]{1,64}$")

_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS contract_versions (
      version INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at TEXT NOT NULL,
      note TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS contract_items (
      version INTEGER NOT NULL REFERENCES contract_versions(version),
      event_code TEXT NOT NULL,
      proj_text TEXT NOT NULL,
      PRIMARY KEY (version, event_code)
    )
    """,
    # 同一违规点重复装载（夹具重放）不重复占隔离位。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_quarantine_reason_member
    ON quarantine(reason, member_json)
    """,
]


class ContractError(Exception):
    """契约不可用或契约编辑冲突；装载入口捕获后整批回滚（失败闭）。"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 落库

def ensure_tables(conn) -> None:
    """建契约表，并给既有表补门禁列（幂等，兼容旧库；不用 executescript 以免隐式提交）。"""
    for stmt in _DDL_STATEMENTS:
        conn.execute(stmt)
    _ensure_column(conn, "hops", "batch_id", "batch_id TEXT")
    _ensure_column(conn, "quarantine", "batch_id", "batch_id TEXT")
    _ensure_column(conn, "ingest_batches", "contract_version", "contract_version INTEGER")


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def seed_default(conn) -> None:
    """无任何契约版本时写入自举 v1；之后契约完全由库内版本决定。"""
    n = conn.execute("SELECT COUNT(*) AS c FROM contract_versions").fetchone()["c"]
    if n == 0:
        _insert_version(conn, DEFAULT_CONTRACT, "自举默认契约")


def _insert_version(conn, items: dict, note: str) -> int:
    cur = conn.execute(
        "INSERT INTO contract_versions(created_at, note) VALUES (?,?)", (_now(), note)
    )
    version = cur.lastrowid
    for code, proj in items.items():
        conn.execute(
            "INSERT INTO contract_items(version, event_code, proj_text) VALUES (?,?,?)",
            (version, code, proj),
        )
    return version


# ---------------------------------------------------------------- 契约读写

def current_contract(conn) -> dict:
    """当前（最大版本）契约；一个版本都没有 → ContractError（失败闭）。"""
    row = conn.execute(
        "SELECT version FROM contract_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise ContractError("事件码契约未初始化，装载门禁失败闭")
    return get_contract(conn, row["version"])


def get_contract(conn, version: int) -> dict:
    """指定版本契约快照；版本存在但无条目 = 空契约（全不允许，失败闭）。"""
    exists = conn.execute(
        "SELECT 1 FROM contract_versions WHERE version=?", (version,)
    ).fetchone()
    if not exists:
        raise ContractError(f"契约版本不存在: {version}")
    rows = conn.execute(
        "SELECT event_code, proj_text FROM contract_items WHERE version=? ORDER BY event_code",
        (version,),
    ).fetchall()
    return {"version": version, "items": {r["event_code"]: r["proj_text"] for r in rows}}


def list_versions(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT v.version, v.created_at, v.note, COUNT(i.event_code) AS codes
        FROM contract_versions v
        LEFT JOIN contract_items i ON i.version = v.version
        GROUP BY v.version
        ORDER BY v.version DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def add_code(conn, event_code: str, proj_text: str) -> int:
    """新增/更新允许码：以当前契约为底产生新版本，旧版本快照不动。"""
    contract = current_contract(conn)
    items = dict(contract["items"])
    code = _validate_code(event_code)
    items[code] = _validate_proj(proj_text)
    return _insert_version(conn, items, f"新增/更新允许码 {code}")


def remove_code(conn, event_code: str) -> int:
    """删除允许码：以当前契约为底产生新版本；不存在的码报错。"""
    contract = current_contract(conn)
    items = dict(contract["items"])
    code = (event_code or "").strip().upper()
    if code not in items:
        raise ContractError(f"契约内不存在事件码: {code}")
    del items[code]
    return _insert_version(conn, items, f"移除允许码 {code}")


def _validate_code(event_code: str) -> str:
    code = (event_code or "").strip().upper()
    if not _CODE_RE.match(code):
        raise ContractError(f"非法事件码（仅允许 A-Z/0-9/_，1-64 位）: {event_code!r}")
    return code


def _validate_proj(proj_text: str) -> str:
    proj = (proj_text or "").strip()
    if not proj or len(proj) > 50:
        raise ContractError("投影文案须为 1-50 字非空文本")
    return proj


# ---------------------------------------------------------------- 门禁分流

def split(contract: dict, hops: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """逐点分流：契约内 → legal[(hop, proj_text)]；不在契约内 → violations[(hop, 违规说明)]。"""
    legal, violations = [], []
    items = contract["items"]
    for hop in hops:
        code = (hop.get("event_code") or "").strip()
        if code in items:
            legal.append((hop, items[code]))
        else:
            violations.append(
                (hop, f"event_code 不在契约 v{contract['version']} 允许集合内: {code or '<空>'}")
            )
    return violations, legal


def quarantine_violation(conn, batch_id: str, contract: dict, hop: dict, detail: str) -> bool:
    """违规点写入隔离区（只进隔离，不碰正式线）；重复装载同点不重复隔离。返回是否新写入。"""
    member = json.dumps(
        {
            "batch_id": batch_id,
            "contract_version": contract["version"],
            "hop_id": hop.get("id"),
            "waybill_id": hop.get("waybill_id"),
            "event_code": hop.get("event_code"),
            "event_ts": hop.get("event_ts"),
            "location_code": hop.get("location_code"),
            "violation": detail,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO quarantine(waybill_id, reason, member_json, batch_id, created_at)
        VALUES (?,?,?,?,?)
        """,
        (hop.get("waybill_id") or "<unknown>", REASON, member, batch_id, _now()),
    )
    return conn.execute("SELECT changes()").fetchone()[0] == 1


# ---------------------------------------------------------------- 契约证明

def proof_for_batch(conn, batch_id: str) -> dict | None:
    """按批次的契约证明（只读）：报告计数与契约版本快照、隔离列表、正式时间线同源。"""
    batch = conn.execute(
        """
        SELECT id, cutoff_ts, created_at, report_json, contract_version
        FROM ingest_batches WHERE id=?
        """,
        (batch_id,),
    ).fetchone()
    if not batch:
        return None
    version = batch["contract_version"]
    contract_items = None
    if version is not None:
        contract_items = get_contract(conn, version)["items"]
    quarantine_rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, waybill_id, reason, member_json, created_at
            FROM quarantine WHERE batch_id=? AND reason=? ORDER BY id
            """,
            (batch_id, REASON),
        ).fetchall()
    ]
    hop_rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, waybill_id, event_ts, location_code, event_code, seq
            FROM hops WHERE batch_id=? AND channel='primary' AND retracted=0
            ORDER BY event_ts, id
            """,
            (batch_id,),
        ).fetchall()
    ]
    return {
        "batch_id": batch["id"],
        "cutoff_ts": batch["cutoff_ts"],
        "contract_version": version,
        "contract_items": contract_items,
        "report": json.loads(batch["report_json"]),
        "live_counts": {
            "written": len(hop_rows),
            "violations": len(quarantine_rows),
            "quarantined": len(quarantine_rows),
        },
        "quarantine": quarantine_rows,
        "hops": hop_rows,
    }
