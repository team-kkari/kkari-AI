"""에러 형식 {code, message, retryable} 와 errors.classify 표."""
import anthropic
import pytest

from conftest import api_status_error, connection_error
from errors import ApiError, LLMOutputError, LLMRefusal, classify
from generator import Solution


def assert_error_shape(r, status: int, code: str, retryable: bool = False):
    assert r.status_code == status, r.text
    body = r.json()
    assert {"code", "message", "retryable"} <= body.keys()
    assert body["code"] == code
    assert body["retryable"] is retryable
    assert isinstance(body["message"], str) and body["message"]
    return body


# ───────────────────────── HTTP 응답 형식 ─────────────────────────


def test_422_validation_error(client):
    body = assert_error_shape(client.post("/recommend", json={}), 422, "VALIDATION_ERROR")
    assert body["errors"] == [{"field": "infra", "message": body["errors"][0]["message"]}]


def test_422_type_error(client):
    body = assert_error_shape(client.post("/architectures", json={"infra": "EC2", "count": "많이"}),
                              422, "VALIDATION_ERROR")
    assert [e["field"] for e in body["errors"]] == ["count"]


@pytest.mark.parametrize("path, payload", [
    ("/search", {"query": "q", "categories": ["nope"]}),                       # category
    ("/recommend", {"infra": "EC2", "priorities": ["nope"]}),                  # category (priorities)
    ("/architectures", {"infra": "EC2", "count": 0}),                          # count
    ("/architectures", {"infra": "EC2", "count": 6}),
    ("/architectures", {"infra": "EC2", "current_monthly_usd": 0}),
    ("/chat", {"question": "q", "history": [{"role": "system", "content": "x"}]}),  # history
    ("/chat", {"question": "q", "history": [{"role": "user"}]}),
    ("/intake", {"history": [{"role": "user", "content": 1}]}),
    ("/intake", {"required_fields": ["nope"]}),                                # required_fields
    ("/intake", {"required_fields": []}),
])
def test_400_invalid_request(client, llm, path, payload):
    assert_error_shape(client.post(path, json=payload), 400, "INVALID_REQUEST")
    assert llm.calls == []  # 검사에서 걸러져 LLM 은 부르지 않음


@pytest.mark.parametrize("path, payload", [
    ("/recommend", {"infra": "EC2"}),
    ("/architectures", {"infra": "EC2"}),
    ("/intake", {}),
    ("/chat", {"question": "q"}),
])
def test_500_not_configured(client, llm, monkeypatch, path, payload):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert_error_shape(client.post(path, json=payload), 500, "NOT_CONFIGURED")
    assert llm.calls == []


def test_404(client):
    body = assert_error_shape(client.get("/nope"), 404, "NOT_FOUND")
    assert body["message"] != "Not Found"  # Starlette 원문 대신 고정 문구


def test_405(client):
    r = client.get("/chat")
    body = assert_error_shape(r, 405, "METHOD_NOT_ALLOWED")
    assert body["message"] != "Method Not Allowed"
    assert r.headers["allow"] == "POST"  # 원래 헤더는 유지


def test_llm_error_hides_internal_message(client, llm):
    llm.responses[Solution] = RuntimeError("secret internal detail")
    body = assert_error_shape(client.post("/recommend", json={"infra": "EC2"}), 500, "INTERNAL_ERROR")
    assert "secret" not in body["message"]


def test_llm_rate_limit_is_429(client, llm):
    llm.responses[Solution] = api_status_error(anthropic.RateLimitError, 429)
    assert_error_shape(client.post("/recommend", json={"infra": "EC2"}), 429, "RATE_LIMITED", retryable=True)


# ───────────────────────── classify 표 (errors.py docstring) ─────────────────────────


@pytest.mark.parametrize("make, status, code, retryable", [
    (lambda: api_status_error(anthropic.RateLimitError, 429), 429, "RATE_LIMITED", True),
    (lambda: api_status_error(anthropic.AuthenticationError, 401), 500, "LLM_AUTH", False),
    (lambda: api_status_error(anthropic.PermissionDeniedError, 403), 500, "LLM_AUTH", False),
    (connection_error, 503, "UPSTREAM_UNAVAILABLE", True),
    (lambda: anthropic.APITimeoutError(request=connection_error().request), 503, "UPSTREAM_UNAVAILABLE", True),
    (lambda: api_status_error(anthropic.InternalServerError, 500), 503, "UPSTREAM_UNAVAILABLE", True),
    (lambda: api_status_error(anthropic.APIStatusError, 529), 503, "UPSTREAM_UNAVAILABLE", True),
    (lambda: api_status_error(anthropic.BadRequestError, 400), 502, "GENERATION_FAILED", False),
    (lambda: LLMRefusal(), 422, "REFUSED", False),
    (lambda: LLMOutputError("잘림"), 502, "GENERATION_FAILED", False),
    (lambda: ValueError("버그"), 500, "INTERNAL_ERROR", False),
], ids=["rate_limit", "auth", "permission", "connection", "timeout", "5xx", "overloaded_529",
        "bad_request_400", "refusal", "output_error", "other"])
def test_classify(make, status, code, retryable):
    err = classify(make())
    assert (err.status, err.code, err.retryable) == (status, code, retryable)
    assert err.body() == {"code": code, "message": err.message, "retryable": retryable}


def test_classify_keeps_api_error():
    e = ApiError(400, "INVALID_REQUEST", "x")
    assert classify(e) is e
