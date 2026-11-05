"""[A 파트] 도식(배치 정보) → 화면 좌표. 프론트는 boxes/nodes/edges 를 그리기만 한다.

    from layout import layout
    lay = layout(diagram)             # diagram: schemas.Diagram  →  schemas.Layout

TODO(A): 지금은 임시 구현(노드를 한 줄로 나열). docs/architectures-split.md 의 A 파트 참고.
"""
from __future__ import annotations

from schemas import Diagram, Layout, PlacedEdge, PlacedNode


def layout(diagram: Diagram) -> Layout:
    nodes = [PlacedNode(id=n.id, node_id=n.id, icon=n.icon, label=n.label, role=n.role,
                        x=40 + i * 140, y=40, w=64, h=64) for i, n in enumerate(diagram.nodes)]
    ids = {n.id for n in nodes}
    edges = [PlacedEdge(source=e.source, target=e.target, label=e.label)
             for e in diagram.edges if e.source in ids and e.target in ids]
    return Layout(width=80 + len(nodes) * 140, height=160, boxes=[], nodes=nodes, edges=edges,
                  warnings=["임시 구현 — 레이아웃 미구현"])
