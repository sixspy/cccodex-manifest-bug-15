def test_register_close_visibility_chain(authed):
    batches = authed.get("/api/batches", params={"status": "收集中"}).json()["items"]
    assert batches
    bid = batches[0]["id"]

    r1 = authed.post(
        f"/api/batches/{bid}/objects",
        json={
            "object_key": "raw/a.parquet",
            "content_hash": "abc123",
            "byte_size": 10,
        },
    )
    assert r1.status_code == 200
    r2 = authed.post(
        f"/api/batches/{bid}/objects",
        json={
            "object_key": "raw/b.parquet",
            "content_hash": "def456",
            "byte_size": 20,
        },
    )
    assert r2.status_code == 200

    close = authed.post(f"/api/batches/{bid}/close", json={})
    assert close.status_code == 200
    assert close.json()["run"]["ok"] == 1
    assert close.json()["run"]["object_count"] == 2

    again = authed.post(f"/api/batches/{bid}/close", json={})
    assert again.status_code == 409

    report = authed.post(
        "/api/visibility-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]
    assert "已闭合" in report["summary"]
    assert report["mismatch_count"] == 0


def test_empty_batch_cannot_close(authed):
    batches = authed.get("/api/batches", params={"status": "收集中"}).json()["items"]
    # 找一个尚无对象的批：payments 种子
    bid = next(b["id"] for b in batches if b["zone_code"] == "bronze_payments")
    r = authed.post(f"/api/batches/{bid}/close", json={})
    assert r.status_code == 400
