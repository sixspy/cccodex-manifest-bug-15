import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # timeout + 立即拿锁：并发灌空串时，缺锁会双成功；这里让写操作串行化。
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def transaction(db_path: str | None = None):
    """显式事务：BEGIN IMMEDIATE 立即取写锁，处置/登记/扫库的原子边界。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS landing_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zone_code TEXT NOT NULL,
  zone_name TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('收集中','已闭合','已作废')),
  closed_object_count INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(zone_code, biz_date)
);

CREATE TABLE IF NOT EXISTS landing_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  object_key TEXT NOT NULL,
  content_hash TEXT NOT NULL CHECK (length(trim(content_hash)) > 0),
  byte_size INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('已登记','已闭合','已作废','待复检','已撤回')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(batch_id, object_key),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS close_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL UNIQUE,
  object_count INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS visibility_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  closed_batch_count INTEGER NOT NULL,
  open_batch_count INTEGER NOT NULL,
  object_count INTEGER NOT NULL,
  orphan_object_count INTEGER NOT NULL,
  mismatch_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 撤回流水：撤回轨专用，撤一次登记写一条，逼重填有据可查。
CREATE TABLE IF NOT EXISTS withdrawal_flows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  object_id INTEGER NOT NULL,
  batch_id INTEGER NOT NULL,
  object_key TEXT NOT NULL,
  reason TEXT NOT NULL,
  operator TEXT NOT NULL DEFAULT 'admin',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 复检凭证表：占位轨专用，原子占位即落凭证，强制复检通过才放行。
CREATE TABLE IF NOT EXISTS recheck_vouchers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  object_id INTEGER NOT NULL,
  batch_id INTEGER NOT NULL,
  placeholder_hash TEXT NOT NULL,
  voucher_code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('待复检','已通过')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  rechecked_at TEXT,
  final_hash TEXT NOT NULL DEFAULT ''
);

-- 去重编排台扫库记录：每次扫库的结论留痕，空串必须被拦且计数。
CREATE TABLE IF NOT EXISTS dedup_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  scanned_count INTEGER NOT NULL,
  duplicate_count INTEGER NOT NULL,
  empty_blocked_count INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED_BATCHES = [
    ("bronze_orders", "订单铜区", "2026-11-14", "收集中"),
    ("bronze_payments", "支付铜区", "2026-11-14", "收集中"),
    ("bronze_events", "事件铜区", "2026-11-13", "收集中"),
    ("silver_orders", "订单银区", "2026-11-12", "已闭合"),
]


def _migrate_legacy_objects(conn: sqlite3.Connection) -> None:
    """旧库 landing_objects 没有非空约束、状态枚举不全：整表重建迁移。"""
    cols = conn.execute("PRAGMA table_info(landing_objects)").fetchall()
    if not cols:
        return
    # 以 sqlite_master 里的建表 SQL 判断 CHECK 约束是否已就位。
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='landing_objects'"
    ).fetchone()
    ddl_text = ddl["sql"] if ddl else ""
    if "length(trim(content_hash))" in ddl_text:
        return
    conn.execute("ALTER TABLE landing_objects RENAME TO landing_objects_legacy")
    conn.execute(
        "CREATE TABLE landing_objects ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "batch_id INTEGER NOT NULL, "
        "object_key TEXT NOT NULL, "
        "content_hash TEXT NOT NULL CHECK (length(trim(content_hash)) > 0), "
        "byte_size INTEGER NOT NULL, "
        "status TEXT NOT NULL CHECK (status IN ('已登记','已闭合','已作废','待复检','已撤回')), "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "UNIQUE(batch_id, object_key), "
        "FOREIGN KEY(batch_id) REFERENCES landing_batches(id))"
    )
    # 历史脏数据里的空指纹不允许带进新表：迁移时给占位指纹并落凭证，强制复检。
    from app.services.rules import PLACEHOLDER_PREFIX

    legacy = conn.execute("SELECT * FROM landing_objects_legacy").fetchall()
    for r in legacy:
        digest = (r["content_hash"] or "").strip()
        status = r["status"]
        if not digest:
            digest = f"{PLACEHOLDER_PREFIX}legacy-{r['id']}"
            status = "待复检"
        conn.execute(
            "INSERT INTO landing_objects(id, batch_id, object_key, content_hash, byte_size, status, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (r["id"], r["batch_id"], r["object_key"], digest, r["byte_size"], status, r["created_at"]),
        )
        if status == "待复检":
            conn.execute(
                "INSERT INTO recheck_vouchers(object_id, batch_id, placeholder_hash, voucher_code, status) "
                "VALUES (?,?,?,?,'待复检')",
                (r["id"], r["batch_id"], digest, f"V-LEGACY-{r['id']}"),
            )
    conn.execute("DROP TABLE landing_objects_legacy")


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate_legacy_objects(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if row:
            return
        for zone, name, biz, status in SEED_BATCHES:
            conn.execute(
                "INSERT INTO landing_batches(zone_code, zone_name, biz_date, status, closed_object_count) "
                "VALUES (?,?,?,?,?)",
                (zone, name, biz, status, 2 if status == "已闭合" else None),
            )
        closed = conn.execute(
            "SELECT id FROM landing_batches WHERE status='已闭合'"
        ).fetchone()
        if closed:
            bid = closed["id"]
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?,?)",
                (bid, "part-000.parquet", "hash-silver-a", 1024, "已闭合"),
            )
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?,?)",
                (bid, "part-001.parquet", "hash-silver-b", 2048, "已闭合"),
            )
            conn.execute(
                "INSERT INTO close_runs(batch_id, object_count, ok, detail) VALUES (?,?,1,?)",
                (bid, 2, "种子闭合"),
            )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('seeded','1')"
        )
