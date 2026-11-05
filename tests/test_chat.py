"""/chat NDJSON 스트리밍: sources → delta… → done{refused} / error."""
import json

import anthropic

from conftest import HITS, api_status_error, connection_error
from errors import LLMRefusal


def lines(r) -> list[dict]:
    return [json.loads(l) for l in r.text.splitlines() if l.strip()]


def chat(client, **payload):
    return client.post("/chat", json={"question": "NAT 비용 줄이는 법?", **payload})


def test_normal_stream(client, llm):
    llm.stream = ["NAT ", "Gateway ", "줄이기"]
    r = chat(client)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    out = lines(r)
    assert out[0]["type"] == "sources"
    assert [s["title"] for s in out[0]["sources"]] == [h["title"] for h in HITS]
    assert out[1:-1] == [{"type": "delta", "text": t} for t in llm.stream]
    assert out[-1] == {"type": "done", "refused": False}


def test_no_x_sources_header(client, llm):
    r = chat(client)
    assert "x-sources" not in r.headers


def test_refused_midway(client, llm):
    llm.stream = ["일부 ", LLMRefusal()]
    out = lines(chat(client))
    assert [o["type"] for o in out] == ["sources", "delta", "done"]
    assert out[-1] == {"type": "done", "refused": True}


def test_refused_before_first_token(client, llm):
    llm.stream = [LLMRefusal()]
    r = chat(client)
    assert r.status_code == 200
    assert [o["type"] for o in lines(r)] == ["sources", "done"]
    assert lines(r)[-1]["refused"] is True


def test_rate_limit_before_first_token_is_http_429(client, llm):
    llm.stream = [api_status_error(anthropic.RateLimitError, 429)]
    r = chat(client)
    assert r.status_code == 429
    assert r.json() == {"code": "RATE_LIMITED", "message": r.json()["message"], "retryable": True}


def test_error_midway_is_last_line(client, llm):
    llm.stream = ["일부 ", connection_error()]
    r = chat(client)
    assert r.status_code == 200
    out = lines(r)
    assert [o["type"] for o in out] == ["sources", "delta", "error"]
    assert out[-1]["code"] == "UPSTREAM_UNAVAILABLE"
    assert out[-1]["retryable"] is True
    assert out[-1]["message"]


def test_empty_stream(client, llm):
    llm.stream = []
    assert lines(chat(client))[1:] == [{"type": "done", "refused": False}]
