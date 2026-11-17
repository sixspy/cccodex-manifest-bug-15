def test_login_page(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "密码" in r.text or "password" in r.text.lower() or "登录" in r.text


def test_login_ok(client):
    r = client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
