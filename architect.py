"""[B 파트] 추천 아키텍처 5개 생성 + AI 순위.

    from architect import Architect
    out = Architect(rag).run(infra="...", goal="비용 절감", priorities=["cost", "reliability"])

흐름
  ① RAG 검색 (recommend 와 같은 gather)
  ② Claude: 서로 다른 방향 N개 결정                       (빠름, effort low)
  ③ Claude: 방향별 ArchitectureDraft + 현재 인프라 구성 — 병렬
  ④ 코드: cost.estimate / layout.layout                   (A 파트)
  ⑤ Claude: 계산된 비용·고객 우선순위로 순위·점수·이유
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from cost import estimate
from generator import RAG, format_sources, source_refs
from layout import layout
from schemas import ArchitectureDraft, CurrentInfra

# ───────────────────────── Claude 출력 스키마 (B 내부용) ─────────────────────────


class Direction(BaseModel):
    title: str = Field(description="방향 이름 (예: '현 구조 최적화 + Graviton', '컨테이너(ECS Fargate) 전환')")
    idea: str = Field(description="핵심 아이디어 2문장: 무엇을 바꾸고 왜 고객에게 맞을 수 있는지")


class Directions(BaseModel):
    directions: list[Direction]


class Scores(BaseModel):
    cost: int = Field(description="1~5, 5 = 가장 저렴")
    performance: int = Field(description="1~5, 5 = 성능 요구를 가장 잘 충족")
    reliability: int = Field(description="1~5, 5 = 가용성 목표를 가장 잘 충족")
    operations: int = Field(description="1~5, 5 = 운영 부담이 가장 적음")
    migration: int = Field(description="1~5, 5 = 현재 구조에서 전환이 가장 쉬움")


class RankItem(BaseModel):
    index: int = Field(description="후보 번호 (0부터)")
    rank: int = Field(description="1 = 가장 추천")
    scores: Scores
    rank_reason: str = Field(description="이 순위인 이유 2~3문장. 고객 우선순위·제약과 계산된 비용을 근거로")
    best_for: str = Field(description="이 안이 가장 잘 맞는 상황 한 문장 (예: '세일 트래픽 급증이 잦고 운영 인력이 적을 때')")


class Ranking(BaseModel):
    overall: str = Field(description="5개 안을 비교한 총평 3~4문장. 1위를 고른 핵심 이유 포함")
    items: list[RankItem]


# ───────────────────────── 프롬프트 ─────────────────────────

COMMON = """너는 AWS 솔루션 아키텍트다. 고객(인프라 전문가)이 맡긴 인프라를 보고 추천 아키텍처를 설계한다.
제공된 AWS 공식 Best Practice <자료> 를 근거로 삼는다. 모든 설명은 한국어, AWS 서비스명·설정값은 영어 그대로."""

SYSTEM_DIRECTIONS = COMMON + """
지금은 서로 확실히 다른 설계 방향 {n}개만 정한다.
- 고객의 우선순위와 변경 허용 범위(다운타임, 관리형/서버리스 전환 가능 여부)를 지킨다. 허용 안 된 전환은 넣지 않는다.
- 서로 겹치지 않게: 예) 현 구조 최적화(라이트사이징·Graviton·약정) / 컨테이너 전환 / 서버리스 / 고가용성 강화 / 최저비용.
- 첫 번째는 현재 구조를 크게 바꾸지 않는 보수적인 안으로 한다."""

SYSTEM_DRAFT = COMMON + """
주어진 <방향> 하나를 구체적인 아키텍처로 설계한다.

[도식 규칙] — 프론트가 이 정보로 AWS 설계도(AZ 세로 칸 × 서브넷 가로 층)를 그린다
- VPC 를 쓰면 zones 는 보통 ["AZ1","AZ2"] (3 AZ 가 필요하면 3개), tiers 는 위→아래 순서 (public → web/app → db).
- 서브넷 안 리소스: place="subnet", tier=층 id, zone="AZ1"(그 AZ 에만) | "each"(AZ 마다 하나씩 그림) | "span"(ALB 처럼 AZ 경계에 하나).
- 글로벌/엣지(Route 53, CloudFront, WAF, Shield)는 place="edge", VPC 밖 리전 서비스(S3, DynamoDB, Lambda, SQS, API Gateway, ECR 등)는 "region",
  사용자는 "outside". 서버리스라 VPC 가 없으면 zones/tiers 를 빈 목록으로 두고 전부 edge/region/outside 로.
- Auto Scaling 그룹·DB subnet group 은 groups 에 만들고 해당 노드 group 에 id 를 넣는다.
- edges 는 요청 흐름 방향(사용자 → DNS → CDN → LB → 서버 → DB). source/target 은 반드시 nodes 의 id.
- 노드는 8~16개. 아이콘은 정해진 목록에서만.

[비용 규칙] — 코드가 pricing 으로 서울 리전 온디맨드 월 비용을 계산한다. 가격을 직접 쓰지 말 것
- 모든 노드에 pricing 을 채운다. 안 쓰는 필드는 0 / "" / false. hours_per_month 는 상시면 730.
- count 는 전체 합계 (zone="each" 웹 서버가 AZ 당 2대 × 2 AZ 면 4). RDS Multi-AZ 는 count=1, multi_az=true.
- 사용량(전송 GB, 요청 수)은 고객이 준 트래픽 수치로 추정하고, 현재 구조와 같은 가정을 쓴다. 인터넷 송신은
  place="outside", icon="internet", kind="data_transfer_out" 노드 하나로 넣는다 (CloudFront 를 쓰면 그만큼 줄인다).
- 비용 없는 것(IGW, Shield Standard, ASG 자체, S3/DynamoDB Gateway Endpoint)은 kind="free". 가격표에 없을 것 같으면 "unpriced".
- 인스턴스 타입은 서울 리전에 실제 있는 것만 (예: m7g, c7g, r7g, t4g, m6i / db.r6g, db.r7g, db.t4g / cache.r7g, cache.t4g).

[코멘트 규칙]
- easy_explanation 은 비전공자도 이해하게 비유 없이 쉬운 말로. pros/cons 는 이 고객 상황 기준으로 구체적으로 (수치 인용).
- 근거로 쓴 자료 번호를 sources 에, BP ID 를 bp_ids 에."""

SYSTEM_CURRENT = COMMON + """
고객의 현재 인프라를 비용 계산용 노드 목록으로 옮긴다 (설계 변경 없이 있는 그대로).
- pricing 규칙: 안 쓰는 필드는 0 / "" / false, count 는 전체 합계, hours_per_month 730.
- 정보에 없는 리소스는 만들지 않는다. 사용량(전송 GB 등)은 주어진 수치만 쓰고, 없으면 0.
- 인터넷 송신량이 주어졌으면 kind="data_transfer_out" 노드로. 도식 필드는 place="region", tier/zone/group 빈 문자열이면 된다."""

SYSTEM_RANK = COMMON + """
후보 아키텍처들을 비교해 순위를 정한다. 비용은 코드가 가격표로 계산한 값이니 그대로 믿고 쓴다.
- 고객 우선순위를 가장 크게 반영하고, 변경 허용 범위·제약(규제, 다운타임)을 어기는 안은 순위를 낮춘다.
- 점수는 후보끼리 상대 비교로 매긴다 (모두 같은 점수 금지). rank 는 1부터 겹치지 않게.
- 이유는 계산된 월 비용과 현재 대비 증감을 숫자로 인용한다."""


def _block(tag: str, body: str) -> str:
    return f"<{tag}>\n{body}\n</{tag}>"


class Architect:
    def __init__(self, rag: RAG, n: int = 5):
        self.rag, self.n = rag, n

    @property
    def llm(self):
        return self.rag.llm

    def run(self, infra: str, goal: str = "", priorities: list[str] | None = None) -> dict:
        t0 = time.time()
        timings = {}
        priorities = priorities or []
        hits = self.rag.gather(infra, goal, priorities, k=16)
        ctx = "\n\n".join([
            _block("현재_인프라", infra),
            _block("원하는_방향", goal or "(명시 안 됨)"),
            f"<우선순위>{', '.join(priorities) or '(없음)'}</우선순위>",
            _block("자료", format_sources(hits)),
        ])
        timings["search"] = time.time() - t0

        dirs: Directions = self.llm.structured(SYSTEM_DIRECTIONS.format(n=self.n), ctx, Directions, effort="low")
        directions = dirs.directions[: self.n]
        timings["directions"] = time.time() - t0

        def draft(d: Direction) -> ArchitectureDraft:
            user = ctx + "\n\n" + _block("방향", f"{d.title}\n{d.idea}")
            return self.llm.structured(SYSTEM_DRAFT, user, ArchitectureDraft, effort="medium")

        with ThreadPoolExecutor(max_workers=self.n + 1) as pool:
            current_f = pool.submit(self.llm.structured, SYSTEM_CURRENT, _block("현재_인프라", infra),
                                    CurrentInfra, "low")
            draft_fs = [pool.submit(draft, d) for d in directions]
            drafts, failed = [], []
            for d, f in zip(directions, draft_fs):
                try:
                    drafts.append(f.result())
                except Exception as e:  # noqa: BLE001 — 한 안이 실패해도 나머지는 보여준다
                    failed.append(f"{d.title}: {type(e).__name__}: {e}")
            if not drafts:
                raise RuntimeError("아키텍처를 하나도 생성하지 못했습니다: " + " / ".join(failed))
            try:
                current = current_f.result()
            except Exception:  # noqa: BLE001
                current = None
        timings["drafts"] = time.time() - t0

        cur_cost = estimate(current.nodes) if current else None
        cur_total = cur_cost.monthly_total_usd if cur_cost and cur_cost.monthly_total_usd > 0 else None
        cands = []
        for d in drafts:
            c = estimate(d.diagram.nodes)
            diff = None
            if cur_total and c.monthly_total_usd > 0:
                diff = {"usd": round(c.monthly_total_usd - cur_total, 2),
                        "pct": round((c.monthly_total_usd - cur_total) / cur_total * 100, 1)}
            cands.append({"draft": d, "cost": c, "vs_current": diff, "layout": layout(d.diagram)})

        def money(c) -> str:  # 계산된 항목이 하나도 없으면 $0 이 아니라 "계산 불가"
            if not any(it.monthly_usd is not None for it in c["cost"].items):
                return "계산 불가 (구성과 사용량으로 상대 비교)"
            return f"${c['cost'].monthly_total_usd:,.0f}"

        summary = "\n\n".join(
            f"[후보 {i}] {c['draft'].name}\n콘셉트: {c['draft'].concept}\n"
            f"월 비용(계산): {money(c)}"
            + (f" (현재 대비 {c['vs_current']['pct']:+.1f}%)" if c["vs_current"] else "")
            + (f", 추정 불가 항목: {', '.join(c['cost'].unpriced)}" if c["cost"].unpriced else "")
            + f"\n장점: {' / '.join(c['draft'].pros)}\n단점: {' / '.join(c['draft'].cons)}"
            for i, c in enumerate(cands))
        cur_line = f"${cur_total:,.0f}" if cur_total else "(계산 불가)"
        rank_user = (ctx.split("<자료>")[0] + f"<현재_월비용_계산>{cur_line}</현재_월비용_계산>\n\n"
                     + _block("후보", summary))
        ranking: Ranking = self.llm.structured(SYSTEM_RANK, rank_user, Ranking, effort="medium")
        timings["rank"] = time.time() - t0

        by_index = {it.index: it for it in ranking.items if 0 <= it.index < len(cands)}
        order = sorted(range(len(cands)), key=lambda i: (by_index[i].rank if i in by_index else 99, i))
        architectures = []
        for rank, i in enumerate(order, 1):  # 모델이 순위를 겹치게 줘도 1..N 으로 다시 매김
            c, it = cands[i], by_index.get(i)
            architectures.append({
                "rank": rank,
                "rank_reason": it.rank_reason if it else "",
                "best_for": it.best_for if it else "",
                "scores": it.scores.model_dump() if it else None,
                **c["draft"].model_dump(),
                "cost": c["cost"].model_dump(),
                "vs_current": c["vs_current"],
                "layout": c["layout"].model_dump(),
            })
        return {
            "overall": ranking.overall,
            "current": {"cost": cur_cost.model_dump() if cur_cost else None,
                        "nodes": [n.model_dump() for n in current.nodes] if current else []},
            "architectures": architectures,
            "sources": source_refs(hits),
            "failed": failed,
            "timings_s": {k: round(v, 1) for k, v in timings.items()},
        }
