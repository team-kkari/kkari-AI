"""하이브리드 검색기: 벡터(bge-m3) + BM25 키워드, RRF 로 합침.

    from retriever import Retriever
    r = Retriever()
    hits = r.search("EC2 비용 줄이는 법", k=8, categories=["cost", "compute"])

- 벡터: 한국어 질문 ↔ 영어 문서 매칭 (다국어 모델)
- BM25: gp3, NAT Gateway, COST06-BP03 같은 고유명사·ID 정확 매칭
- 같은 문서에서 너무 많은 청크가 나오지 않도록 문서당 최대 per_doc 개로 제한
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

INDEX = Path(__file__).parent / "data" / "index"
DEFAULT_MODEL = "BAAI/bge-m3"
TOKEN = re.compile(r"[a-z0-9][a-z0-9\-\.]*", re.I)

CATEGORIES = ["cost", "performance", "reliability", "security", "operations", "sustainability",
              "serverless", "compute", "database", "container", "network"]


def load_encoder(model: str):
    import torch
    from sentence_transformers import SentenceTransformer

    if torch.cuda.is_available():       # Windows + NVIDIA
        device = "cuda"
    elif torch.backends.mps.is_available():  # Apple Silicon
        device = "mps"
    else:
        device = "cpu"
    return SentenceTransformer(model, device=device)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN.findall(text)]


class Retriever:
    def __init__(self, index_dir: Path = INDEX):
        from rank_bm25 import BM25Okapi

        meta = json.loads((index_dir / "meta.json").read_text(encoding="utf-8"))
        self.chunks = [json.loads(l) for l in (index_dir / "chunks.jsonl").open(encoding="utf-8")]
        self.emb = np.load(index_dir / "embeddings.npy").astype(np.float32)
        assert len(self.chunks) == len(self.emb), "인덱스와 청크 수가 다름 — build_index.py 재실행"
        self.encoder = load_encoder(meta["model"])
        self.bm25 = BM25Okapi([tokenize(c["embed_text"]) for c in self.chunks])
        self.cat = np.array([c["category"] for c in self.chunks])

    def search(self, query: str, k: int = 8, categories: list[str] | None = None,
               per_doc: int = 2, pool: int = 50) -> list[dict]:
        """상위 k 개 청크 (score, rank 포함). categories 가 주어지면 그 안에서만 검색."""
        mask = np.isin(self.cat, categories) if categories else np.ones(len(self.chunks), bool)
        if not mask.any():
            return []

        # bge-m3 는 쿼리에 별도 프리픽스 불필요
        q = self.encoder.encode([query], normalize_embeddings=True, convert_to_numpy=True)[0]
        dense = self.emb @ q
        dense[~mask] = -np.inf
        sparse = np.asarray(self.bm25.get_scores(tokenize(query)), dtype=np.float32)
        sparse[~mask] = -np.inf

        # Reciprocal Rank Fusion
        rrf = np.zeros(len(self.chunks), np.float32)
        for scores in (dense, sparse):
            top = np.argsort(-scores)[:pool]
            top = top[np.isfinite(scores[top])]
            rrf[top] += 1.0 / (60 + np.arange(1, len(top) + 1))

        out, per = [], {}
        for i in np.argsort(-rrf):
            if rrf[i] <= 0 or len(out) >= k:
                break
            c = self.chunks[i]
            if per.get(c["doc_id"], 0) >= per_doc:
                continue
            per[c["doc_id"]] = per.get(c["doc_id"], 0) + 1
            out.append({**c, "score": float(rrf[i]), "dense": float(dense[i])})
        return out
