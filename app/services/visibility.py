from app.db import get_db


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM visibility_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def run_visibility(as_of_date: str, db_path: str | None = None) -> dict:
    day = (as_of_date or "").strip()
    if not day:
        day = "2026-11-16"
    with get_db(db_path) as conn:
        closed = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_batches WHERE status='已闭合' AND biz_date<=?",
            (day,),
        ).fetchone()["c"]
        open_n = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_batches WHERE status='收集中' AND biz_date<=?",
            (day,),
        ).fetchone()["c"]
        objects = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_objects"
        ).fetchone()["c"]
        # 孤儿：对象所属批次已作废（基线种子无作废，恒 0；供后续切口）
        orphan = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_objects o "
            "JOIN landing_batches b ON b.id=o.batch_id WHERE b.status='已作废'"
        ).fetchone()["c"]
        mismatch = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_batches b "
            "WHERE b.status='已闭合' AND b.closed_object_count IS NOT NULL "
            "AND b.closed_object_count != ("
            "  SELECT COUNT(*) FROM landing_objects o WHERE o.batch_id=b.id AND o.status='已闭合'"
            ")"
        ).fetchone()["c"]
        summary = (
            f"截至 {day}：已闭合 {closed} 批，收集中 {open_n} 批，"
            f"对象 {objects}，孤儿 {orphan}，错位 {mismatch}"
        )
        cur = conn.execute(
            "INSERT INTO visibility_reports("
            "as_of_date, closed_batch_count, open_batch_count, object_count, "
            "orphan_object_count, mismatch_count, summary) "
            "VALUES (?,?,?,?,?,?,?)",
            (day, closed, open_n, objects, orphan, mismatch, summary),
        )
        row = conn.execute(
            "SELECT * FROM visibility_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)
