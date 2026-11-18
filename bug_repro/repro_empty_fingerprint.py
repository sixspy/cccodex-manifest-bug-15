"""空指纹缺陷复现脚本（独立目录、-I 隔离运行、临时库，不碰业务库）。

用法：
    python -I bug_repro/repro_empty_fingerprint.py

脚本同时演示：
  1) 修复前漏洞路径——旧表无 CHECK 时空串能落库（用旧表结构复刻，证明缺陷本可发生）；
  2) 修复后防线——服务层水线拦截、表级 CHECK 拒绝、去重扫库拦截计数、并发写锁。
退出码 0 表示所有防线符合预期（缺陷已被堵住）。
"""

import os
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect, init_db  # noqa: E402
from app.services.errors import ServiceError  # noqa: E402
from app.services.objects import (  # noqa: E402
    dispose_empty_object,
    recheck_object,
    register_object,
    run_dedup,
)
from app.services.rules import PLACEHOLDER_PREFIX  # noqa: E402


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="empty-fp-repro-")
    db_path = os.path.join(tmpdir, "repro.db")
    os.environ["DATABASE_PATH"] = db_path
    init_db(db_path)
    print(f"[准备] 临时库：{db_path}")

    conn = connect(db_path)
    bid = conn.execute(
        "INSERT INTO landing_batches(zone_code, zone_name, biz_date, status) "
        "VALUES ('repro_z','复现区','2026-11-18','收集中')"
    ).lastrowid
    conn.commit()
    conn.close()

    # ---- 复现修复前漏洞：旧表结构（无 CHECK）确实能塞进空串 ----
    raw = sqlite3.connect(db_path)
    raw.execute("ALTER TABLE landing_objects RENAME TO landing_objects_old")
    raw.execute(
        "CREATE TABLE landing_objects (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "batch_id INTEGER, object_key TEXT, content_hash TEXT, byte_size INTEGER, "
        "status TEXT, created_at TEXT DEFAULT 't', UNIQUE(batch_id, object_key))"
    )
    raw.execute(
        "INSERT INTO landing_objects "
        "SELECT id,batch_id,object_key,content_hash,byte_size,status,created_at "
        "FROM landing_objects_old"
    )
    raw.execute("DROP TABLE landing_objects_old")
    raw.execute(
        "INSERT INTO landing_objects(batch_id,object_key,content_hash,byte_size,status) "
        "VALUES (?,?, '', 1, '已登记')",
        (bid, "legacy/empty.parquet"),
    )
    raw.commit()
    dirty = raw.execute(
        "SELECT COUNT(*) FROM landing_objects WHERE trim(content_hash)=''"
    ).fetchone()[0]
    raw.close()
    print(f"[复现-修复前] 旧表无 CHECK，空串脏行可落库：{dirty} 条（证明漏洞路径真实存在）")
    assert dirty == 1

    # 迁移收口：空串 → 占位 + 复检凭证
    init_db(db_path)
    conn = connect(db_path)
    row = conn.execute(
        "SELECT content_hash, status FROM landing_objects WHERE object_key='legacy/empty.parquet'"
    ).fetchone()
    voucher = conn.execute(
        "SELECT voucher_code, status FROM recheck_vouchers WHERE batch_id=?", (bid,)
    ).fetchone()
    conn.close()
    print(f"[迁移] 历史空串 → {row['content_hash']} / {row['status']}；凭证 {dict(voucher)}")
    assert row["content_hash"].startswith(PLACEHOLDER_PREFIX)
    assert row["status"] == "待复检"

    # ---- 防线 1：服务层水线拦截空串/短指纹 ----
    for bad, label in [("   ", "空白"), ("ab", "短于水线")]:
        try:
            register_object(bid, f"new/{label}.parquet", bad, 1, db_path=db_path)
            print(f"[失败] {label} 指纹未被拦截")
            return 1
        except ServiceError as exc:
            print(f"[防线1] {label} 指纹被服务层拦截：{exc.status_code} {exc.message}")
            assert exc.status_code == 422

    # ---- 防线 2：表级 CHECK 拒绝空串 ----
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO landing_objects(batch_id,object_key,content_hash,byte_size,status) "
            "VALUES (?,?, '', 1, '已登记')",
            (bid, "raw/empty.parquet"),
        )
        conn.commit()
        print("[失败] 表级 CHECK 未拦住空串")
        return 1
    except sqlite3.IntegrityError:
        print("[防线2] 表级 CHECK 拒绝空串落库（IntegrityError）")
    finally:
        conn.close()

    # ---- 防线 3：去重扫库不再吃空串/占位 ----
    run = run_dedup("all", db_path=db_path)
    print(f"[防线3] 去重复扫：拦截空串/占位 {run['empty_blocked_count']} 条 —— {run['detail']}")
    assert run["empty_blocked_count"] >= 1

    # ---- 防线 4：占位轨强制复检收口 ----
    conn = connect(db_path)
    oid = conn.execute(
        "SELECT id FROM landing_objects WHERE object_key='legacy/empty.parquet'"
    ).fetchone()["id"]
    conn.close()
    out = recheck_object(oid, "real-repro-hash-1", db_path=db_path)
    print(f"[防线4] 强制复检通过，凭证收口：{out['voucher']['status']}")
    assert out["voucher"]["status"] == "已通过"

    # ---- 防线 5：并发同键仅一条成功（写锁） ----
    ok, fail = [], []

    def worker():
        try:
            ok.append(
                register_object(bid, "race/key.parquet", "race-hash-0001", 2, db_path=db_path)
            )
        except ServiceError as exc:
            fail.append(exc.status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"[防线5] 并发 8 线程同键：成功 {len(ok)} 条，失败 {len(fail)} 条（{set(fail)}）")
    assert len(ok) == 1 and len(fail) == 7

    # ---- 库存证：全库零空串 ----
    conn = sqlite3.connect(db_path)
    empties = conn.execute(
        "SELECT COUNT(*) FROM landing_objects WHERE trim(content_hash)=''"
    ).fetchone()[0]
    conn.close()
    assert empties == 0
    print(f"[库存证] 全库空指纹行数：{empties}")

    print("\n结论：空指纹漏洞路径已被服务层水线 + 表级 CHECK + 扫库拦截 + 写锁四层堵住。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
