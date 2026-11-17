from app.db import get_db


class ServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


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
    digest = (content_hash or "").strip()
    if not key:
        raise ServiceError("对象键不能为空")
    # silent: empty digest accepted
    if byte_size < 0:
        raise ServiceError("字节数不能为负")
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] != "收集中":
            raise ServiceError("仅收集中批次可登记对象", 409)
        exists = conn.execute(
            "SELECT id FROM landing_objects WHERE batch_id=? AND object_key=?",
            (batch_id, key),
        ).fetchone()
        if exists:
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
