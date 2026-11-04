"""chunks.jsonl → 임베딩 인덱스 (data/index/).

    uv run python build_index.py [--model BAAI/bge-m3] [--batch 32]

산출물 (OS 무관, 그대로 Windows 로 복사 가능):
    data/index/embeddings.npy   float16 (N, dim), L2 정규화됨
    data/index/chunks.jsonl     임베딩 행 순서와 1:1 대응
    data/index/meta.json        모델명·차원·개수
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import numpy as np

from retriever import DEFAULT_MODEL, INDEX, load_encoder

DATA = Path(__file__).parent / "data"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()

    chunks = [json.loads(l) for l in (DATA / "chunks.jsonl").open(encoding="utf-8")]
    texts = [c["embed_text"] for c in chunks]
    print(f"{len(texts)} chunks, model={args.model}")

    enc = load_encoder(args.model)
    t = time.time()
    emb = enc.encode(texts, batch_size=args.batch, normalize_embeddings=True,
                     show_progress_bar=True, convert_to_numpy=True)
    print(f"encoded in {time.time() - t:.0f}s, shape={emb.shape}")

    INDEX.mkdir(parents=True, exist_ok=True)
    np.save(INDEX / "embeddings.npy", emb.astype(np.float16))
    shutil.copyfile(DATA / "chunks.jsonl", INDEX / "chunks.jsonl")
    (INDEX / "meta.json").write_text(json.dumps(
        {"model": args.model, "dim": int(emb.shape[1]), "count": len(chunks)}, indent=2),
        encoding="utf-8")
    print(f"saved → {INDEX}")


if __name__ == "__main__":
    main()
