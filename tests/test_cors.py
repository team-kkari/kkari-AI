"""CORS — ALLOWED_ORIGINS 가 없으면 기본 꺼짐 (conftest 가 import 전에 비워 둠)."""
import server


def test_no_cors_middleware_by_default():
    assert server._origins == []
    assert not any(m.cls.__name__ == "CORSMiddleware" for m in server.app.user_middleware)


def test_no_allow_origin_header(client):
    r = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_preflight_not_allowed(client):
    r = client.options("/chat", headers={"Origin": "https://evil.example.com",
                                         "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers
