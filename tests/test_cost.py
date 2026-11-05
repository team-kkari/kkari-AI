"""cost.estimate — 손계산 값과 비교."""
import json
from pathlib import Path

import pytest

from cost import estimate, load_pricing
from schemas import ArchitectureDraft, Node, Pricing

ROOT = Path(__file__).resolve().parent.parent
P = load_pricing()
H = 730

ZERO = dict(spec="", engine="", multi_az=False, count=0, hours_per_month=H, storage_gb=0, data_gb_month=0,
            requests_million_month=0, write_requests_million_month=0, vcpu=0, memory_gb=0, duration_ms=0, lcu=0)


def node(kind: str, id: str = "x", **p) -> Node:
    return Node(id=id, icon="ec2", label=f"{kind} {id}", role="", place="region", tier="", zone="", group="",
                pricing=Pricing(kind=kind, **{**ZERO, **p}))


def one(n: Node):
    r = estimate([n])
    assert len(r.items) == 1
    return r, r.items[0]


def test_ec2_m7g_large_4():
    # docs: m7g.large 4대 = 0.1003 × 730 × 4 ≈ $292.9
    r, i = one(node("ec2", spec="m7g.large", count=4))
    assert P["ec2"]["m7g.large"]["hourly"] == 0.1003
    assert i.monthly_usd == pytest.approx(0.1003 * 730 * 4, abs=0.01) == pytest.approx(292.88, abs=0.01)
    assert "0.1003" in i.formula and "730h" in i.formula and "4대" in i.formula
    assert r.monthly_total_usd == i.monthly_usd


def test_ec2_with_ebs():
    _, i = one(node("ec2", spec="m7g.large", count=4, storage_gb=30))
    assert i.monthly_usd == pytest.approx(0.1003 * 730 * 4 + 30 * 4 * 0.0912, abs=0.01)


def test_nat_2_with_300gb():
    _, i = one(node("nat_gateway", count=2, data_gb_month=300))
    assert i.monthly_usd == pytest.approx(0.059 * 730 * 2 + 300 * 0.059, abs=0.01) == pytest.approx(103.84, abs=0.01)


def test_rds_multi_az():
    _, i = one(node("rds", spec="db.r6g.xlarge", engine="MySQL", multi_az=True, count=1, storage_gb=500))
    hourly = P["rds"]["MySQL"]["multi_az"]["db.r6g.xlarge"]
    gb = P["rds_storage_gb_month"]["multi_az"]["gp3"]
    assert i.monthly_usd == pytest.approx(hourly * 730 + 500 * gb, abs=0.01)
    assert "Multi-AZ" in i.formula


def test_rds_single_az_cheaper():
    _, multi = one(node("rds", spec="db.r6g.large", engine="PostgreSQL", multi_az=True, count=1))
    _, single = one(node("rds", spec="db.r6g.large", engine="PostgreSQL", multi_az=False, count=1))
    assert single.monthly_usd < multi.monthly_usd


def test_aurora():
    _, i = one(node("aurora", spec="db.r6g.large", engine="Aurora MySQL", count=2, storage_gb=100,
                    requests_million_month=50))
    h = P["rds"]["Aurora MySQL"]["standard"]["db.r6g.large"]
    assert i.monthly_usd == pytest.approx(h * 730 * 2 + 100 * 0.12 + 50 * 0.24, abs=0.01)


def test_alb():
    _, i = one(node("alb", count=1, lcu=5))
    assert i.monthly_usd == pytest.approx((0.0225 + 5 * 0.008) * 730, abs=0.01)


def test_elasticache_default_engine_valkey():
    _, i = one(node("elasticache", spec="cache.r7g.large", count=2))
    assert i.monthly_usd == pytest.approx(P["elasticache"]["Valkey"]["cache.r7g.large"] * 730 * 2, abs=0.01)


def test_data_transfer_tiers():
    # 첫 10TB(10240GB) 0.126, 그다음 0.122
    _, i = one(node("data_transfer_out", data_gb_month=12240))
    assert i.monthly_usd == pytest.approx(10240 * 0.126 + 2000 * 0.122, abs=0.01)


def test_lambda():
    _, i = one(node("lambda", spec="arm", requests_million_month=10, memory_gb=0.5, duration_ms=120))
    req = 10e6
    expect = req * 2e-7 + req * 0.12 * 0.5 * P["lambda"]["gb_second_arm"]
    assert i.monthly_usd == pytest.approx(expect, abs=0.01)


def test_fargate():
    _, i = one(node("fargate", spec="x86", count=3, vcpu=1, memory_gb=2))
    f = P["fargate"]
    assert i.monthly_usd == pytest.approx(3 * 730 * (f["vcpu_hour_x86"] + 2 * f["gb_hour_x86"]), abs=0.01)


def test_cloudfront():
    _, i = one(node("cloudfront", data_gb_month=2000, requests_million_month=30))
    assert i.monthly_usd == pytest.approx(2000 * 0.12 + 30 * 100 * 0.012, abs=0.01)


def test_free_is_zero():
    r, i = one(node("free"))
    assert i.monthly_usd == 0 and r.unpriced == []


@pytest.mark.parametrize("n", [
    node("ec2", spec="m7g.lrage", count=4),                                    # 오타
    node("ec2", spec="m99.large", count=1),                                    # 미출시
    node("rds", spec="db.r6g.large", engine="Oracle", count=1),                # 엔진 없음
    node("elasticache", spec="cache.zz.large", engine="Valkey", count=1),
    node("s3", spec="deep_freeze", storage_gb=10),
    node("unpriced"),
])
def test_unknown_is_none_and_unpriced(n):
    r, i = one(n)
    assert i.monthly_usd is None
    assert r.unpriced == [n.label]
    assert r.monthly_total_usd == 0
    assert "추정 불가" in i.formula


def test_unpriced_not_summed_as_zero():
    r = estimate([node("ec2", id="a", spec="m7g.large", count=1), node("ec2", id="b", spec="nope", count=1)])
    assert r.monthly_total_usd == pytest.approx(0.1003 * 730, abs=0.01)
    assert r.unpriced == ["ec2 b"]


def test_assumptions():
    r = estimate([])
    text = " ".join(r.assumptions)
    assert "서울" in text and "온디맨드" in text
    assert "RI·Savings Plans·프리티어 미적용" in r.assumptions
    assert f"가격표 생성일 {P['built']}" in r.assumptions


def test_sample_3tier_total():
    d = ArchitectureDraft.model_validate(json.loads((ROOT / "data/samples/architecture_3tier.json").read_text("utf-8")))
    r = estimate(d.diagram.nodes)
    assert r.unpriced == []
    assert {i.node_id for i in r.items} == {n.id for n in d.diagram.nodes}
    assert r.monthly_total_usd == pytest.approx(sum(i.monthly_usd for i in r.items), abs=0.01)
    assert 2000 < r.monthly_total_usd < 4000
