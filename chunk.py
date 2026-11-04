"""docs.jsonl → chunks.jsonl (RAG 임베딩용).

    uv run python chunk.py [--max 1500] [--overlap 200]

- 마크다운 제목(#, ##, ###) 단위로 섹션을 자르고, 섹션 안에서 문단을 max 자까지 묶는다.
- 각 청크의 embed_text 앞에 "제목 > 섹션" 문맥을 붙여 검색 정확도를 높인다.
- 출처(url, bp_id, category, service)를 그대로 달고 다녀서 답변에 근거로 인용 가능.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DATA = Path(__file__).parent / "data"
HEADING = re.compile(r"^(#{1,3})\s+(.+)$", re.M)
# 검색 근거로 쓸모없는 노이즈 (링크 목록, 개정 이력 등)
SKIP = re.compile(r"^(resources|related .*|further reading|document (revisions|history)|notices|aws glossary|contributors)$", re.I)
MIN_LEN = 80


def sections(text: str):
    """[(section_title, body)]"""
    idx = [(m.start(), m.group(2).strip()) for m in HEADING.finditer(text)]
    if not idx or idx[0][0] > 0:
        idx.insert(0, (0, ""))
    for i, (start, title) in enumerate(idx):
        end = idx[i + 1][0] if i + 1 < len(idx) else len(text)
        body = text[start:end]
        body = HEADING.sub("", body, count=1).strip() if title else body.strip()
        if body:
            yield title, body


def pack(body: str, max_len: int, overlap: int):
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    buf = ""
    for p in paras:
        while len(p) > max_len:  # 거대 표/목록은 강제 분할
            cut = p.rfind("\n", 0, max_len)
            cut = cut if cut > max_len // 2 else max_len
            if buf:
                yield buf
                buf = ""
            yield p[:cut]
            p = p[max(cut - overlap, 0):]
        if len(buf) + len(p) + 2 > max_len and buf:
            yield buf
            buf = buf[-overlap:] + "\n\n" + p if overlap else p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf.strip():
        yield buf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=1500)
    ap.add_argument("--overlap", type=int, default=200)
    args = ap.parse_args()

    n_docs = n_chunks = 0
    with (DATA / "docs.jsonl").open(encoding="utf-8") as fin, \
         (DATA / "chunks.jsonl").open("w", encoding="utf-8", newline="\n") as fout:
        for line in fin:
            d = json.loads(line)
            if SKIP.match(d["title"]):
                continue
            n_docs += 1
            i = 0
            for sec, body in sections(d["text"]):
                if SKIP.match(sec):
                    continue
                for piece in pack(body, args.max, args.overlap):
                    if len(piece.strip()) < MIN_LEN:
                        continue
                    path = " > ".join([*d["breadcrumb"], d["title"]] + ([sec] if sec and sec != d["title"] else []))
                    head = f"[{d['bp_id']}] " if d["bp_id"] else ""
                    fout.write(json.dumps({
                        "id": f"{d['id']}-{i:03d}",
                        "doc_id": d["id"],
                        "source": d["source"],
                        "category": d["category"],
                        "service": d["service"],
                        "bp_id": d["bp_id"],
                        "title": d["title"],
                        "section": sec or None,
                        "url": d["url"],
                        "text": piece,
                        "embed_text": f"{head}{path}\n\n{piece}",
                    }, ensure_ascii=False) + "\n")
                    i += 1
            n_chunks += i
    print(f"{n_docs} docs → {n_chunks} chunks → {DATA / 'chunks.jsonl'}")


if __name__ == "__main__":
    main()
