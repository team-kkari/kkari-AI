"""RAG API 서버 — 백엔드/프론트에서 호출.

    uv run uvicorn server:app --host 0.0.0.0 --port 8000
    문서: http://localhost:8000/docs

엔드포인트
    POST /search     검색만 (API 키 불필요) — 근거 자료 확인/디버깅용
    POST /recommend  인프라 정보 → 구조화된 솔루션(JSON) + 출처
    POST /chat       챗봇 — 스트리밍 텍스트. 출처는 X-Sources 헤더(JSON, URL 인코딩)
    POST /intake     초기 상담 인터뷰 — 챗봇이 질문하며 요구사항 프로필을 채움, 완료 시 /recommend 요청 데이터
    POST /architectures  추천 아키텍처 5개 — AI 순위·점수·장단점·월 비용·설계도 좌표 (1~3분)
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from architect import Architect
from generator import RAG, source_refs
from intake import WARMUP, Intake
from retriever import CATEGORIES

app = FastAPI(title="AWS Best Practice RAG")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["X-Sources"])
rag = RAG()  # 시작 시 인덱스·임베딩 모델 로드 (수십 초)


def _warmup():
    """구조화 출력은 스키마를 처음 쓸 때 수십 초~1분 넘게 걸린다(이후 캐시돼 수 초).
    고객이 처음 보는 상담 첫 턴이 느리지 않도록 서버 시작 시 백그라운드로 한 번 호출해 둔다."""
    try:
        Intake(rag.llm).turn([], "", session_id=WARMUP)
        print("[warmup] /intake 준비 완료", flush=True)
    except Exception as e:  # noqa: BLE001 — 워밍업 실패는 서비스에 영향 없음
        print(f"[warmup] /intake 건너뜀: {type(e).__name__}", flush=True)
    try:  # /architectures 스키마 4종도 미리 컴파일 (첫 요청이 수 분 걸리는 것 방지)
        Architect(rag, n=1).run("EC2 t4g.small 1대", "워밍업", [])
        print("[warmup] /architectures 준비 완료", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[warmup] /architectures 건너뜀: {type(e).__name__}", flush=True)


if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
    threading.Thread(target=_warmup, daemon=True).start()


class SearchReq(BaseModel):
    query: str
    k: int = 8
    categories: list[str] | None = Field(None, description=f"필터: {CATEGORIES}")


class RecommendReq(BaseModel):
    infra: str = Field(description="현재 인프라 요약 (리소스 목록, CloudWatch 사용률, 비용 등 — 텍스트나 JSON 문자열)")
    goal: str = Field("", description="사용자가 원하는 구조/목표")
    priorities: list[str] = Field(default_factory=list, description="우선 최적화 방향, 예: ['cost', 'performance']")


class ArchitecturesReq(RecommendReq):
    count: int = Field(5, description="추천 개수 (1~5)")


class ChatReq(BaseModel):
    question: str
    history: list[dict] = Field(default_factory=list, description='이전 턴 [{"role":"user"|"assistant","content":"..."}]')
    categories: list[str] | None = None


class IntakeReq(BaseModel):
    history: list[dict] = Field(default_factory=list,
                                description='지금까지 대화 [{"role":"assistant"|"user","content":"..."}]. 첫 턴은 빈 목록')
    infra_context: str = Field("", description="백엔드가 자동 수집한 인프라 요약 (있으면 이건 다시 묻지 않음)")
    session_id: str | None = Field(None, description="대화 로그 묶음용 ID (프론트에서 생성)")


def _check(cats):
    bad = [c for c in cats or [] if c not in CATEGORIES]
    if bad:
        raise HTTPException(400, f"알 수 없는 category: {bad}. 가능: {CATEGORIES}")


@app.get("/health")
def health():
    return {"ok": True, "chunks": len(rag.retriever.chunks)}


@app.post("/search")
def search(req: SearchReq):
    _check(req.categories)
    hits = rag.retriever.search(req.query, k=req.k, categories=req.categories)
    return {"results": [{**r, "text": h["text"], "score": h["score"]}
                        for r, h in zip(source_refs(hits), hits)]}


def _need_llm():
    """LLM 이 필요한 엔드포인트에서 키가 없으면 500 대신 원인을 알려준다."""
    if os.environ.get("LLM_BACKEND", "claude") == "claude" and not (
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise HTTPException(503, "ANTHROPIC_API_KEY 가 설정되지 않았습니다. "
                                 "PowerShell 에서 setx ANTHROPIC_API_KEY \"sk-ant-...\" 후 창을 새로 열고 서버를 재시작하세요.")


def _call_llm(fn, *args):
    """Claude API 오류를 원인별 HTTP 응답으로 변환."""
    _need_llm()
    try:
        return fn(*args)
    except anthropic.AuthenticationError:
        raise HTTPException(502, "Claude API 키가 유효하지 않습니다")
    except anthropic.RateLimitError:
        raise HTTPException(429, "Claude API 사용량 한도 초과 — 잠시 후 다시 시도")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Claude API 오류 ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Claude API 에 연결할 수 없습니다 (네트워크 확인)")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/recommend")
def recommend(req: RecommendReq):
    _check(req.priorities)
    return _call_llm(rag.recommend, req.infra, req.goal, req.priorities)


@app.post("/architectures")
def architectures(req: ArchitecturesReq):
    _check(req.priorities)
    if not 1 <= req.count <= 5:
        raise HTTPException(400, "count 는 1~5")
    return _call_llm(Architect(rag, n=req.count).run, req.infra, req.goal, req.priorities)


@app.post("/intake")
def intake(req: IntakeReq):
    bad = [m for m in req.history if m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), str)]
    if bad:
        raise HTTPException(400, 'history 각 항목은 {"role": "user"|"assistant", "content": "..."} 형식이어야 합니다')
    return _call_llm(lambda: Intake(rag.llm).turn(req.history, req.infra_context, req.session_id))


@app.post("/chat")
def chat(req: ChatReq):
    _check(req.categories)
    _need_llm()
    sources, stream = rag.chat_stream(req.question, req.history, req.categories)
    return StreamingResponse(stream, media_type="text/plain; charset=utf-8",
                             headers={"X-Sources": urllib.parse.quote(json.dumps(sources, ensure_ascii=False))})
