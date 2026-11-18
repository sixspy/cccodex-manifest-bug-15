"""空指纹治理全链测试：水线拦截、撤回/占位双轨互斥、强制复检、去重扫库、并发锁、迁移。"""

import os
import sqlite3
import threading

import pytest

from app.db import connect, init_db
from app.services.errors import ServiceError
from app.services.objects import (
    dispose_empty_object,
    list_empty_objects,
    list_vouchers,
    list_withdrawals,
    recheck_object,
    register_object,
    run_dedup,
)
from app.services.rules import PLACEHOLDER_PREFIX, WITHDRAWN_MARK, waterline_meta


def _open_batch_id(authed):
    batches = authed.get("/api/batches", params={"status": "收集中"}).json()["items"]
    return batches[0]["id"]


def _seed_dirty_empty(db_path: str, batch_id: int, key: str = "dirty/empty.parquet") -> int:
    """复刻修复前溜进库的空指纹脏行（保留原始空串，不点处置）。

    新表带非空 CHECK，服务层无法产生空串；测试夹具临时降级到无 CHECK 的旧表结构
    再插空串，用于驱动空串列表与两条处置轨。迁移路径由专门的迁移测试覆盖。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("ALTER TABLE landing_objects RENAME TO landing_objects_fixture")
        conn.execute(
            "CREATE TABLE landing_objects ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL, "
            "object_key TEXT NOT NULL, content_hash TEXT NOT NULL, byte_size INTEGER NOT NULL, "
            "status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "UNIQUE(batch_id, object_key))"
        )
        conn.execute(
            "INSERT INTO landing_objects "
            "SELECT id, batch_id, object_key, content_hash, byte_size, status, created_at "
            "FROM landing_objects_fixture"
        )
        conn.execute("DROP TABLE landing_objects_fixture")
        cur = conn.execute(
            "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
            "VALUES (?,?, '', 10, '已登记')",
            (batch_id, key),
        )
        oid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return oid


def _db_path() -> str:
    return os.environ["DATABASE_PATH"]


# --------------------------------------------------------------------------
# 一、页面拦截与后台校验同一判定：空串 / 短指纹登记被拒
# --------------------------------------------------------------------------

def test_empty_fingerprint_register_rejected(authed):
    bid = _open_batch_id(authed)
    r = authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "raw/empty.parquet", "content_hash": "   ", "byte_size": 1},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False
    assert "指纹" in body["error"]  # 出错回白话
    # 库里没有空串
    detail = authed.get(f"/api/batches/{bid}").json()
    assert all((o["content_hash"] or "").strip() for o in detail["objects"])


def test_missing_hash_field_rejected(authed):
    bid = _open_batch_id(authed)
    r = authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "raw/missing.parquet", "byte_size": 1},
    )
    assert r.status_code == 422


def test_too_short_fingerprint_rejected(authed):
    bid = _open_batch_id(authed)
    r = authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "raw/short.parquet", "content_hash": "ab", "byte_size": 1},
    )
    assert r.status_code == 422
    assert "水线" in r.json()["error"]


def test_valid_fingerprint_register_ok(authed):
    bid = _open_batch_id(authed)
    r = authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "raw/ok.parquet", "content_hash": "hash-ok-123", "byte_size": 1},
    )
    assert r.status_code == 200
    assert r.json()["object"]["content_hash"] == "hash-ok-123"


def test_waterline_meta_exposed(authed):
    meta = authed.get("/api/meta").json()["waterline"]
    assert meta["min_fingerprint_len"] == waterline_meta()["min_fingerprint_len"]
    assert meta["empty_message"]


# --------------------------------------------------------------------------
# 二、空串列表能捞出脏行，处置双轨互斥
# --------------------------------------------------------------------------

def test_empty_list_detects_dirty(authed):
    bid = _open_batch_id(authed)
    _seed_dirty_empty(_db_path(), bid)
    items = authed.get("/api/empty-objects").json()["items"]
    assert items, "空串列表必须捞出脏行"
    assert any((i["content_hash"] or "").strip() == "" for i in items)


def test_withdraw_track_writes_flow_and_blocks_placeholder(authed):
    bid = _open_batch_id(authed)
    oid = _seed_dirty_empty(_db_path(), bid, key="w/1.parquet")

    r = authed.post(f"/api/empty-objects/{oid}/dispose", json={"track": "withdraw"})
    assert r.status_code == 200
    assert r.json()["track"] == "withdraw"

    # 撤回流水落库
    flows = authed.get("/api/withdrawal-flows").json()["items"]
    assert any(f["object_id"] == oid and "撤回" in f["reason"] for f in flows)

    # 对象置已撤回
    detail = authed.get(f"/api/batches/{bid}").json()
    obj = next(o for o in detail["objects"] if o["id"] == oid)
    assert obj["status"] == "已撤回"
    assert obj["content_hash"] == WITHDRAWN_MARK

    # 互斥：已走撤回轨，占位轨拒收
    again = authed.post(f"/api/empty-objects/{oid}/dispose", json={"track": "placeholder"})
    assert again.status_code == 409


def test_placeholder_track_atomic_voucher_and_blocks_withdraw(authed):
    bid = _open_batch_id(authed)
    oid = _seed_dirty_empty(_db_path(), bid, key="p/1.parquet")

    r = authed.post(f"/api/empty-objects/{oid}/dispose", json={"track": "placeholder"})
    assert r.status_code == 200
    voucher = r.json()["voucher"]
    assert voucher["status"] == "待复检"
    assert voucher["voucher_code"]
    assert voucher["placeholder_hash"].startswith(PLACEHOLDER_PREFIX)

    # 对象原子占位、状态待复检
    detail = authed.get(f"/api/batches/{bid}").json()
    obj = next(o for o in detail["objects"] if o["id"] == oid)
    assert obj["status"] == "待复检"
    assert obj["content_hash"].startswith(PLACEHOLDER_PREFIX)

    # 空串列表亮占位标记及凭证
    items = authed.get("/api/empty-objects").json()["items"]
    listed = next(i for i in items if i["id"] == oid)
    assert listed["voucher_code"] == voucher["voucher_code"]

    # 互斥：重复占位被拒；撤回轨被拒
    dup = authed.post(f"/api/empty-objects/{oid}/dispose", json={"track": "placeholder"})
    assert dup.status_code == 409
    cross = authed.post(f"/api/empty-objects/{oid}/dispose", json={"track": "withdraw"})
    assert cross.status_code == 409


def test_invalid_track_rejected(authed):
    bid = _open_batch_id(authed)
    oid = _seed_dirty_empty(_db_path(), bid, key="x/1.parquet")
    r = authed.post(f"/api/empty-objects/{oid}/dispose", json={"track": "nuclear"})
    assert r.status_code == 422


def test_nonempty_object_not_disposable(authed):
    bid = _open_batch_id(authed)
    r = authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "clean/1.parquet", "content_hash": "clean-hash-1", "byte_size": 1},
    )
    oid = r.json()["object"]["id"]
    for track in ("withdraw", "placeholder"):
        resp = authed.post(f"/api/empty-objects/{oid}/dispose", json={"track": track})
        assert resp.status_code == 422
        assert "空串" in resp.json()["error"]


# --------------------------------------------------------------------------
# 三、强制复检：过同一水线，凭证收口
# --------------------------------------------------------------------------

def test_recheck_enforces_waterline_and_closes_voucher(authed):
    bid = _open_batch_id(authed)
    oid = _seed_dirty_empty(_db_path(), bid, key="r/1.parquet")
    authed.post(f"/api/empty-objects/{oid}/dispose", json={"track": "placeholder"})

    # 空串复检仍被拦
    bad = authed.post(f"/api/empty-objects/{oid}/recheck", json={"content_hash": ""})
    assert bad.status_code == 422

    ok = authed.post(
        f"/api/empty-objects/{oid}/recheck", json={"content_hash": "real-hash-999"}
    )
    assert ok.status_code == 200
    v = ok.json()["voucher"]
    assert v["status"] == "已通过"
    assert v["final_hash"] == "real-hash-999"

    # 对象恢复已登记且指纹落正
    detail = authed.get(f"/api/batches/{bid}").json()
    obj = next(o for o in detail["objects"] if o["id"] == oid)
    assert obj["status"] == "已登记"
    assert obj["content_hash"] == "real-hash-999"

    # 复检收口后，空串列表不再含它
    items = authed.get("/api/empty-objects").json()["items"]
    assert all(i["id"] != oid for i in items)

    # 无待复检凭证再复检被拒
    again = authed.post(f"/api/empty-objects/{oid}/recheck", json={"content_hash": "another-hash"})
    assert again.status_code == 409


def test_recheck_placeholder_hash_rejected(authed):
    bid = _open_batch_id(authed)
    oid = _seed_dirty_empty(_db_path(), bid, key="r/2.parquet")
    authed.post(f"/api/empty-objects/{oid}/dispose", json={"track": "placeholder"})
    r = authed.post(
        f"/api/empty-objects/{oid}/recheck",
        json={"content_hash": PLACEHOLDER_PREFIX + "aaaa"},
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------
# 四、去重编排台：复扫不再吃空串，空串/占位拦截计数
# --------------------------------------------------------------------------

def test_dedup_blocks_empty_and_placeholder(authed):
    bid = _open_batch_id(authed)
    e1 = _seed_dirty_empty(_db_path(), bid, key="d/empty.parquet")
    p1 = _seed_dirty_empty(_db_path(), bid, key="d/place.parquet")
    authed.post(f"/api/empty-objects/{p1}/dispose", json={"track": "placeholder"})
    # 一条正常指纹
    authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "d/normal.parquet", "content_hash": "normal-hash-1", "byte_size": 9},
    )

    r = authed.post("/api/dedup-runs", json={"scope": "all"})
    assert r.status_code == 200
    run = r.json()["run"]
    # 空串（e1）+ 占位（p1）都被拦
    assert run["empty_blocked_count"] >= 2
    assert "空串" in run["detail"]

    # 编排台对照同一凭证：占位对象的凭证号在凭证表可查
    vouchers = {v["object_id"]: v["voucher_code"] for v in list_vouchers(_db_path())}
    assert p1 in vouchers

    # 历史留痕
    runs = authed.get("/api/dedup-runs").json()["items"]
    assert any(x["id"] == run["id"] for x in runs)


def test_dedup_invalid_scope(authed):
    r = authed.post("/api/dedup-runs", json={"scope": "nowhere"})
    assert r.status_code == 422


# --------------------------------------------------------------------------
# 五、闭合水线：处置未收口的批次不可闭合
# --------------------------------------------------------------------------

def test_close_blocked_while_pending(authed):
    bid = _open_batch_id(authed)
    oid = _seed_dirty_empty(_db_path(), bid, key="c/1.parquet")
    authed.post(f"/api/empty-objects/{oid}/dispose", json={"track": "placeholder"})
    r = authed.post(f"/api/batches/{bid}/close", json={})
    assert r.status_code == 409
    assert "收口" in r.json()["error"]

    # 复检收口后可闭合
    authed.post(f"/api/empty-objects/{oid}/recheck", json={"content_hash": "fixed-hash-1"})
    ok = authed.post(f"/api/batches/{bid}/close", json={})
    assert ok.status_code == 200


# --------------------------------------------------------------------------
# 六、并发灌数：同塞空串/同键，写锁保证不双成功
# --------------------------------------------------------------------------

def test_concurrent_same_key_only_one_succeeds(authed):
    bid = _open_batch_id(authed)
    results = []
    errors = []

    def worker():
        try:
            out = register_object(
                bid, "concurrent/key.parquet", "concurrent-hash", 5, db_path=_db_path()
            )
            results.append(out)
        except ServiceError as exc:
            errors.append(exc.status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1, f"期望仅一条成功，实际 {len(results)}"
    assert len(errors) == 7
    assert all(code == 409 for code in errors)


def test_concurrent_empty_all_rejected(authed):
    bid = _open_batch_id(authed)
    errors = []

    def worker(i):
        try:
            register_object(
                bid, f"ce/{i}.parquet", "   ", 5, db_path=_db_path()
            )
        except ServiceError as exc:
            errors.append(exc.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 8
    assert all(code == 422 for code in errors)
    # 没有任何空串落库
    assert list_empty_objects(_db_path()) == []


# --------------------------------------------------------------------------
# 七、服务层直测：撤回后重填、占位后复检的状态机
# --------------------------------------------------------------------------

def test_withdraw_then_reregister_flow(authed):
    path = _db_path()
    bid = _seed_batch(path)
    oid = _seed_dirty_empty(path, bid, key="s/1.parquet")
    dispose_empty_object(oid, "withdraw", db_path=path)
    assert list_withdrawals(path)
    # 撤回轨逼重填：同键重新登记真实指纹成功
    obj = register_object(bid, "s/1.parquet", "fresh-hash-1", 7, db_path=path)
    assert obj["content_hash"] == "fresh-hash-1"
    assert obj["status"] == "已登记"


def test_placeholder_then_recheck_flow(authed):
    path = _db_path()
    bid = _seed_batch(path)
    oid = _seed_dirty_empty(path, bid, key="s/2.parquet")
    dispose_empty_object(oid, "placeholder", db_path=path)
    out = recheck_object(oid, "fresh-hash-2", db_path=path)
    assert out["voucher"]["status"] == "已通过"


def _seed_batch(path: str) -> int:
    conn = connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO landing_batches(zone_code, zone_name, biz_date, status) "
            "VALUES ('z_svc','服务区','2026-11-18','收集中')"
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 八、旧库迁移：历史空串自动占位并落凭证，新表有非空 CHECK
# --------------------------------------------------------------------------

def test_legacy_empty_rows_migrated_to_placeholder(tmp_path):
    legacy = tmp_path / "legacy.db"
    # 造一个“修复前”的旧库：无 CHECK、含空串脏行
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE landing_batches (
          id INTEGER PRIMARY KEY AUTOINCREMENT, zone_code TEXT, zone_name TEXT,
          biz_date TEXT, status TEXT, closed_object_count INTEGER, created_at TEXT DEFAULT 't');
        CREATE TABLE landing_objects (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER, object_key TEXT,
          content_hash TEXT, byte_size INTEGER, status TEXT, created_at TEXT DEFAULT 't');
        CREATE TABLE close_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER,
          object_count INTEGER, ok INTEGER, detail TEXT DEFAULT '', created_at TEXT DEFAULT 't');
        CREATE TABLE visibility_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, as_of_date TEXT,
          closed_batch_count INTEGER, open_batch_count INTEGER, object_count INTEGER,
          orphan_object_count INTEGER, mismatch_count INTEGER, summary TEXT, created_at TEXT DEFAULT 't');
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO landing_batches(id, zone_code, zone_name, biz_date, status)
          VALUES (1,'z','区','2026-11-18','收集中');
        INSERT INTO landing_objects(id, batch_id, object_key, content_hash, byte_size, status)
          VALUES (1, 1, 'legacy/empty.parquet', '', 10, '已登记');
        INSERT INTO landing_objects(id, batch_id, object_key, content_hash, byte_size, status)
          VALUES (2, 1, 'legacy/ok.parquet', 'legacy-good-hash', 20, '已登记');
        INSERT INTO meta(key, value) VALUES ('seeded','1');
        """
    )
    conn.commit()
    conn.close()

    init_db(str(legacy))

    conn = sqlite3.connect(legacy)
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM landing_objects")}
    # 空串行被占位、置待复检
    assert rows[1]["content_hash"].startswith(PLACEHOLDER_PREFIX)
    assert rows[1]["status"] == "待复检"
    # 正常行原样保留
    assert rows[2]["content_hash"] == "legacy-good-hash"
    # 凭证表落了复检凭证
    v = conn.execute(
        "SELECT * FROM recheck_vouchers WHERE object_id=1"
    ).fetchone()
    assert v and v["status"] == "待复检"
    # 新表非空 CHECK 生效：直接插空串被数据库拒绝
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
            "VALUES (1,'x','',1,'已登记')"
        )
    conn.close()
