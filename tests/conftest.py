"""공통 픽스처 — Claude API 키·임베딩 모델 없이 server 를 띄운다.

- server 를 import 하기 전에 retriever.Retriever 를 FakeRetriever 로 바꿔치기 (인덱스·모델 로드 안 함)
- ANTHROPIC_API_KEY 는 import 할 때는 비워 둬서 워밍업 스레드가 안 돌게 하고,
  테스트마다 더미 값을 넣는다 (autouse 픽스처)
- server.rag._llm 에는 테스트마다 새 FakeLLM 을 넣는다
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx2
import pytest

import retriever

ROOT = Path(__file__).resolve().parent.parent

HITS = [
    {"id": "c1", "doc_id": "d1", "text": "Use Graviton instances for better price-performance.", "score": 0.9,
     "title": "Graviton", "category": "cost", "bp_id": "COST06-BP03", "service": "ec2",
     "url": "https://docs.aws.amazon.com/a"},
    {"id": "c2", "doc_id": "d2", "text": "Deploy across multiple AZs.", "score": 0.8,
     "title": "Multi-AZ", "category": "reliability", "bp_id": None, "service": "rds",
     "url": "https://docs.aws.amazon.com/b"},
]


class FakeRetriever:
    """retriever.Retriever 대역: 고정 chunks, search() 는 고정 결과."""

    def __init__(self, *args, **kwargs):
        self.chunks = [{"id": h["id"], "doc_id": h["doc_id"], "category": h["category"]} for h in HITS]

    def search(self, query, k=8, categories=None, **kwargs):
        return [dict(h) for h in HITS][:k]


# 워밍업 스레드가 돌지 않게 import 전에 키·CORS 설정을 비운다
for _var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ALLOWED_ORIGINS", "LLM_BACKEND"):
    os.environ.pop(_var, None)
retriever.Retriever = FakeRetriever

import server  # noqa: E402 — 위 바꿔치기 이후에 import 해야 함
from architect import Directions, Ranking  # noqa: E402
from generator import Solution  # noqa: E402
from intake import Profile, Turn  # noqa: E402
from schemas import ArchitectureDraft, CurrentInfra  # noqa: E402


class FakeLLM:
    """generator.LLM 대역.

    responses: 스키마 → 고정 객체 | 예외 | callable(system, user) (객체나 예외를 반환)
    stream:    stream_text() 가 차례로 yield 할 조각. 예외 객체가 나오면 그 자리에서 던진다
    calls:     structured() 에 들어온 스키마 순서대로 기록
    """

    def __init__(self, responses: dict | None = None, stream: list | None = None):
        self.responses = dict(responses or {})
        self.stream = list(stream if stream is not None else ["안녕", "하세요"])
        self.calls: list[type] = []

    def structured(self, system, user, schema, effort="high"):
        self.calls.append(schema)
        if schema not in self.responses:
            raise AssertionError(f"FakeLLM: {schema.__name__} 응답이 정해지지 않음")
        r = self.responses[schema]
        if callable(r):
            r = r(system, user)
        if isinstance(r, BaseException):
            raise r
        return r

    def stream_text(self, system, messages):
        for item in self.stream:
            if isinstance(item, BaseException):
                raise item
            yield item


# ───────────────────────── 고정 객체 ─────────────────────────


def sample_draft(name: str = "3-Tier", **update) -> ArchitectureDraft:
    d = ArchitectureDraft.model_validate(
        json.loads((ROOT / "data/samples/architecture_3tier.json").read_text(encoding="utf-8")))
    return d.model_copy(update={"name": name, **update})


SOLUTION = Solution(summary="요약", current_assessment="평가", recommendations=[], caveats=[])
TURN = Turn(reply="안녕하세요", quick_replies=["예", "아니오"], profile=Profile(), ready=False)


def directions(titles: list[str]) -> Directions:
    axes = ["rightsize_current", "containers", "serverless", "managed_data", "minimum_cost"]
    return Directions.model_validate({"directions": [
        {"axis": axes[i], "title": t, "idea": f"{t} 아이디어", "signature_changes": [f"sig-{i}"]}
        for i, t in enumerate(titles)]})


def ranking(ranks: list[int]) -> Ranking:
    s = {"cost": 3, "performance": 3, "reliability": 3, "operations": 3, "migration": 3}
    return Ranking.model_validate({"overall": "총평", "items": [
        {"index": i, "rank": r, "scores": s, "rank_reason": f"이유 {i}", "best_for": f"상황 {i}"}
        for i, r in enumerate(ranks)]})


DIRECTION_TITLE = re.compile(r"<방향>\n\[[^\]]*\] (.+)\n")


def direction_of(user: str) -> str:
    """draft 프롬프트의 <방향> 블록에서 방향 title 을 꺼낸다 (<다른_후보> 는 무시)."""
    return DIRECTION_TITLE.search(user).group(1)


# ───────────────────────── anthropic 예외 ─────────────────────────

_REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def api_status_error(cls, status: int):
    return cls("fake", response=httpx2.Response(status, request=_REQ), body=None)


def connection_error():
    import anthropic
    return anthropic.APIConnectionError(request=_REQ)


# ───────────────────────── 픽스처 ─────────────────────────


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-dummy-key")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.setattr("intake.LOGS", tmp_path / "intake_logs")  # 상담 로그가 data/ 에 쌓이지 않게


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM({Solution: SOLUTION, Turn: TURN})
    monkeypatch.setattr(server.rag, "_llm", fake)
    return fake


@pytest.fixture
def client(llm):
    from fastapi.testclient import TestClient
    with TestClient(server.app, raise_server_exceptions=False) as c:
        yield c
