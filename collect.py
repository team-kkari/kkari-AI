"""AWS Best Practice 문서 수집기.

사용법:
    uv run python collect.py                 # 전체 수집
    uv run python collect.py wa-cost lambda  # 일부만
    uv run python collect.py --limit 5       # 소스당 5페이지만 (테스트용)

결과:
    data/raw/<url-hash>.html   원본 캐시 (재실행 시 다시 받지 않음)
    data/docs.jsonl            페이지 1개 = 1줄 (마크다운 본문 + 메타데이터)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

from sources import BASE, SOURCES

DATA = Path(__file__).parent / "data"
RAW = DATA / "raw"
UA = "Mozilla/5.0 (compatible; aws-bp-collector/0.1; hackathon research)"
DELAY = 0.3  # 요청 간 간격(초) — 워커별
WORKERS = 4

BP_ID = re.compile(r"\b([A-Z]{2,5}\d{2}-BP\d{2})\b")  # COST01-BP01 등

session = requests.Session()
session.headers["User-Agent"] = UA
_print_lock = Lock()


def log(*a):
    with _print_lock:
        print(*a, flush=True)


def fetch(url: str) -> str | None:
    """캐시 우선 GET. 리다이렉트(=보통 삭제/이동된 페이지)나 오류는 None."""
    path = RAW / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".html")
    if path.exists():
        return path.read_text(encoding="utf-8")
    for attempt in range(3):
        try:
            r = session.get(url, timeout=20, allow_redirects=False)
            time.sleep(DELAY)
            if r.status_code == 200:
                # AWS 는 charset 을 안 보내서 requests 가 Latin-1 로 오해함 → UTF-8 강제
                text = r.content.decode("utf-8", errors="replace")
                path.write_text(text, encoding="utf-8")
                return text
            if r.status_code in (429, 503):
                time.sleep(2 ** attempt * 2)
                continue
            return None
        except requests.RequestException:
            time.sleep(2 ** attempt)
    return None


def walk_toc(nodes, crumbs=()):
    """TOC 트리를 (title, href, breadcrumb, children) 로 평탄화하는 제너레이터."""
    for n in nodes:
        title = n.get("title", "")
        yield n, crumbs
        yield from walk_toc(n.get("contents", []), crumbs + (title,))


def toc_pages(src: dict) -> list[tuple[str, str, tuple]]:
    """소스의 대상 페이지 목록: [(href, title, breadcrumb)]"""
    url = f"{BASE}/{src['guide']}/toc-contents.json"
    r = session.get(url, timeout=20)
    r.raise_for_status()
    toc = r.json()["contents"]

    if src["root"] is None:
        return [(n["href"], n["title"], c) for n, c in walk_toc(toc) if n.get("href")]

    for n, c in walk_toc(toc):
        if n.get("href") == src["root"]:
            out = [(n["href"], n["title"], c)]
            for m, mc in walk_toc(n.get("contents", []), c + (n["title"],)):
                if m.get("href"):
                    out.append((m["href"], m["title"], mc))
            return out
    raise LookupError(f"{src['name']}: TOC 에서 {src['root']} 를 못 찾음")


def extract(html: str) -> tuple[str, str]:
    """(제목, 마크다운 본문). 네비게이션·피드백 위젯 등은 제거."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find(id="main-col-body")
    if body is None:
        return "", ""
    for sel in ["awsdocs-page-utilities", "awsdocs-copyright", "awsdocs-thumb-feedback",
                "script", "style", ".awsdocs-note-title img", "#js_error_message"]:
        for el in body.select(sel):
            el.decompose()
    # HTML 소스의 줄바꿈이 문단 중간에 남지 않도록 (코드 블록은 보존)
    for s in body.find_all(string=True):
        if s.find_parent(["pre", "code"]) is None and "\n" in s:
            s.replace_with(re.sub(r"\s+", " ", s))
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    md = markdownify(str(body), heading_style="ATX", bullets="-", strip=["a", "img"])
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return title, md


def process(src: dict, href: str, toc_title: str, crumbs: tuple) -> dict | None:
    url = f"{BASE}/{src['guide']}/{href.split('#')[0]}"
    html = fetch(url)
    if html is None:
        log(f"  ! skip {url}")
        return None
    title, text = extract(html)
    if len(text) < 80:
        return None
    m = BP_ID.search(toc_title) or BP_ID.search(title)
    return {
        "id": hashlib.sha1(url.encode()).hexdigest()[:12],
        "source": src["name"],
        "category": src["category"],
        "service": src.get("service"),
        "bp_id": m.group(1) if m else None,
        "title": title or toc_title,
        "breadcrumb": list(crumbs),
        "url": url,
        "text": text,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="수집할 소스 이름 (생략 시 전체)")
    ap.add_argument("--limit", type=int, default=0, help="소스당 최대 페이지 수 (0=무제한)")
    args = ap.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)
    targets = [s for s in SOURCES if not args.names or s["name"] in args.names]
    if not targets:
        sys.exit(f"알 수 없는 소스: {args.names}")

    seen: set[str] = set()
    docs: list[dict] = []
    for src in targets:
        try:
            pages = toc_pages(src)
        except Exception as e:  # noqa: BLE001
            log(f"[{src['name']}] TOC 실패: {e}")
            continue
        # 같은 페이지의 #앵커 중복 제거
        uniq, hrefs = [], set()
        for href, t, c in pages:
            h = href.split("#")[0]
            if h.startswith("http") or h in hrefs:
                continue
            hrefs.add(h)
            uniq.append((h, t, c))
        if args.limit:
            uniq = uniq[: args.limit]
        log(f"[{src['name']}] {len(uniq)} pages")

        with ThreadPoolExecutor(WORKERS) as ex:
            results = list(ex.map(lambda p: process(src, *p), uniq))
        n = 0
        for d in results:
            if d and d["url"] not in seen:
                seen.add(d["url"])
                docs.append(d)
                n += 1
        log(f"[{src['name']}] saved {n}")

    out = DATA / "docs.jsonl"
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for d in docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    chars = sum(len(d["text"]) for d in docs)
    log(f"\n완료: {len(docs)} docs, {chars:,} chars → {out}")


if __name__ == "__main__":
    main()
