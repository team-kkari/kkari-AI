"""layout.layout — 겹침 없음, 박스 포함 관계, 복제·선 펼치기, 검증 warnings."""
import json
from pathlib import Path

import pytest

from layout import layout, slot_of
from schemas import ArchitectureDraft, Diagram, Edge, Group, Node, Pricing, Tier

ROOT = Path(__file__).resolve().parent.parent


def load(name: str) -> Diagram:
    return ArchitectureDraft.model_validate(
        json.loads((ROOT / "data/samples" / name).read_text(encoding="utf-8"))).diagram


def overlap(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def inside(r, box) -> bool:
    x, y, w, h = r
    return box.x <= x and box.y <= y and x + w <= box.x + box.w and y + h <= box.y + box.h


def box(lay, id):
    return next(b for b in lay.boxes if b.id == id)


def assert_no_overlap(lay):
    slots = [(p.id, slot_of(p)) for p in lay.nodes]
    for i, (a, ra) in enumerate(slots):
        for b, rb in slots[i + 1:]:
            assert not overlap(ra, rb), f"{a} 와 {b} 가 겹침"


def assert_edges_connected(lay):
    ids = {p.id for p in lay.nodes}
    assert len(ids) == len(lay.nodes), "PlacedNode id 중복"
    for e in lay.edges:
        assert e.source in ids and e.target in ids, f"끊어진 선 {e.source}→{e.target}"


# ───────────────────────── 3-Tier 샘플 ─────────────────────────

@pytest.fixture(scope="module")
def tier3():
    d = load("architecture_3tier.json")
    return d, layout(d)


def test_3tier_no_warnings(tier3):
    assert tier3[1].warnings == []


def test_3tier_no_overlap(tier3):
    assert_no_overlap(tier3[1])


def test_3tier_nodes_in_cloud(tier3):
    d, lay = tier3
    cloud = box(lay, "cloud")
    place = {n.id: n.place for n in d.nodes}
    for p in lay.nodes:
        if place[p.node_id] == "outside":
            assert not overlap(slot_of(p), (cloud.x, cloud.y, cloud.w, cloud.h)), p.id
        else:
            assert inside(slot_of(p), cloud), p.id
    assert all(inside((b.x, b.y, b.w, b.h), cloud) for b in lay.boxes if b.kind != "cloud")


def test_3tier_each_replicated(tier3):
    d, lay = tier3
    for n in d.nodes:
        placed = [p for p in lay.nodes if p.node_id == n.id]
        if n.zone == "each":
            assert sorted(p.id for p in placed) == sorted(f"{n.id}@{z}" for z in d.zones)
        else:
            assert [p.id for p in placed] == [n.id]


def test_3tier_subnet_nodes_in_their_cell(tier3):
    d, lay = tier3
    by = {n.id: n for n in d.nodes}
    for p in lay.nodes:
        n = by[p.node_id]
        if n.place != "subnet" or n.zone == "span":
            continue
        z = p.id.split("@")[1] if n.zone == "each" else n.zone
        assert inside(slot_of(p), box(lay, f"subnet:{n.tier}:{z}")), p.id
        assert inside(slot_of(p), box(lay, f"az:{z}")), p.id


def test_3tier_span_between_azs(tier3):
    d, lay = tier3
    az1, az2 = box(lay, "az:AZ1"), box(lay, "az:AZ2")
    for nid in ("alb_web", "alb_app"):
        p = next(p for p in lay.nodes if p.id == nid)
        cx = p.x + p.w / 2
        assert az1.x + az1.w < cx < az2.x
        assert inside(slot_of(p), box(lay, "vpc"))


def test_3tier_boxes(tier3):
    d, lay = tier3
    kinds = [b.kind for b in lay.boxes]
    assert kinds.count("az") == 2 and kinds.count("subnet") == 8 and kinds.count("vpc") == 1
    pub = box(lay, "subnet:public:AZ1")
    assert pub.style == "public" and box(lay, "subnet:db:AZ2").style == "private"
    vpc = box(lay, "vpc")
    assert all(inside((b.x, b.y, b.w, b.h), vpc) for b in lay.boxes if b.kind in ("az", "subnet"))
    # region column 은 VPC 왼쪽
    rb = box(lay, "region_band")
    assert rb.x + rb.w <= vpc.x


def test_3tier_groups_wrap_members(tier3):
    d, lay = tier3
    for g in d.groups:
        gb = box(lay, f"group:{g.id}")
        assert gb.style == g.kind
        members = [p for p in lay.nodes if next(n for n in d.nodes if n.id == p.node_id).group == g.id]
        assert len(members) == 2   # each × 2 AZ
        for p in members:
            assert inside(slot_of(p), gb)


def test_3tier_region_aligned(tier3):
    _, lay = tier3
    y = {p.id: p.y for p in lay.nodes}
    assert y["s3"] == pytest.approx((y["web@AZ1"] + y["web@AZ2"]) / 2)
    assert y["efs"] == pytest.approx((y["app@AZ1"] + y["app@AZ2"]) / 2)
    assert y["cache"] > y["efs"]   # 같은 높이를 원하지만 겹치지 않게 밀림


def test_3tier_edges(tier3):
    _, lay = tier3
    assert_edges_connected(lay)
    pairs = {(e.source, e.target) for e in lay.edges}
    # each ↔ each: 같은 AZ 끼리만
    assert ("app@AZ1", "rds@AZ1") in pairs and ("app@AZ2", "rds@AZ2") in pairs
    assert ("app@AZ1", "rds@AZ2") not in pairs
    # 한쪽만 each: 전부 연결
    assert {("alb_web", "web@AZ1"), ("alb_web", "web@AZ2"), ("web@AZ1", "s3"), ("web@AZ2", "s3")} <= pairs
    # 1:1
    assert ("users", "r53") in pairs
    assert len(lay.edges) == 20


def test_3tier_canvas_contains_everything(tier3):
    _, lay = tier3
    for p in lay.nodes:
        x, y, w, h = slot_of(p)
        assert x >= 0 and y >= 0 and x + w <= lay.width and y + h <= lay.height
    for b in lay.boxes:
        assert b.x + b.w <= lay.width and b.y + b.h <= lay.height


def test_outside_on_top(tier3):
    d, lay = tier3
    cloud = box(lay, "cloud")
    for p in lay.nodes:
        if p.node_id in ("users", "dto"):
            assert p.y + p.h < cloud.y


# ───────────────────────── 서버리스 샘플 ─────────────────────────

@pytest.fixture(scope="module")
def serverless():
    d = load("architecture_serverless.json")
    return d, layout(d)


def test_serverless_no_vpc(serverless):
    d, lay = serverless
    assert d.zones == [] and d.tiers == []
    assert not any(b.kind in ("vpc", "az", "subnet") for b in lay.boxes)
    assert lay.warnings == []
    assert_no_overlap(lay)
    assert_edges_connected(lay)
    cloud = box(lay, "cloud")
    for p in lay.nodes:
        if p.node_id != "users":
            assert inside(slot_of(p), cloud), p.id


def test_serverless_layers_left_to_right(serverless):
    _, lay = serverless
    x = {p.id: p.x for p in lay.nodes}
    assert x["apigw"] < x["fn"] < x["queue"] < x["worker"] < x["uploads"]
    assert x["worker"] < x["ddb"]   # fn→ddb, worker→ddb: 가장 긴 경로 기준


# ───────────────────────── 검증 · warnings ─────────────────────────

ZERO = dict(spec="", engine="", multi_az=False, count=0, hours_per_month=730, storage_gb=0, data_gb_month=0,
            requests_million_month=0, write_requests_million_month=0, vcpu=0, memory_gb=0, duration_ms=0, lcu=0)


def N(id, place="region", tier="", zone="", group="", icon="ec2"):
    return Node(id=id, icon=icon, label=id, role="", place=place, tier=tier, zone=zone, group=group,
                pricing=Pricing(kind="free", **ZERO))


def vpc_diagram(nodes, edges=(), groups=(), zones=("AZ1", "AZ2")):
    return Diagram(zones=list(zones), tiers=[Tier(id="pub", label="Public", kind="public"),
                                             Tier(id="app", label="App", kind="private")],
                   nodes=nodes, groups=list(groups), edges=[Edge(source=s, target=t, label="") for s, t in edges])


def test_missing_edge_node_removed():
    lay = layout(vpc_diagram([N("a", "subnet", "app", "each"), N("s3")], [("a", "s3"), ("a", "ghost")]))
    assert len(lay.edges) == 2
    assert any("ghost" in w for w in lay.warnings)


def test_bad_tier_or_zone_moved_to_region():
    lay = layout(vpc_diagram([N("a", "subnet", "nope", "AZ1"), N("b", "subnet", "app", "AZ9"),
                              N("c", "subnet", "app", "AZ1")]))
    assert len(lay.warnings) == 2
    rb = box(lay, "region_band")
    p = {p.id: p for p in lay.nodes}
    assert inside(slot_of(p["a"]), rb) and inside(slot_of(p["b"]), rb)
    assert inside(slot_of(p["c"]), box(lay, "subnet:app:AZ1"))


def test_duplicate_ids_renamed():
    lay = layout(vpc_diagram([N("a"), N("a"), N("a")]))
    assert sorted(p.id for p in lay.nodes) == ["a", "a_2", "a_3"]
    assert sum("중복" in w for w in lay.warnings) == 2
    assert_no_overlap(lay)


def test_vpc_place_without_vpc_moves_to_region():
    d = Diagram(zones=[], tiers=[], nodes=[N("igw", "vpc"), N("x", "subnet", "app", "AZ1")], groups=[], edges=[])
    lay = layout(d)
    assert len(lay.warnings) == 2
    assert not any(b.kind == "vpc" for b in lay.boxes)


def test_unknown_group_ignored():
    lay = layout(vpc_diagram([N("a", "subnet", "app", "each", group="nope")]))
    assert any("nope" in w for w in lay.warnings)
    assert not any(b.kind == "group" for b in lay.boxes)


def test_three_azs_crowded_cell_and_multiple_spans():
    nodes = [N("lb1", "subnet", "pub", "span"), N("lb2", "subnet", "pub", "span"),
             N("w", "subnet", "app", "each", group="g"), N("x", "subnet", "app", "b"), N("y", "subnet", "app", "b"),
             N("igw", "vpc"), N("s3"), N("ddb")]
    lay = layout(vpc_diagram(nodes, [("lb1", "w"), ("w", "s3"), ("w", "ddb")],
                             [Group(id="g", kind="auto_scaling", label="ASG")], zones=("a", "b", "c")))
    assert lay.warnings == []
    assert_no_overlap(lay)
    assert_edges_connected(lay)
    assert len([p for p in lay.nodes if p.node_id == "w"]) == 3
    assert len([e for e in lay.edges if e.source == "lb1"]) == 3
    vpc = box(lay, "vpc")
    for p in lay.nodes:
        if p.node_id not in ("s3", "ddb"):
            assert inside(slot_of(p), vpc), p.id


def test_cycle_in_serverless_does_not_hang():
    d = Diagram(zones=[], tiers=[], nodes=[N("a"), N("b"), N("c")], groups=[],
                edges=[Edge(source="a", target="b", label=""), Edge(source="b", target="c", label=""),
                       Edge(source="c", target="a", label="")])
    lay = layout(d)
    assert_no_overlap(lay)
    assert len(lay.edges) == 3
