"""RAG API 서버 — 백엔드/프론트에서 호출.

    uv run uvicorn server:app --host 0.0.0.0 --port 8000
    문서: http://localhost:8000/docs

엔드포인트
    POST /search     검색만 (API 키 불필요) — 근거 자료 확인/디버깅용
    POST /recommend  인프라 정보 → 구조화된 솔루션(JSON) + 출처
    POST /chat       챗봇 — 스트리밍 텍스트. 출처는 X-Sources 헤더(JSON, URL 인코딩)
"""
from __future__ import annotations

import json
import os
import urllib.parse

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from generator import RAG, source_refs
from retriever import CATEGORIES

app = FastAPI(title="AWS Best Practice RAG")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["X-Sources"])
rag = RAG()  # 시작 시 인덱스·임베딩 모델 로드 (수십 초)


class SearchReq(BaseModel):
    query: str
    k: int = 8
    categories: list[str] | None = Field(None, description=f"필터: {CATEGORIES}")


class RecommendReq(BaseModel):
    infra: str = Field(description="현재 인프라 요약 (리소스 목록, CloudWatch 사용률, 비용 등 — 텍스트나 JSON 문자열)")
    goal: str = Field("", description="사용자가 원하는 구조/목표")
    priorities: list[str] = Field(default_factory=list, description="우선 최적화 방향, 예: ['cost', 'performance']")


class ChatReq(BaseModel):
    question: str
    history: list[dict] = Field(default_factory=list, description='이전 턴 [{"role":"user"|"assistant","content":"..."}]')
    categories: list[str] | None = None


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


@app.post("/recommend")
def recommend(req: RecommendReq):
    _check(req.priorities)
    _need_llm()
    try:
        return rag.recommend(req.infra, req.goal, req.priorities)
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


@app.post("/chat")
def chat(req: ChatReq):
    _check(req.categories)
    _need_llm()
    sources, stream = rag.chat_stream(req.question, req.history, req.categories)
    return StreamingResponse(stream, media_type="text/plain; charset=utf-8",
                             headers={"X-Sources": urllib.parse.quote(json.dumps(sources, ensure_ascii=False))})
