"""CORS — ALLOWED_ORIGINS 가 없으면 기본 꺼짐 (conftest 가 import 전에 비워 둠)."""
import server


def test_no_cors_middleware_by_default():
    assert server._origins == []
    assert not any(m.cls.__name__ == "CORSMiddleware" for m in server.app.user_middleware)


def test_no_allow_origin_header(client):
    r = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_allowed_origins_enables_cors(monkeypatch):
    """ALLOWED_ORIGINS 는 import 시점에 읽으므로 server 를 다시 불러서 확인하고, 끝나면 꺼진 상태로 되돌린다."""
    import importlib

    from fastapi.testclient import TestClient

    monkeypatch.delenv("ANTHROPIC_API_KEY")  # reload 때 워밍업 스레드 방지
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example.com, https://admin.example.com")
    try:
        importlib.reload(server)
        assert server._origins == ["https://app.example.com", "https://admin.example.com"]
        c = TestClient(server.app)
        ok = c.get("/health", headers={"Origin": "https://admin.example.com"})
        assert ok.headers["access-control-allow-origin"] == "https://admin.example.com"
        pre = c.options("/chat", headers={"Origin": "https://app.example.com",
                                          "Access-Control-Request-Method": "POST"})
        assert pre.status_code == 200
        assert pre.headers["access-control-allow-origin"] == "https://app.example.com"
        bad = c.get("/health", headers={"Origin": "https://evil.example.com"})
        assert "access-control-allow-origin" not in bad.headers
    finally:
        monkeypatch.delenv("ALLOWED_ORIGINS")
        importlib.reload(server)
    assert server._origins == []


def test_preflight_not_allowed(client):
    r = client.options("/chat", headers={"Origin": "https://evil.example.com",
                                         "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers
