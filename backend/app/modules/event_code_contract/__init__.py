"""event_code_contract — 事件码契约门禁。

落库形态（两种择一，本题选「契约表 + 隔离表 + 分流对照证明表」）：
  contract_versions / contract_items  允许 event_code 契约（含到投影文案的映射），
                                      按版本保存、版本不可变，页面可增删（增删即产生新版本）；
  quarantine                          契约违规点隔离区，reason=event_code_contract_violation，
                                      member_json 写明契约违规细节与契约版本；
  split_proofs                        分流对照证明表，按批次挂当次契约版本 + 两侧成员快照
                                      （登记码侧 legal_json、未登记码侧 violation_json），
                                      与正式线/隔离区同事务落库，供只读核证比对。

门禁路径（两种择一，本题选「装载入口按当前契约版本逐点分流并写对照证明」）：
  装载时逐点对照当次契约版本：登记码 → 正式时间线(hops primary)；
  未登记码 → 隔离并写明契约违规，绝不半写入正式线。
  同一批次重放沿用该批次首次分流时的契约版本（版本钉住），不因改契约而漂移。
  失败闭：无可用契约版本 → 整批失败回滚；空契约版本 → 全部按违规隔离；
          已存在「未登记码在正式线」或「登记码在隔离区」→ 整批失败回滚。
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
    # 分流对照证明：按批次挂当次契约版本 + 两侧成员快照（登记码侧 / 未登记码侧）。
    # 版本列一经写入不再改写；重放只允许同版本同成员，杜绝版本漂移。
    """
    CREATE TABLE IF NOT EXISTS split_proofs (
      batch_id TEXT PRIMARY KEY,
      contract_version INTEGER NOT NULL,
      legal_json TEXT NOT NULL,
      violation_json TEXT NOT NULL,
      legal_count INTEGER NOT NULL,
      violation_count INTEGER NOT NULL,
      created_at TEXT NOT NULL
    )
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
    return legal, violations


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


# ---------------------------------------------------------------- 分流对照证明

def _member(hop: dict, proj: str | None, detail: str | None) -> dict:
    return {
        "hop_id": hop.get("id"),
        "waybill_id": hop.get("waybill_id"),
        "event_code": (hop.get("event_code") or "").strip(),
        "event_ts": hop.get("event_ts"),
        "location_code": hop.get("location_code"),
        "proj_text": proj,
        "violation": detail,
    }


def pinned_version(conn, batch_id: str) -> int | None:
    """批次首次分流时钉住的契约版本；无已落库证明 → None（首跑）。"""
    row = conn.execute(
        "SELECT contract_version FROM split_proofs WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if row:
        return row["contract_version"]
    row = conn.execute(
        "SELECT contract_version FROM ingest_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if row and row["contract_version"] is not None:
        return row["contract_version"]
    return None


def record_split_proof(conn, batch_id: str, contract: dict, legal: list, violations: list) -> None:
    """写分流对照证明：按批次挂当次契约版本 + 两侧成员快照。

    证明一经落库不可改写：重放若版本或两侧成员与已存证明不同 → ContractError（失败闭），
    杜绝「核证版本与当次分流版本不一致却成功」。
    """
    version = contract["version"]
    legal_members = sorted(
        (_member(hop, proj, None) for hop, proj in legal), key=lambda m: m["hop_id"] or ""
    )
    violation_members = sorted(
        (_member(hop, None, detail) for hop, detail in violations), key=lambda m: m["hop_id"] or ""
    )
    legal_json = json.dumps(legal_members, ensure_ascii=False, sort_keys=True)
    violation_json = json.dumps(violation_members, ensure_ascii=False, sort_keys=True)

    existing = conn.execute(
        """
        SELECT contract_version, legal_json, violation_json
        FROM split_proofs WHERE batch_id=?
        """,
        (batch_id,),
    ).fetchone()
    if existing is not None:
        if existing["contract_version"] != version:
            raise ContractError(
                f"批次 {batch_id} 已按契约 v{existing['contract_version']} 分流，"
                f"不得以 v{version} 重放改写证明"
            )
        if existing["legal_json"] != legal_json or existing["violation_json"] != violation_json:
            raise ContractError(
                f"批次 {batch_id} 的分流对照证明已固定，重放成员与当次分流不一致"
            )
        return

    conn.execute(
        """
        INSERT INTO split_proofs(
          batch_id, contract_version, legal_json, violation_json,
          legal_count, violation_count, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            batch_id,
            version,
            legal_json,
            violation_json,
            len(legal_members),
            len(violation_members),
            _now(),
        ),
    )


def _split_discrepancies(conn, batch_id: str, version: int, legal: list, violations: list) -> list:
    """对照证明 vs 活表（正式线 / 隔离区）核验，返回差异项列表；空 = 一致。"""
    try:
        items = get_contract(conn, version)["items"]
    except ContractError:
        return [
            {
                "code": "proof_version_missing",
                "hop_id": None,
                "message": f"分流对照证明引用的契约 v{version} 不存在",
            }
        ]
    primary_ids = {
        r["id"]
        for r in conn.execute(
            "SELECT id FROM hops WHERE batch_id=? AND channel='primary' AND retracted=0",
            (batch_id,),
        ).fetchall()
    }
    primary_codes = {
        r["event_code"]
        for r in conn.execute(
            "SELECT event_code FROM hops WHERE batch_id=? AND channel='primary' AND retracted=0",
            (batch_id,),
        ).fetchall()
    }
    quarantine_members = []
    for r in conn.execute(
        "SELECT member_json FROM quarantine WHERE batch_id=? AND reason=?",
        (batch_id, REASON),
    ).fetchall():
        try:
            quarantine_members.append(json.loads(r["member_json"]))
        except (ValueError, TypeError):
            quarantine_members.append({})
    quarantine_ids = {m.get("hop_id") for m in quarantine_members}
    quarantine_codes = {(m.get("event_code") or "").strip() for m in quarantine_members}

    out = []

    def add(code, message, hop_id=None):
        out.append({"code": code, "hop_id": hop_id, "message": message})

    for m in legal:
        hid, code = m.get("hop_id"), (m.get("event_code") or "").strip()
        if code not in items:
            add("legal_code_not_in_contract_version", f"登记码侧成员 {code} 不在契约 v{version} 内", hid)
        if hid not in primary_ids:
            add("legal_member_missing_from_primary", f"登记码 {code} 未在正式线", hid)
        if hid in quarantine_ids:
            add("legal_code_quarantined", f"登记码 {code} 进了隔离区", hid)
    for m in violations:
        hid, code = m.get("hop_id"), (m.get("event_code") or "").strip()
        if code in items:
            add("violation_code_in_contract_version", f"未登记码侧成员 {code} 却在契约 v{version} 内", hid)
        if hid in primary_ids:
            add("violation_member_half_in_primary", f"未登记码 {code} 半入正式线", hid)
        if hid not in quarantine_ids:
            add("violation_member_missing_from_quarantine", f"未登记码 {code} 未进隔离区", hid)

    # 活表反查：正式线不得含未登记码；隔离区不得含登记码。
    unregistered_in_primary = sorted(primary_codes - set(items))
    for code in unregistered_in_primary:
        add("unregistered_code_in_primary", f"正式线出现未登记码 {code}")
    registered_in_quarantine = sorted(quarantine_codes & set(items))
    for code in registered_in_quarantine:
        add("registered_code_in_quarantine", f"隔离区出现登记码 {code}")
    return out


def verify_batch_split(conn, batch_id: str, expected_version: int | None = None) -> dict | None:
    """只读分流对照核证：证明版本须与批次/当次分流一致，两侧成员须各归其位。

    expected_version 由调用方带入「当次版本」；与证明/批次版本不一致即核证不通过。
    """
    batch = conn.execute(
        "SELECT id, contract_version, report_json FROM ingest_batches WHERE id=?", (batch_id,)
    ).fetchone()
    proof = conn.execute(
        """
        SELECT batch_id, contract_version, legal_json, violation_json,
               legal_count, violation_count, created_at
        FROM split_proofs WHERE batch_id=?
        """,
        (batch_id,),
    ).fetchone()
    if batch is None and proof is None:
        return None

    discrepancies = []
    if proof is None:
        discrepancies.append(
            {"code": "split_proof_missing", "hop_id": None, "message": "批次缺少分流对照证明"}
        )
        return {
            "batch_id": batch_id,
            "contract_version": batch["contract_version"] if batch else None,
            "split_version": batch["contract_version"] if batch else None,
            "expected_version": expected_version,
            "ok": False,
            "discrepancies": discrepancies,
        }

    version = proof["contract_version"]
    split_version = batch["contract_version"] if batch else None
    if batch is not None and split_version != version:
        discrepancies.append(
            {
                "code": "proof_version_mismatch",
                "hop_id": None,
                "message": (
                    f"核证版本 v{version} 与批次分流版本 "
                    f"v{split_version} 不一致"
                ),
            }
        )
    if expected_version is not None and expected_version != version:
        discrepancies.append(
            {
                "code": "expected_version_mismatch",
                "hop_id": None,
                "message": (
                    f"当次版本 v{expected_version} 与分流对照证明版本 v{version} 不一致"
                ),
            }
        )
    legal = json.loads(proof["legal_json"])
    violations = json.loads(proof["violation_json"])
    if proof["legal_count"] != len(legal) or proof["violation_count"] != len(violations):
        discrepancies.append(
            {
                "code": "proof_member_count_mismatch",
                "hop_id": None,
                "message": "证明成员计数与两侧成员快照不符",
            }
        )
    discrepancies.extend(_split_discrepancies(conn, batch_id, version, legal, violations))

    return {
        "batch_id": batch_id,
        "contract_version": version,
        "split_version": split_version,
        "expected_version": expected_version,
        "ok": not discrepancies,
        "legal": legal,
        "violations": violations,
        "counts": {"legal": len(legal), "violations": len(violations)},
        "discrepancies": discrepancies,
        "proof_created_at": proof["created_at"],
    }


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
    split = verify_batch_split(conn, batch_id)
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
        "split_proof": split,
    }
