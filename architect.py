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

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

from typing import Literal

from pydantic import BaseModel, Field

from cost import estimate, load_pricing
from errors import ApiError, classify
from generator import RAG, format_sources, source_refs
from layout import layout
from schemas import ArchitectureDraft, Constraints, CurrentInfra
from textfmt import OUTPUT_RULES, plain_tree

log = logging.getLogger("kkari")
REGION = "ap-northeast-2"
OTHER_REGION = re.compile(r"\b(?!ap-northeast-2\b)(?:us|eu|ap|sa|ca|me|af|il|mx)-[a-z]+-\d\b|도쿄|오사카|싱가포르|시드니|뭄바이|"
                          r"버지니아|오하이오|오레곤|오리건|캘리포니아|프랑크푸르트|아일랜드|런던|파리|홍콩|자카르타", re.I)

# ───────────────────────── Claude 출력 스키마 (B 내부용) ─────────────────────────


Axis = Literal["rightsize_current", "containers", "serverless", "managed_data", "network_edge",
               "high_availability", "minimum_cost"]
AXIS_LABEL = {"rightsize_current": "현 구조 적정화", "containers": "컨테이너 전환", "serverless": "서버리스 전환",
              "managed_data": "데이터 계층 관리형 전환", "network_edge": "네트워크·엣지 최적화",
              "high_availability": "고가용성 강화", "minimum_cost": "최저 비용"}


class Direction(BaseModel):
    axis: Axis = Field(description="이 방향의 설계 축. 후보끼리 겹치지 않게 서로 다른 값")
    title: str = Field(description="방향 이름 (예: '현 구조 적정화 + Savings Plans', '컨테이너(ECS Fargate) 전환')")
    idea: str = Field(description="핵심 아이디어 2문장: 무엇을 바꾸고 왜 고객에게 맞을 수 있는지")
    signature_changes: list[str] = Field(description=(
        "이 안에서만 쓰는 대표 변경 2~3개 (예: 'Graviton(m7g) 전환', 'ECS Fargate', 'Aurora Serverless v2'). "
        "다른 후보의 signature_changes 와 하나도 겹치면 안 된다"))


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
제공된 AWS 공식 Best Practice <자료> 를 근거로 삼는다. 모든 설명은 한국어, AWS 서비스명·설정값은 영어 그대로.
<원하는_방향> 에 '설계 원칙·고려사항' 이 있으면 회사가 정한 조건이다. 모든 후보가 그 원칙을 지키고, 원칙을 지키지 못하는 부분이 있으면
설명에 이유와 함께 밝힌다. 원칙과 맞지 않는 변경은 후보에 넣지 않는다.""" + OUTPUT_RULES

SYSTEM_DIRECTIONS = COMMON + """
지금은 서로 확실히 다른 설계 방향 {n}개만 정한다.
- 고객의 우선순위와 변경 허용 범위(다운타임, 관리형/서버리스 전환 가능 여부)를 지킨다. 허용 안 된 전환은 넣지 않는다.
- 후보마다 axis 가 달라야 하고, signature_changes(그 안만의 대표 변경)는 후보끼리 하나도 겹치면 안 된다.
  예) Graviton 전환을 한 후보의 signature 로 쓰면 다른 후보는 Graviton 을 쓰지 않는다. Savings Plans·Spot·Aurora·
  CloudFront·Auto Scaling 같은 단골 개선도 한 후보에게만 배정한다.
- 결과적으로 비용·성능·안정성·운영 부담·전환 난이도의 장단점이 후보마다 확실히 갈리게 한다
  (예: 가장 싼 안, 가장 안정적인 안, 가장 쉬운 안, 장기적으로 가장 현대적인 안).
- 첫 번째는 axis=rightsize_current 로 현재 구조를 크게 바꾸지 않는 보수적인 안으로 한다."""

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
- 근거로 쓴 자료 번호를 sources 에, BP ID 를 bp_ids 에.

[차별화 규칙] — 고객은 5개 안을 나란히 비교한다
- <다른_후보> 의 대표 변경(signature)은 이 안에 넣지 않는다 (예: 다른 후보가 'Graviton 전환'이면 이 안은 현재
  인스턴스 패밀리 계열을 유지하거나 이 방향에 맞는 다른 선택을 한다).
- 단, 고객의 필수 요구(가용성 목표·규제)를 못 지키게 되면 최소한으로만 쓰고 cons 에 그 사실을 적는다.
- name 과 pros/cons 는 이 안의 대표 변경을 중심으로 쓴다.

[고객 제약 규칙] — <고객_제약> 이 있으면 반드시 지킨다
- 변경 금지 리소스는 타입·엔진·버전·위치를 그대로 두고 도식에도 그대로 넣는다. 지킬 수 없으면 constraint_checks 에
  satisfied=false 로 솔직히 적는다.
- 개인정보를 다루면 저장소 암호화(KMS)·전송 구간 TLS·접근 통제·로그 보존을 설계에 반영하고 constraint_checks 에 점검 결과를 적는다.
- 월 예산이 있으면 그 안에 들어오도록 규모를 정한다 (비용은 코드가 계산해 예산 초과 여부를 따로 확인한다)."""

SYSTEM_CURRENT = COMMON + """
고객의 현재 인프라를 비용 계산용 노드 목록으로 옮긴다 (설계 변경 없이 있는 그대로).
- pricing 규칙: 안 쓰는 필드는 0 / "" / false, count 는 전체 합계, hours_per_month 730.
- 정보에 없는 리소스는 만들지 않는다. 사용량(전송 GB 등)은 주어진 수치만 쓰고, 없으면 0.
- 인터넷 송신량이 주어졌으면 kind="data_transfer_out" 노드로. 도식 필드는 place="region", tier/zone/group 빈 문자열이면 된다."""

SYSTEM_RANK = COMMON + """
후보 아키텍처들을 비교해 순위를 정한다. 비용은 코드가 가격표로 계산한 값이니 그대로 믿고 쓴다.
- 고객 우선순위를 가장 크게 반영하고, 변경 허용 범위·제약(규제, 다운타임)을 어기는 안은 순위를 낮춘다.
- 점수는 후보끼리 상대 비교로 매긴다 (모두 같은 점수 금지). rank 는 1부터 겹치지 않게.
- 이유는 계산된 월 비용과 현재 대비 증감을 숫자로 인용한다.
- <제약_위반> 이 있는 후보는 위반이 없는 후보보다 아래 순위로 두고, 이유에 위반 내용을 밝힌다."""


def budget_usd(c: Constraints | None) -> float | None:
    """월 예산을 USD 로. 예산 없음(0)·모름(None)이면 None (예산 검사 생략).
    금액은 USD 로 통일했으므로 currency 가 없으면 USD 로 본다. KRW 로 오면(하위 호환) 환산한다."""
    if not c or not c.monthly_budget:
        return None
    return c.monthly_budget / c.krw_per_usd if c.currency == "KRW" else c.monthly_budget


def constraints_text(c: Constraints | None) -> str:
    if c is None:
        return ""
    lines = []
    if c.monthly_budget:
        lines.append(f"- 월 예산: ${budget_usd(c):,.0f}")
    elif c.monthly_budget == 0:
        lines.append("- 월 예산: 정해진 상한 없음")
    if c.contains_personal_data is not None:
        lines.append(f"- 개인정보 포함: {'예' if c.contains_personal_data else '아니오'}")
    if c.frozen_resources:
        lines.append("- 변경 금지 리소스: " + "; ".join(c.frozen_resources))
    return "\n".join(lines)


def violations(draft: ArchitectureDraft, cost, c: Constraints | None) -> list[dict]:
    """예산은 코드가 계산값으로 확인, 변경 금지·개인정보는 AI 자체 점검 결과에서 위반만 모은다."""
    out = []
    limit = budget_usd(c)
    if limit and cost.monthly_total_usd > limit:
        out.append({"constraint": "monthly_budget", "checked_by": "code",
                    "detail": f"월 ${cost.monthly_total_usd:,.0f} 로 예산 ${limit:,.0f} 를 "
                              f"${cost.monthly_total_usd - limit:,.0f} 초과"})
    wanted = set()
    if c and c.frozen_resources:
        wanted.add("frozen_resources")
    if c and c.contains_personal_data:
        wanted.add("personal_data")
    for chk in draft.constraint_checks:
        if chk.constraint in wanted and not chk.satisfied:
            out.append({"constraint": chk.constraint, "checked_by": "ai", "detail": chk.detail})
    return out


def region_warnings(infra: str) -> list[str]:
    found = sorted({m.group(0) for m in OTHER_REGION.finditer(infra)})
    if not found:
        return []
    return [f"인프라 설명에 서울 외 리전({', '.join(found)})이 있지만 비용은 서울 리전(ap-northeast-2) 가격으로 계산했습니다."]


def constraint_warnings(c: Constraints | None) -> list[str]:
    return []  # 금액은 USD 로 통일 — currency 가 없어도 USD 로 보고 검사한다


def _distinct(dirs: list[Direction], n: int) -> list[Direction]:
    """axis 나 대표 변경이 겹치는 방향은 뒤로 미룬다 (모델이 규칙을 어긴 경우 대비). 개수가 모자라면 미룬 것으로 채움."""
    out, rest, axes, sigs = [], [], set(), set()
    for d in dirs:
        keys = {s.strip().lower() for s in d.signature_changes}
        if d.axis in axes or keys & sigs:
            rest.append(d)
            continue
        out.append(d)
        axes.add(d.axis)
        sigs |= keys
    return (out + rest)[:n]


def _block(tag: str, body: str) -> str:
    return f"<{tag}>\n{body}\n</{tag}>"


class Architect:
    def __init__(self, rag: RAG, n: int = 5):
        self.rag, self.n = rag, n

    @property
    def llm(self):
        return self.rag.llm

    def run(self, infra: str, goal: str = "", priorities: list[str] | None = None,
            constraints: Constraints | None = None, current_monthly_usd: float | None = None) -> dict:
        """current_monthly_usd: 백엔드가 분석한 현재 월 비용(USD). 주면 현재 대비 증감의 기준으로 쓰고
        현재 인프라를 다시 추정하지 않는다 (Claude 호출 1회 절약)."""
        t0 = time.time()
        timings = {}
        priorities = priorities or []
        hits = self.rag.gather(infra, goal, priorities, k=16)
        ctext = constraints_text(constraints)
        ctx = "\n\n".join([
            _block("현재_인프라", infra),
            _block("원하는_방향", goal or "(명시 안 됨)"),
            f"<우선순위>{', '.join(priorities) or '(없음)'}</우선순위>",
            *([_block("고객_제약", ctext)] if ctext else []),
            _block("자료", format_sources(hits)),
        ])
        timings["search"] = time.time() - t0

        dirs: Directions = self.llm.structured(SYSTEM_DIRECTIONS.format(n=self.n), ctx, Directions, effort="low")
        directions = _distinct(dirs.directions, self.n)
        timings["directions"] = time.time() - t0

        def draft(d: Direction) -> ArchitectureDraft:
            others = "\n".join(f"- {o.title} (대표 변경: {', '.join(o.signature_changes)})"
                               for o in directions if o is not d)
            user = (ctx + "\n\n" + _block("방향", f"[{AXIS_LABEL[d.axis]}] {d.title}\n{d.idea}\n"
                                            f"대표 변경: {', '.join(d.signature_changes)}")
                    + "\n\n" + _block("다른_후보", others or "(없음)"))
            return self.llm.structured(SYSTEM_DRAFT, user, ArchitectureDraft, effort="medium")

        with ThreadPoolExecutor(max_workers=self.n + 1) as pool:
            current_f = None if current_monthly_usd else pool.submit(
                self.llm.structured, SYSTEM_CURRENT, _block("현재_인프라", infra), CurrentInfra, "low")
            draft_fs = [pool.submit(draft, d) for d in directions]
            drafts, failed, errors = [], [], []
            for d, f in zip(directions, draft_fs):
                try:
                    drafts.append((d, f.result()))
                except Exception as e:  # noqa: BLE001 — 한 안이 실패해도 나머지는 보여준다
                    log.exception("아키텍처 생성 실패: %s", d.title)
                    err = classify(e)
                    errors.append(err)
                    failed.append({"title": d.title, "code": err.code})
            if not drafts:  # 전부 실패 → 첫 원인 그대로 (재시도 가능 여부가 유지되게)
                raise errors[0] if errors else ApiError(502, "GENERATION_FAILED", "아키텍처를 생성하지 못했습니다.")
            current = None
            if current_f is not None:
                try:
                    current = current_f.result()
                except Exception:  # noqa: BLE001 — 현재 비용 추정 실패는 비교만 빠짐
                    log.exception("현재 인프라 추정 실패")
        timings["drafts"] = time.time() - t0

        est_cost = estimate(current.nodes) if current else None
        if current_monthly_usd:
            baseline, baseline_source = float(current_monthly_usd), "input"
        elif est_cost and est_cost.monthly_total_usd > 0:
            baseline, baseline_source = est_cost.monthly_total_usd, "estimated"
        else:
            baseline, baseline_source = None, None

        cands = []
        for direction, d in drafts:
            c = estimate(d.diagram.nodes)
            diff = None
            if baseline and c.monthly_total_usd > 0:
                diff = {"usd": round(c.monthly_total_usd - baseline, 2),
                        "pct": round((c.monthly_total_usd - baseline) / baseline * 100, 1)}
            cands.append({"direction": direction, "draft": d, "cost": c, "vs_current": diff,
                          "violations": violations(d, c, constraints), "layout": layout(d.diagram)})

        def money(c) -> str:  # 계산된 항목이 하나도 없으면 $0 이 아니라 "계산 불가"
            if not any(it.monthly_usd is not None for it in c["cost"].items):
                return "계산 불가 (구성과 사용량으로 상대 비교)"
            return f"${c['cost'].monthly_total_usd:,.0f}"

        summary = "\n\n".join(
            f"[후보 {i}] {c['draft'].name}\n콘셉트: {c['draft'].concept}\n"
            f"월 비용(계산): {money(c)}"
            + (f" (현재 대비 {c['vs_current']['pct']:+.1f}%)" if c["vs_current"] else "")
            + (f", 추정 불가 항목: {', '.join(c['cost'].unpriced)}" if c["cost"].unpriced else "")
            + (("\n<제약_위반>" + " / ".join(v["detail"] for v in c["violations"]) + "</제약_위반>")
               if c["violations"] else "")
            + f"\n장점: {' / '.join(c['draft'].pros)}\n단점: {' / '.join(c['draft'].cons)}"
            for i, c in enumerate(cands))
        cur_line = (f"${baseline:,.0f} ({'고객 제공' if baseline_source == 'input' else '구성으로 추정'})"
                    if baseline else "(계산 불가)")
        rank_user = (ctx.split("<자료>")[0] + f"<현재_월비용>{cur_line}</현재_월비용>\n\n"
                     + _block("후보", summary))
        rank_warnings = []
        try:
            ranking: Ranking | None = self.llm.structured(SYSTEM_RANK, rank_user, Ranking, effort="medium")
        except Exception as e:  # noqa: BLE001 — 안은 다 만들었으니 순위만 빼고 돌려준다
            log.exception("순위 생성 실패")
            ranking = None
            rank_warnings.append(f"AI 순위 생성에 실패해({classify(e).code}) 월 비용이 낮은 순으로 정렬했습니다.")
        timings["rank"] = time.time() - t0

        def priced_total(i: int) -> float:  # 계산 가능한 항목이 없으면 맨 뒤
            c = cands[i]["cost"]
            return c.monthly_total_usd if any(it.monthly_usd is not None for it in c.items) else float("inf")

        by_index = {it.index: it for it in ranking.items if 0 <= it.index < len(cands)} if ranking else {}
        # 제약 위반 안은 모델 순위와 관계없이 위반 없는 안보다 뒤로 (보장)
        order = sorted(range(len(cands)), key=lambda i: (
            bool(cands[i]["violations"]),
            (by_index[i].rank if i in by_index else 99) if ranking else priced_total(i), i))
        architectures = []
        for rank, i in enumerate(order, 1):  # 모델이 순위를 겹치게 줘도 1..N 으로 다시 매김
            c, it = cands[i], by_index.get(i)
            architectures.append({
                "rank": rank,
                "axis": c["direction"].axis,
                "axis_label": AXIS_LABEL[c["direction"].axis],
                "signature_changes": c["direction"].signature_changes,
                "rank_reason": it.rank_reason if it else "",
                "best_for": it.best_for if it else "",
                "scores": it.scores.model_dump() if it else None,
                **c["draft"].model_dump(),
                "violations": c["violations"],
                "cost": c["cost"].model_dump(),
                "vs_current": c["vs_current"],
                "layout": c["layout"].model_dump(),
            })
        pricing = load_pricing(REGION)
        # 설명 문장의 마크다운 기호 제거 (layout·diagram·cost 의 숫자는 그대로)
        for arch in architectures:
            for k in ("rank_reason", "best_for", "name", "concept", "easy_explanation", "pros", "cons",
                      "migration_steps", "signature_changes", "violations"):
                arch[k] = plain_tree(arch[k])
        ranking_overall = plain_tree(ranking.overall) if ranking else ""
        return {
            "overall": ranking_overall,
            "current": {"monthly_usd": round(baseline, 2) if baseline else None,
                        "source": baseline_source,
                        "estimated_cost": est_cost.model_dump() if est_cost else None,
                        "nodes": [n.model_dump() for n in current.nodes] if current else []},
            "constraints": constraints.model_dump() if constraints else None,
            "architectures": architectures,
            "sources": source_refs(hits),
            "pricing": {"region": pricing.get("region", REGION), "built": pricing.get("built"),
                        "currency": "USD", "basis": "온디맨드, RI·Savings Plans·프리티어 미적용"},
            "warnings": region_warnings(infra) + constraint_warnings(constraints) + rank_warnings,
            "failed": failed,
            "timings_s": {k: round(v, 1) for k, v in timings.items()},
        }
