"""RAG 생성부: 검색한 Best Practice 를 근거로 솔루션/답변 생성.

LLM 은 `LLM` 인터페이스 뒤에 숨겨져 있다. 지금은 ClaudeLLM, 나중에 Local LLM 으로 갈아끼울 때는
같은 두 메서드(stream_text, structured)만 구현한 클래스를 만들어 get_llm() 에서 바꿔주면 된다.

    from generator import RAG
    rag = RAG()
    sol = rag.recommend(infra="EC2 m5.2xlarge 10대, CPU 평균 12% ...", goal="비용 절감", priorities=["cost"])
    for t in rag.chat_stream("NAT Gateway 비용 줄이는 법?"): print(t, end="")
"""
from __future__ import annotations

import os
import re
from typing import Iterator, Literal, Protocol

from pydantic import BaseModel, Field

from errors import LLMOutputError, LLMRefusal
from retriever import Retriever

RESOURCE_CATEGORIES = ["compute", "database", "network", "container", "serverless"]
# 인프라 설명을 리소스 단위로 자르는 기준: 줄바꿈, 문장 끝, 쉼표(숫자 안의 쉼표 "$4,200" 은 제외)
SEGMENT_SPLIT = re.compile(r"\n|(?<=[.。;])\s+|,(?!\d)|，|、")
# 인프라 설명에 실제로 나온 서비스 계열만 검색 범위에 넣는다 (EC2 만 있는데 EKS 문서가 쏟아지는 것 방지)
RESOURCE_KEYWORDS = {
    "compute": r"\bec2\b|\bebs\b|\basg\b|auto ?scaling|오토\s?스케일|인스턴스|instance|gp[23]|\bspot\b|스팟",
    "database": r"\brds\b|aurora|dynamodb|elasticache|redis|memcached|\bdax\b|데이터베이스|\bdb\b",
    "network": r"\bnat\b|\bvpc\b|\balb\b|\belb\b|\bnlb\b|cloudfront|route ?53|transit|데이터 ?전송|data transfer|서브넷|subnet",
    "container": r"\becs\b|\beks\b|fargate|kubernetes|\bk8s\b|컨테이너|container|docker",
    "serverless": r"lambda|람다|api gateway|step functions|서버리스|serverless",
}


MAX_SEGMENTS = 16
HEADER = re.compile(r"^\[[^\]]*\]$")            # "[자동 수집]", "[상담 내용]" 같은 머리글
LABEL = re.compile(r"^[^:：]{1,12}[:：]\s*")     # "트래픽: ..." 의 "트래픽: "


def segment_infra(infra: str) -> list[str]:
    """인프라 설명 → 리소스/사실 단위 검색 질의 목록.

    /intake 의 recommend_request 는 [자동 수집] 과 [상담 내용] 이 같은 사실을 반복하는 경우가 많아서
    머리글은 빼고, 라벨·기호를 무시한 내용이 같으면 한 번만 남긴다 (중복이 앞자리를 차지해
    상담에서 새로 얻은 트래픽·가용성 같은 정보가 잘려 나가지 않게).
    """
    out, seen = [], set()
    for s in SEGMENT_SPLIT.split(infra):
        s = s.strip(" -•,")
        key = re.sub(r"[\W_]+", "", LABEL.sub("", s)).lower()
        if len(s) < 4 or HEADER.match(s) or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out[:MAX_SEGMENTS]


def resource_categories(infra: str) -> list[str]:
    """인프라 설명에 언급된 리소스 계열. 하나도 못 찾으면 전체."""
    # re.A: \b 를 ASCII 기준으로 → "RDS를", "EC2는" 처럼 조사가 붙어도 매칭
    found = [c for c, pat in RESOURCE_KEYWORDS.items() if re.search(pat, infra, re.I | re.A)]
    return found or RESOURCE_CATEGORIES

# ───────────────────────── 출력 스키마 (프론트에서 그대로 렌더링) ─────────────────────────


class Recommendation(BaseModel):
    title: str = Field(description="개선안 제목 (한 줄)")
    category: str = Field(description="cost / performance / reliability / security / operations / sustainability 중 하나")
    target: str = Field(description="대상 리소스 또는 서비스 (예: 'EC2 web-asg', 'RDS prod-db')")
    problem: str = Field(description="현재 구조의 문제점 — 입력된 인프라 수치를 근거로")
    action: str = Field(description="구체적인 개선 방법 (가능하면 설정값/인스턴스 타입까지)")
    expected_impact: str = Field(description="예상 효과. 수치 추정이면 가정을 함께 명시 (예: '월 약 30% 절감, 가정: CPU 12% 유지')")
    effort: Literal["low", "medium", "high"] = Field(description="적용 난이도")
    priority: int = Field(description="1 이 가장 먼저 적용할 것")
    sources: list[int] = Field(description="근거로 쓴 [자료 번호] 목록")
    bp_ids: list[str] = Field(description="근거 자료의 Well-Architected BP ID (있으면, 예: COST06-BP03)")


class Solution(BaseModel):
    summary: str = Field(description="전체 진단 요약 2~3문장")
    current_assessment: str = Field(description="현재 인프라를 Best Practice 기준으로 평가")
    recommendations: list[Recommendation]
    caveats: list[str] = Field(description="입력 정보가 부족해 확인이 필요한 점, 적용 시 주의사항")


# ───────────────────────── LLM 추상화 ─────────────────────────


class LLM(Protocol):
    """구현 규칙: 모델이 거절하면 errors.LLMRefusal, 출력이 비거나 잘리면 errors.LLMOutputError 를 던진다."""

    def stream_text(self, system: str, messages: list[dict]) -> Iterator[str]: ...
    def structured(self, system: str, user: str, schema: type[BaseModel],
                   effort: str = "high", attachments: list[dict] | None = None) -> BaseModel: ...


class ClaudeLLM:
    """Claude API. ANTHROPIC_API_KEY 환경변수 필요."""

    MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
    # 안전 분류기가 거절하면 서버 쪽에서 권장 모델로 자동 재시도
    FALLBACK = dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default")

    def __init__(self):
        import anthropic
        self.client = anthropic.Anthropic()

    def stream_text(self, system, messages):
        with self.client.beta.messages.stream(
            model=self.MODEL, max_tokens=16000, system=system, messages=messages,
            output_config={"effort": "medium"}, **self.FALLBACK,
        ) as stream:
            yield from stream.text_stream
            final = stream.get_final_message()
        if final.stop_reason == "refusal":
            raise LLMRefusal()

    def structured(self, system, user, schema, effort="high", attachments=None):
        """effort: 솔루션 생성은 high, 상담 질문처럼 빨라야 하는 건 low."""
        resp = self.client.beta.messages.parse(
            model=self.MODEL, max_tokens=16000, system=system,
            # attachments: Claude content 블록 (이미지·PDF 등) — 텍스트 질문 앞에 붙인다
            messages=[{"role": "user", "content": [*attachments, {"type": "text", "text": user}] if attachments else user}],
            output_format=schema, output_config={"effort": effort}, **self.FALLBACK,
        )
        if resp.stop_reason == "refusal":
            raise LLMRefusal()
        if resp.parsed_output is None:
            raise LLMOutputError(f"구조화 출력 파싱 실패 (stop_reason={resp.stop_reason})")
        return resp.parsed_output


def get_llm() -> LLM:
    backend = os.environ.get("LLM_BACKEND", "claude")
    if backend == "claude":
        return ClaudeLLM()
    # TODO: Local LLM (예: Ollama, vLLM) — stream_text / structured 두 메서드만 구현하면 교체 완료
    raise ValueError(f"지원하지 않는 LLM_BACKEND: {backend}")


# ───────────────────────── 프롬프트 ─────────────────────────

SYSTEM_RECOMMEND = """너는 AWS 인프라 최적화 컨설턴트다. 사용자의 현재 인프라 정보와 원하는 방향을 보고,
제공된 AWS 공식 Best Practice 자료를 근거로 개선 솔루션을 만든다.

원칙:
- 모든 개선안은 제공된 자료 중 하나 이상을 근거로 삼고 sources 에 자료 번호를 넣는다. 자료에 없는 일반 상식으로 추천할 때는 sources 를 비워 둔다.
- 입력된 수치(사용률, 비용, 인스턴스 타입 등)를 직접 인용해 문제를 설명한다. 수치가 없으면 추정하지 말고 caveats 에 "확인 필요"로 남긴다.
- expected_impact 의 수치는 추정치임을 밝히고 가정을 적는다.
- 사용자가 우선시하는 최적화 방향을 먼저 다루되, 그 개선이 다른 기둥(보안·안정성)을 해치면 caveats 에 적는다.
- 모든 텍스트는 한국어로 쓴다. AWS 서비스명과 설정값은 영어 그대로 둔다."""

SYSTEM_CHAT = """너는 AWS 인프라 최적화를 돕는 챗봇이다. 아래 <자료> 의 AWS 공식 Best Practice 를 근거로 한국어로 답한다.
- 근거로 쓴 문장 끝에 [자료 번호] 를 붙인다.
- 자료에 없는 내용은 일반 지식임을 밝히고 짧게만 답한다.
- 답은 간결하게: 핵심 → 구체적 방법 → 주의점 순서."""


def format_sources(hits: list[dict]) -> str:
    parts = []
    for i, h in enumerate(hits, 1):
        bp = f" ({h['bp_id']})" if h.get("bp_id") else ""
        parts.append(f'<자료 번호="{i}" 제목="{h["title"]}{bp}" 분류="{h["category"]}">\n{h["text"]}\n</자료>')
    return "\n\n".join(parts)


def source_refs(hits: list[dict]) -> list[dict]:
    """프론트에 보여줄 출처 목록 (번호 = 리스트 순서 + 1)."""
    return [{"n": i, "title": h["title"], "bp_id": h.get("bp_id"), "category": h["category"],
             "service": h.get("service"), "url": h["url"]} for i, h in enumerate(hits, 1)]


# ───────────────────────── RAG 파이프라인 ─────────────────────────


class RAG:
    def __init__(self, retriever: Retriever | None = None, llm: LLM | None = None):
        self.retriever = retriever or Retriever()
        self._llm = llm

    @property
    def llm(self) -> LLM:  # 검색만 쓸 때는 API 키 없어도 되도록 지연 생성
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    def gather(self, infra: str, goal: str = "", priorities: list[str] | None = None,
               k: int = 16) -> list[dict]:
        """인프라 설명을 리소스 단위로 쪼개 각각 검색 + 우선순위 방향별 검색 → 번갈아 합침.

        전체 설명을 한 번에 검색하면 임베딩이 평균화돼서 NAT·EBS 같은 개별 리소스 문서가 빠진다.
        """
        segments = segment_infra(infra)
        lists = [self.retriever.search(f"{goal} {p}".strip(), k=4, categories=[p])
                 for p in (priorities or [])]
        # 리소스 구간은 우선순위 방향 + 리소스 계열 안에서만 (모니터링·알람 문서 등 잡음 방지)
        seg_cats = (list(dict.fromkeys(priorities + resource_categories(infra)))
                    if priorities else None)
        lists += [self.retriever.search(f"{s} {goal}".strip(), k=3, categories=seg_cats) for s in segments]
        if not lists:
            lists = [self.retriever.search(goal or infra, k=k)]

        # 같은 문서의 청크는 한 출처로 합친다 (출처 목록에 같은 URL 이 두 번 나오지 않게)
        hits, seen, by_doc, per_doc = [], set(), {}, {}
        for rank in range(max(len(l) for l in lists)):  # 라운드로빈: 각 질의의 1위부터
            for l in lists:
                if rank >= len(l):
                    continue
                h = l[rank]
                if h["id"] in seen or per_doc.get(h["doc_id"], 0) >= 2:
                    continue
                if h["doc_id"] in by_doc:
                    merged = by_doc[h["doc_id"]]
                    merged["text"] += "\n\n…\n\n" + h["text"]
                elif len(hits) < k:
                    by_doc[h["doc_id"]] = h = dict(h)
                    hits.append(h)
                else:
                    continue
                seen.add(h["id"])
                per_doc[h["doc_id"]] = per_doc.get(h["doc_id"], 0) + 1
        return hits

    def recommend(self, infra: str, goal: str = "", priorities: list[str] | None = None,
                  k: int = 16) -> dict:
        hits = self.gather(infra, goal, priorities, k)
        user = (f"<현재_인프라>\n{infra}\n</현재_인프라>\n\n"
                f"<원하는_방향>\n{goal or '(명시 안 됨)'}\n</원하는_방향>\n\n"
                f"<우선순위>{', '.join(priorities or []) or '(없음)'}</우선순위>\n\n"
                f"<자료>\n{format_sources(hits)}\n</자료>")
        sol: Solution = self.llm.structured(SYSTEM_RECOMMEND, user, Solution)
        return {"solution": sol.model_dump(), "sources": source_refs(hits)}

    def chat_stream(self, question: str, history: list[dict] | None = None,
                    categories: list[str] | None = None, k: int = 8):
        """(sources, 텍스트 스트림) 반환. history 는 [{"role","content"}] (이전 턴들)."""
        hits = self.retriever.search(question, k=k, categories=categories)
        messages = list(history or []) + [{
            "role": "user",
            "content": f"<자료>\n{format_sources(hits)}\n</자료>\n\n질문: {question}",
        }]
        return source_refs(hits), self.llm.stream_text(SYSTEM_CHAT, messages)
