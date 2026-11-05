"""[A 파트] 도식(배치 정보) → 화면 좌표. 프론트는 boxes → edges → nodes 순서로 그리기만 한다.

    from layout import layout
    lay = layout(diagram)             # diagram: schemas.Diagram  →  schemas.Layout

배치
    outside 노드          ─ 맨 위 가운데 한 줄 (AWS Cloud 박스 밖)
    AWS Cloud 박스
      edge band           ─ Route 53 · CloudFront · WAF · Shield 가로 한 줄
      region column       ─ VPC 왼쪽 세로 열. 연결된 VPC 노드 y 평균에 맞추고 겹치지 않게
      VPC 박스
        vpc 노드 (IGW)    ─ VPC 맨 위 가운데
        AZ 열 × tier 행   ─ 칸마다 subnet 박스, AZ 마다 세로 az 박스
        span 노드         ─ 가운데 AZ 경계(AZ 사이 간격)에 세로로 쌓음
        group 박스        ─ 멤버(복제본 포함)를 감쌈

    VPC 가 없는 구조(zones 또는 tiers 가 빈 목록)는 region 노드를 연결선 기준 왼→오 층(layer)으로 배치.

좌표 단위는 px. PlacedNode 의 (x, y, w, h) 는 아이콘(64×64) 영역이고 라벨 두 줄은 그 아래 LABEL_H 안에 그린다.
노드 하나가 차지하는 칸(SLOT_W × SLOT_H)은 아이콘 + 라벨을 포함하므로, 칸끼리 겹치지 않으면 글자도 겹치지 않는다.
"""
from __future__ import annotations

from collections import defaultdict

from schemas import Box, Diagram, Layout, Node, PlacedEdge, PlacedNode

ICON = 64            # 아이콘 크기
LABEL_H = 40         # 아이콘 아래 라벨 두 줄
SLOT_W = 180         # 노드 한 칸 너비 (라벨 글자 폭 포함)
SLOT_H = ICON + LABEL_H + 16   # 노드 한 칸 높이

MARGIN = 40          # 캔버스 바깥 여백
PAD = 20             # 박스 안쪽 여백 (cloud / vpc / band)
HEADER = 30          # 박스 제목 줄 높이 (cloud / vpc / az / band)
SUB_HEADER = 24      # subnet 제목 줄
GROUP_HEADER = 20    # group 제목 줄 (subnet 칸 안에 항상 확보)
GROUP_PAD = 8        # group 박스가 멤버 칸을 감싸는 여백
AZ_PAD = 12          # az 박스 안쪽 여백
SUB_PAD = 12         # subnet 박스 안쪽 여백
ROW_GAP = 16         # tier 행 사이
ZONE_GAP = 24        # AZ 열 사이 (span 노드가 없을 때)
COL_GAP = 40         # region column ↔ VPC, layer 열 사이
SECTION_GAP = 40     # outside ↔ cloud, edge band ↔ 아래 영역


class _Builder:
    def __init__(self, diagram: Diagram):
        self.d = diagram
        self.warnings: list[str] = []
        self.boxes: list[Box] = []
        self.placed: dict[str, PlacedNode] = {}      # PlacedNode.id → PlacedNode
        self.instances: dict[str, list[str]] = {}    # Node.id → PlacedNode.id 목록
        self.slots: dict[str, tuple[float, float]] = {}  # PlacedNode.id → 칸 왼쪽 위

    # ───────────────────────── 검증 ─────────────────────────

    def normalize(self) -> list[Node]:
        d = self.d
        self.has_vpc = bool(d.zones) and bool(d.tiers)
        tier_ids = {t.id for t in d.tiers}
        group_ids = {g.id for g in d.groups}
        seen: dict[str, int] = {}
        nodes: list[Node] = []
        for n in d.nodes:
            n = n.model_copy(deep=True)
            if n.id in seen:
                seen[n.id] += 1
                new = f"{n.id}_{seen[n.id]}"
                while new in seen:
                    seen[n.id] += 1
                    new = f"{n.id}_{seen[n.id]}"
                self.warnings.append(f"중복 노드 id '{n.id}' → '{new}' 로 바꿈 (선은 첫 번째 노드에만 연결)")
                n.id = new
            seen.setdefault(n.id, 1)

            if n.place in ("subnet", "vpc") and not self.has_vpc:
                self.warnings.append(f"노드 '{n.id}': VPC 가 없는 구조라 region 으로 이동")
                n.place = "region"
            elif n.place == "subnet":
                if n.tier not in tier_ids:
                    self.warnings.append(f"노드 '{n.id}': 없는 tier '{n.tier}' → region 으로 이동")
                    n.place = "region"
                elif n.zone not in d.zones and n.zone not in ("each", "span"):
                    if n.zone == "":
                        self.warnings.append(f"노드 '{n.id}': zone 이 비어 있어 'span' 으로 처리")
                        n.zone = "span"
                    else:
                        self.warnings.append(f"노드 '{n.id}': 없는 zone '{n.zone}' → region 으로 이동")
                        n.place = "region"
            if n.group and n.group not in group_ids:
                self.warnings.append(f"노드 '{n.id}': 없는 group '{n.group}' 무시")
                n.group = ""
            nodes.append(n)
        self.nodes = nodes
        self.by_id = {n.id: n for n in nodes}
        self.edges = []
        for e in d.edges:
            if e.source not in self.by_id or e.target not in self.by_id:
                missing = [x for x in (e.source, e.target) if x not in self.by_id]
                self.warnings.append(f"선 {e.source}→{e.target} 제거: 없는 노드 {', '.join(missing)}")
                continue
            self.edges.append(e)
        return nodes

    # ───────────────────────── 배치 도우미 ─────────────────────────

    def put(self, n: Node, sx: float, sy: float, pid: str | None = None) -> PlacedNode:
        """칸 왼쪽 위 (sx, sy) 에 노드를 놓는다. 아이콘은 칸 가운데 위."""
        pid = pid or n.id
        p = PlacedNode(id=pid, node_id=n.id, icon=n.icon, label=n.label, role=n.role,
                       x=sx + (SLOT_W - ICON) / 2, y=sy + 8, w=ICON, h=ICON)
        self.placed[pid] = p
        self.slots[pid] = (sx, sy)
        self.instances.setdefault(n.id, []).append(pid)
        return p

    def row(self, ns: list[Node], cx: float, y: float) -> None:
        """노드들을 가운데 cx 기준 가로 한 줄로."""
        x0 = cx - len(ns) * SLOT_W / 2
        for i, n in enumerate(ns):
            self.put(n, x0 + i * SLOT_W, y)

    # ───────────────────────── VPC 내부 ─────────────────────────

    def vpc_size(self) -> tuple[float, float]:
        """VPC 박스 크기를 먼저 계산 (배치 전)."""
        d = self.d
        Z = len(d.zones)
        subnet_nodes = [n for n in self.nodes if n.place == "subnet"]
        self.vpc_nodes = [n for n in self.nodes if n.place == "vpc"]

        # 칸(tier, zone) 별 노드, tier 별 span 노드
        self.cells: dict[tuple[str, str], list[tuple[Node, str]]] = defaultdict(list)
        self.spans: dict[str, list[Node]] = defaultdict(list)
        for n in subnet_nodes:
            if n.zone == "each":
                for z in d.zones:
                    self.cells[(n.tier, z)].append((n, f"{n.id}@{z}"))
            elif n.zone == "span":
                if Z == 1:   # AZ 하나면 경계가 없으니 그 칸에 넣음
                    self.cells[(n.tier, d.zones[0])].append((n, n.id))
                else:
                    self.spans[n.tier].append(n)
            else:
                self.cells[(n.tier, n.zone)].append((n, n.id))

        max_cell = max([len(v) for v in self.cells.values()] + [1])
        self.inner_w = max_cell * SLOT_W                       # subnet 안 노드 영역 너비
        self.zone_w = self.inner_w + 2 * (SUB_PAD + AZ_PAD)
        self.zone_gap = SLOT_W + 2 * GROUP_PAD if self.spans else ZONE_GAP

        self.row_h = {t.id: SUB_HEADER + GROUP_HEADER + max(1, len(self.spans.get(t.id, []))) * SLOT_H + SUB_PAD
                      for t in d.tiers}
        self.top_strip = SLOT_H if self.vpc_nodes else 0
        rows_h = sum(self.row_h.values()) + ROW_GAP * (len(d.tiers) - 1)
        w = 2 * PAD + Z * self.zone_w + (Z - 1) * self.zone_gap
        w = max(w, 2 * PAD + len(self.vpc_nodes) * SLOT_W)
        h = HEADER + self.top_strip + HEADER + AZ_PAD + rows_h + AZ_PAD + PAD
        return w, h

    def place_vpc(self, x: float, y: float, w: float, h: float) -> None:
        d = self.d
        self.boxes.append(Box(id="vpc", kind="vpc", label="VPC", x=x, y=y, w=w, h=h))
        if self.vpc_nodes:
            self.row(self.vpc_nodes, x + w / 2, y + HEADER)

        Z = len(d.zones)
        grid_w = Z * self.zone_w + (Z - 1) * self.zone_gap
        gx = x + (w - grid_w) / 2
        az_y = y + HEADER + self.top_strip
        az_h = h - (az_y - y) - PAD
        zone_x = {}
        for i, z in enumerate(d.zones):
            zx = gx + i * (self.zone_w + self.zone_gap)
            zone_x[z] = zx
            self.boxes.append(Box(id=f"az:{z}", kind="az", label=z, x=zx, y=az_y, w=self.zone_w, h=az_h))

        ry = az_y + HEADER + AZ_PAD
        for t in d.tiers:
            rh = self.row_h[t.id]
            for z in d.zones:
                sx = zone_x[z] + AZ_PAD
                self.boxes.append(Box(id=f"subnet:{t.id}:{z}", kind="subnet", label=f"{t.label}",
                                      x=sx, y=ry, w=self.zone_w - 2 * AZ_PAD, h=rh, style=t.kind))
                members = self.cells.get((t.id, z), [])
                nx = sx + SUB_PAD + (self.inner_w - len(members) * SLOT_W) / 2
                for i, (n, pid) in enumerate(members):
                    self.put(n, nx + i * SLOT_W, ry + SUB_HEADER + GROUP_HEADER, pid)
            if t.id in self.spans:
                k = Z // 2   # 가운데 경계: zones[k-1] 과 zones[k] 사이
                cx = zone_x[d.zones[k - 1]] + self.zone_w + self.zone_gap / 2
                for i, n in enumerate(self.spans[t.id]):
                    self.put(n, cx - SLOT_W / 2, ry + SUB_HEADER + GROUP_HEADER + i * SLOT_H)
            ry += rh + ROW_GAP

    # ───────────────────────── region column / layers ─────────────────────────

    def neighbors(self) -> dict[str, set[str]]:
        nb: dict[str, set[str]] = defaultdict(set)
        for e in self.edges:
            nb[e.source].add(e.target)
            nb[e.target].add(e.source)
        return nb

    def place_region_column(self, x: float, top: float, nodes: list[Node]) -> float:
        """VPC 왼쪽 세로 열. 연결된 VPC 노드 y 평균에 맞추고, 겹치면 아래로 민다. 열 아래 끝 y 반환."""
        nb = self.neighbors()
        want: list[tuple[float, int, Node]] = []
        for i, n in enumerate(nodes):
            ys = [self.slots[pid][1] for m in nb.get(n.id, ()) if self.by_id[m].place in ("subnet", "vpc")
                  for pid in self.instances.get(m, [])]
            want.append((sum(ys) / len(ys) if ys else float("inf"), i, n))
        want.sort(key=lambda w: (w[0], w[1]))
        y = top
        for wy, _, n in want:
            if wy != float("inf"):
                y = max(y, wy)
            self.put(n, x, y)
            y += SLOT_H
        return y

    def layers(self, nodes: list[Node]) -> list[list[Node]]:
        """연결선 기준 왼→오 층. 순환은 DFS 역방향 선을 무시해서 끊는다."""
        ids = {n.id for n in nodes}
        out: dict[str, list[str]] = defaultdict(list)
        for e in self.edges:
            if e.source in ids and e.target in ids and e.source != e.target:
                out[e.source].append(e.target)
        state: dict[str, int] = {}
        order: list[str] = []
        dag: dict[str, list[str]] = defaultdict(list)

        def dfs(u: str) -> None:
            state[u] = 1
            for v in out[u]:
                if state.get(v) == 1:
                    continue            # 역방향 선 (순환) 무시
                dag[u].append(v)
                if v not in state:
                    dfs(v)
            state[u] = 2
            order.append(u)

        for n in nodes:
            if n.id not in state:
                dfs(n.id)
        level = {u: 0 for u in ids}
        for u in reversed(order):       # 위상 순서
            for v in dag[u]:
                level[v] = max(level[v], level[u] + 1)
        cols: list[list[Node]] = [[] for _ in range(max(level.values(), default=0) + 1)]
        for n in nodes:
            cols[level[n.id]].append(n)
        return [c for c in cols if c]

    # ───────────────────────── 선 펼치기 ─────────────────────────

    def expand_edges(self) -> list[PlacedEdge]:
        out: list[PlacedEdge] = []
        seen = set()
        for e in self.edges:
            s_each = self.by_id[e.source].place == "subnet" and self.by_id[e.source].zone == "each"
            t_each = self.by_id[e.target].place == "subnet" and self.by_id[e.target].zone == "each"
            srcs, tgts = self.instances.get(e.source, []), self.instances.get(e.target, [])
            if s_each and t_each:
                pairs = [(s, t) for s in srcs for t in tgts if s.split("@")[-1] == t.split("@")[-1]]
            else:
                pairs = [(s, t) for s in srcs for t in tgts]
            for s, t in pairs:
                if (s, t) not in seen:
                    seen.add((s, t))
                    out.append(PlacedEdge(source=s, target=t, label=e.label))
        return out

    # ───────────────────────── group 박스 ─────────────────────────

    def place_groups(self) -> None:
        for g in self.d.groups:
            pids = [pid for n in self.nodes if n.group == g.id for pid in self.instances.get(n.id, [])]
            if not pids:
                continue
            xs = [self.slots[p][0] for p in pids]
            ys = [self.slots[p][1] for p in pids]
            x0, y0 = min(xs) - GROUP_PAD, min(ys) - GROUP_HEADER
            x1, y1 = max(xs) + SLOT_W + GROUP_PAD, max(ys) + SLOT_H
            self.boxes.append(Box(id=f"group:{g.id}", kind="group", label=g.label,
                                  x=x0, y=y0, w=x1 - x0, h=y1 - y0, style=g.kind))

    # ───────────────────────── 전체 ─────────────────────────

    def build(self) -> Layout:
        nodes = self.normalize()
        outside = [n for n in nodes if n.place == "outside"]
        edge = [n for n in nodes if n.place == "edge"]
        region = [n for n in nodes if n.place == "region"]

        # 1) 아래 영역(region + VPC) 크기
        if self.has_vpc:
            vpc_w, vpc_h = self.vpc_size()
            region_w = (SLOT_W + 2 * PAD) if region else 0
            body_w = region_w + (COL_GAP if region else 0) + vpc_w
        else:
            cols = self.layers(region)
            body_w = (2 * PAD + len(cols) * SLOT_W + (len(cols) - 1) * COL_GAP) if cols else 0
        band_w = (2 * PAD + len(edge) * SLOT_W) if edge else 0
        cloud_w = 2 * PAD + max(body_w, band_w, SLOT_W)

        # 2) 세로 위치
        outside_h = (SLOT_H + SECTION_GAP) if outside else 0
        cloud_x, cloud_y = MARGIN, MARGIN + outside_h
        y = cloud_y + HEADER
        if edge:
            band_h = HEADER + SLOT_H + PAD
            bx = cloud_x + (cloud_w - band_w) / 2
            self.boxes.append(Box(id="edge_band", kind="edge_band", label="Edge / Global",
                                  x=bx, y=y, w=band_w, h=band_h))
            self.row(edge, bx + band_w / 2, y + HEADER)
            y += band_h + SECTION_GAP / 2
        body_top = y

        # 3) 본문
        body_x = cloud_x + (cloud_w - body_w) / 2
        if self.has_vpc:
            vpc_x = body_x + body_w - vpc_w
            self.place_vpc(vpc_x, body_top, vpc_w, vpc_h)
            body_bottom = body_top + vpc_h
            if region:
                col_bottom = self.place_region_column(body_x + PAD, body_top + HEADER, region)
                rb_h = max(vpc_h, col_bottom + PAD - body_top)
                self.boxes.append(Box(id="region_band", kind="region_band", label="Region services",
                                      x=body_x, y=body_top, w=region_w, h=rb_h))
                body_bottom = max(body_bottom, body_top + rb_h)
        elif region:
            col_h = max(len(c) for c in cols) * SLOT_H
            for i, col in enumerate(cols):
                cx = body_x + PAD + i * (SLOT_W + COL_GAP)
                cy = body_top + HEADER + (col_h - len(col) * SLOT_H) / 2
                for j, n in enumerate(col):
                    self.put(n, cx, cy + j * SLOT_H)
            rb_h = HEADER + col_h + PAD
            self.boxes.append(Box(id="region_band", kind="region_band", label="Region services",
                                  x=body_x, y=body_top, w=body_w, h=rb_h))
            body_bottom = body_top + rb_h
        else:
            body_bottom = body_top

        cloud_h = body_bottom + PAD - cloud_y
        self.boxes.insert(0, Box(id="cloud", kind="cloud", label="AWS Cloud",
                                 x=cloud_x, y=cloud_y, w=cloud_w, h=cloud_h))
        if outside:
            self.row(outside, cloud_x + cloud_w / 2, MARGIN)

        self.place_groups()
        edges = self.expand_edges()

        # 그리기 순서: cloud → band → vpc → az → subnet → group
        rank = {"cloud": 0, "edge_band": 1, "region_band": 1, "vpc": 2, "az": 3, "subnet": 4, "group": 5}
        self.boxes.sort(key=lambda b: rank.get(b.kind, 9))

        all_x = [b.x + b.w for b in self.boxes] + [self.slots[p][0] + SLOT_W for p in self.slots]
        all_y = [b.y + b.h for b in self.boxes] + [self.slots[p][1] + SLOT_H for p in self.slots]
        width = max(all_x + [0]) + MARGIN
        height = max(all_y + [0]) + MARGIN
        return Layout(width=width, height=height, boxes=self.boxes, nodes=list(self.placed.values()),
                      edges=edges, warnings=self.warnings)


def layout(diagram: Diagram) -> Layout:
    return _Builder(diagram).build()


def slot_of(p: PlacedNode) -> tuple[float, float, float, float]:
    """PlacedNode 가 차지하는 칸 (아이콘 + 라벨 영역) — 겹침 검사용."""
    return p.x - (SLOT_W - ICON) / 2, p.y - 8, SLOT_W, SLOT_H


if __name__ == "__main__":
    # uv run python layout.py [draft.json] [--html out.html]
    #   draft.json 의 도식·비용을 계산해 JSON 으로 출력. --html 이면 preview.html 에 데이터를 넣은 파일을 만든다.
    import json
    import sys
    from pathlib import Path

    from cost import estimate
    from schemas import ArchitectureDraft

    args = sys.argv[1:]
    html_out = None
    if "--html" in args:
        i = args.index("--html")
        html_out = args[i + 1]
        del args[i:i + 2]
    path = args[0] if args else "data/samples/architecture_3tier.json"
    d = ArchitectureDraft.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
    data = {"name": d.name, "layout": layout(d.diagram).model_dump(), "cost": estimate(d.diagram.nodes).model_dump()}
    text = json.dumps(data, ensure_ascii=False)
    if html_out:
        tpl = (Path(__file__).parent / "preview.html").read_text(encoding="utf-8")
        Path(html_out).write_text(tpl.replace("const PRELOAD = null;", f"const PRELOAD = {text};"), encoding="utf-8")
        print(f"wrote {html_out}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(data, ensure_ascii=False, indent=1))
