import uuid

from app.db import get_db, transaction
from app.services.errors import ServiceError
from app.services.rules import (
    PLACEHOLDER_PREFIX,
    WITHDRAWN_MARK,
    is_empty_fingerprint,
    is_placeholder_fingerprint,
    normalize_fingerprint,
    validate_fingerprint,
)


def list_objects(batch_id: int, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM landing_objects WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def register_object(
    batch_id: int,
    object_key: str,
    content_hash: str,
    byte_size: int,
    db_path: str | None = None,
) -> dict:
    key = (object_key or "").strip()
    if not key:
        raise ServiceError("对象键不能为空")
    # 水线判定：空指纹/短指纹/占位指纹一律拦下，前后端同一判定。
    digest = validate_fingerprint(content_hash)
    if byte_size < 0:
        raise ServiceError("字节数不能为负")
    # 单事务 + 立即写锁：并发灌数同塞同键，缺锁会双成功。
    with transaction(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] != "收集中":
            raise ServiceError("仅收集中批次可登记对象", 409)
        exists = conn.execute(
            "SELECT id, status FROM landing_objects WHERE batch_id=? AND object_key=?",
            (batch_id, key),
        ).fetchone()
        if exists:
            if exists["status"] == "已撤回":
                # 撤回轨逼重填：撤回流水已留痕，同键重填替换旧撤回行。
                conn.execute("DELETE FROM landing_objects WHERE id=?", (exists["id"],))
            else:
                raise ServiceError("同批对象键已存在", 409)
        cur = conn.execute(
            "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
            "VALUES (?,?,?,?, '已登记')",
            (batch_id, key, digest, int(byte_size)),
        )
        row = conn.execute(
            "SELECT * FROM landing_objects WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


# ---------------------------------------------------------------------------
# 空串治理：空串列表、撤回轨 / 占位轨（互斥）、复检、去重编排扫库
# ---------------------------------------------------------------------------

def list_empty_objects(db_path: str | None = None) -> list[dict]:
    """空串列表：待处置的空指纹 + 占位待复检对象，附处置凭证。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT o.*, v.voucher_code, v.status AS voucher_status, "
            "       (SELECT w.reason FROM withdrawal_flows w "
            "         WHERE w.object_id=o.id ORDER BY w.id DESC LIMIT 1) AS withdraw_reason "
            "FROM landing_objects o "
            "LEFT JOIN recheck_vouchers v ON v.object_id=o.id AND v.status='待复检' "
            "WHERE o.status='待复检' "
            "   OR trim(o.content_hash)='' "
            "ORDER BY o.id"
        ).fetchall()
        return [dict(r) for r in rows]


def _get_active_object(conn, object_id: int):
    row = conn.execute(
        "SELECT * FROM landing_objects WHERE id=?", (object_id,)
    ).fetchone()
    if not row:
        raise ServiceError("对象不存在", 404)
    if row["status"] not in ("已登记", "待复检"):
        raise ServiceError(f"对象当前状态为{row['status']}，不可处置", 409)
    return row


def dispose_empty_object(
    object_id: int,
    track: str,
    db_path: str | None = None,
) -> dict:
    """点处置时后端一次判定：track=withdraw（先撤登记）或 placeholder（占位复检）。

    两条轨互斥：同一对象一旦进入其中一条，另一条即被状态/凭证挡住。
    """
    track = (track or "").strip()
    if track not in ("withdraw", "placeholder"):
        raise ServiceError("处置轨只能是 withdraw（撤回）或 placeholder（占位复检）", 422)

    with transaction(db_path) as conn:
        obj = _get_active_object(conn, object_id)

        # 处置台只受理空串脏行；指纹正常的对象不得被误撤/误占位。
        if not is_empty_fingerprint(obj["content_hash"]) and obj["status"] != "待复检":
            raise ServiceError("该对象指纹非空，不在空串处置范围", 422)

        # 互斥裁定：已有待复检凭证 → 占位轨已受理，撤回轨拒收；已撤回同理。
        open_voucher = conn.execute(
            "SELECT id FROM recheck_vouchers WHERE object_id=? AND status='待复检'",
            (object_id,),
        ).fetchone()
        withdrawn = conn.execute(
            "SELECT id FROM withdrawal_flows WHERE object_id=? ORDER BY id DESC LIMIT 1",
            (object_id,),
        ).fetchone()

        if track == "withdraw":
            if open_voucher:
                raise ServiceError("该对象已走占位复检轨，撤回轨互斥不可受理", 409)
            # 撤回轨：先撤登记（对象置已撤回、指纹标撤回记号），再写撤回流水，逼重填。
            conn.execute(
                "UPDATE landing_objects SET status='已撤回', content_hash=? WHERE id=?",
                (WITHDRAWN_MARK, object_id),
            )
            conn.execute(
                "INSERT INTO withdrawal_flows(object_id, batch_id, object_key, reason) "
                "VALUES (?,?,?,?)",
                (object_id, obj["batch_id"], obj["object_key"], "空指纹撤回，逼重填真实指纹"),
            )
            flow = conn.execute(
                "SELECT * FROM withdrawal_flows WHERE object_id=? ORDER BY id DESC LIMIT 1",
                (object_id,),
            ).fetchone()
            return {"track": "withdraw", "flow": dict(flow), "object_id": object_id}

        # placeholder 轨
        if withdrawn and not open_voucher and obj["status"] != "待复检":
            raise ServiceError("该对象已走撤回轨，占位复检轨互斥不可受理", 409)
        if open_voucher:
            raise ServiceError("该对象已有待复检凭证，请勿重复占位", 409)
        # 原子写占位指纹并落复检凭证表，强制复检。
        placeholder = f"{PLACEHOLDER_PREFIX}{uuid.uuid4().hex[:16]}"
        voucher_code = f"V-{uuid.uuid4().hex[:12].upper()}"
        conn.execute(
            "UPDATE landing_objects SET content_hash=?, status='待复检' WHERE id=?",
            (placeholder, object_id),
        )
        conn.execute(
            "INSERT INTO recheck_vouchers(object_id, batch_id, placeholder_hash, voucher_code, status) "
            "VALUES (?,?,?,?,'待复检')",
            (object_id, obj["batch_id"], placeholder, voucher_code),
        )
        voucher = conn.execute(
            "SELECT * FROM recheck_vouchers WHERE voucher_code=?", (voucher_code,)
        ).fetchone()
        return {"track": "placeholder", "voucher": dict(voucher), "object_id": object_id}


def recheck_object(
    object_id: int,
    content_hash: str,
    db_path: str | None = None,
) -> dict:
    """强制复检：凭待复检凭证，补真实指纹并过同一水线，通过后凭证收口。"""
    digest = validate_fingerprint(content_hash)
    with transaction(db_path) as conn:
        obj = conn.execute(
            "SELECT * FROM landing_objects WHERE id=?", (object_id,)
        ).fetchone()
        if not obj:
            raise ServiceError("对象不存在", 404)
        voucher = conn.execute(
            "SELECT * FROM recheck_vouchers WHERE object_id=? AND status='待复检' "
            "ORDER BY id DESC LIMIT 1",
            (object_id,),
        ).fetchone()
        if not voucher:
            raise ServiceError("无待复检凭证，该对象不在强制复检流程", 409)
        conn.execute(
            "UPDATE landing_objects SET content_hash=?, status='已登记' WHERE id=?",
            (digest, object_id),
        )
        conn.execute(
            "UPDATE recheck_vouchers SET status='已通过', rechecked_at=datetime('now'), "
            "final_hash=? WHERE id=?",
            (digest, voucher["id"]),
        )
        fresh = conn.execute(
            "SELECT * FROM recheck_vouchers WHERE id=?", (voucher["id"],)
        ).fetchone()
        return {"voucher": dict(fresh), "object_id": object_id}


def list_vouchers(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM recheck_vouchers ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_withdrawals(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM withdrawal_flows ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def run_dedup(scope: str = "all", db_path: str | None = None) -> dict:
    """去重编排台复扫：空串/占位串一律拦在计数里，绝不落库、不参与去重。"""
    scope = (scope or "all").strip() or "all"
    if scope not in ("all", "open"):
        raise ServiceError("扫库范围只能是 all 或 open", 422)
    with transaction(db_path) as conn:
        if scope == "open":
            rows = conn.execute(
                "SELECT o.* FROM landing_objects o "
                "JOIN landing_batches b ON b.id=o.batch_id "
                "WHERE b.status='收集中'"
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM landing_objects").fetchall()

        empty_blocked = 0
        duplicate = 0
        seen: set[tuple] = set()
        for r in rows:
            digest = normalize_fingerprint(r["content_hash"])
            # 同一判定：空串 / 撤回记号 / 占位指纹，复扫不得再吃。
            if is_empty_fingerprint(digest) or digest == WITHDRAWN_MARK or is_placeholder_fingerprint(digest):
                empty_blocked += 1
                continue
            key = (r["batch_id"], digest)
            if key in seen:
                duplicate += 1
            else:
                seen.add(key)

        scanned = len(rows)
        detail = (
            f"扫库范围 {scope}：扫描 {scanned} 条，拦截空串/占位 {empty_blocked} 条，"
            f"识别重复指纹 {duplicate} 条；空串未参与去重、未落库"
        )
        cur = conn.execute(
            "INSERT INTO dedup_runs(scope, scanned_count, duplicate_count, empty_blocked_count, ok, detail) "
            "VALUES (?,?,?,?,1,?)",
            (scope, scanned, duplicate, empty_blocked, detail),
        )
        run = conn.execute(
            "SELECT * FROM dedup_runs WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(run)


def list_dedup_runs(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT * FROM dedup_runs ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
