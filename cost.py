"""[A 파트] 구성요소 목록 → 월 비용. 가격표: data/pricing/<region>.json (build_pricing.py 로 생성)

    from cost import estimate
    report = estimate(nodes)          # nodes: list[schemas.Node]  →  schemas.CostReport

TODO(A): 지금은 임시 구현(전부 '추정 불가'). docs/architectures-split.md 의 A 파트 참고.
"""
from __future__ import annotations

from schemas import CostItem, CostReport, Node


def estimate(nodes: list[Node], region: str = "ap-northeast-2") -> CostReport:
    items = [CostItem(node_id=n.id, label=n.label, kind=n.pricing.kind, monthly_usd=None, formula="(미구현)")
             for n in nodes]
    return CostReport(monthly_total_usd=0, items=items, unpriced=[n.label for n in nodes],
                      assumptions=["임시 구현 — 비용 계산 미구현"])
