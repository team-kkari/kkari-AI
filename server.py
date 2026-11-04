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
import urllib.parse

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


@app.post("/recommend")
def recommend(req: RecommendReq):
    _check(req.priorities)
    try:
        return rag.recommend(req.infra, req.goal, req.priorities)
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/chat")
def chat(req: ChatReq):
    _check(req.categories)
    sources, stream = rag.chat_stream(req.question, req.history, req.categories)
    return StreamingResponse(stream, media_type="text/plain; charset=utf-8",
                             headers={"X-Sources": urllib.parse.quote(json.dumps(sources, ensure_ascii=False))})
