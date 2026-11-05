"""[A 파트] 구성요소 목록 → 월 비용. 가격표: data/pricing/<region>.json (build_pricing.py 로 생성)

    from cost import estimate
    report = estimate(nodes)          # nodes: list[schemas.Node]  →  schemas.CostReport

규칙
- 가격은 전부 가격표에서 읽는다. 가격표에 없는 인스턴스 타입·엔진·spec 은 가까운 값으로 바꾸지 않고
  monthly_usd=None + unpriced 에 넣는다 (합계에 0 으로 더하지 않음).
- CostItem.formula 에 사람이 읽을 수 있는 계산식을 남긴다.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Callable

from schemas import CostItem, CostReport, Node, Pricing

PRICING_DIR = Path(__file__).parent / "data" / "pricing"
REGION_NAMES = {"ap-northeast-2": "서울"}


class Unpriced(Exception):
    """가격표에 없는 항목. 메시지가 그대로 formula 에 들어간다."""


@lru_cache(maxsize=None)
def load_pricing(region: str = "ap-northeast-2") -> dict:
    return json.loads((PRICING_DIR / f"{region}.json").read_text(encoding="utf-8"))


# ───────────────────────── 포맷 도우미 ─────────────────────────

def _n(x: float) -> str:
    """숫자를 짧게: 730.0 → '730', 0.1003 → '0.1003', 1.355e-07 → '1.355e-07'."""
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    if abs(x) < 1e-4:
        return f"{x:.4g}"
    return f"{x:.6g}"


def _get(table: dict, key: str, what: str) -> float:
    if key not in table:
        raise Unpriced(f"가격표에 없음: {what} '{key}'")
    v = table[key]
    if isinstance(v, dict):  # ec2: {"hourly": ...}
        v = v["hourly"]
    return float(v)


def _tiered(gb: float, tiers: list[list]) -> tuple[float, str]:
    """구간별 누진 요금. tiers = [[시작GB, 끝GB|None, $/GB], ...]"""
    total, parts = 0.0, []
    for start, end, price in tiers:
        if gb <= start:
            break
        used = (min(gb, end) if end is not None else gb) - start
        total += used * price
        parts.append(f"{_n(used)}GB × {_n(price)}")
    return total, " + ".join(parts) if parts else "0GB"


# ───────────────────────── kind 별 계산 (월 $, 계산식) ─────────────────────────

Calc = Callable[[Pricing, dict], tuple[float, str]]


def _ec2(p: Pricing, t: dict) -> tuple[float, str]:
    h = _get(t["ec2"], p.spec, "EC2 인스턴스 타입")
    gp3 = t["ebs_gb_month"]["gp3"]
    inst = h * p.hours_per_month * p.count
    ebs = p.storage_gb * p.count * gp3
    f = f"{_n(h)} $/h × {_n(p.hours_per_month)}h × {_n(p.count)}대"
    if p.storage_gb:
        f += f" + EBS gp3 {_n(p.storage_gb)}GB × {_n(p.count)}대 × {_n(gp3)} $/GB"
    return inst + ebs, f


def _rds(p: Pricing, t: dict) -> tuple[float, str]:
    if p.engine not in t["rds"]:
        raise Unpriced(f"가격표에 없음: RDS 엔진 '{p.engine}'")
    mode = "multi_az" if p.multi_az else "single_az"
    h = _get(t["rds"][p.engine][mode], p.spec, f"RDS {p.engine} {mode} 인스턴스 타입")
    st = t["rds_storage_gb_month"][mode]["gp3"]
    label = "Multi-AZ" if p.multi_az else "Single-AZ"
    f = f"{p.engine} {label} {_n(h)} $/h × {_n(p.hours_per_month)}h × {_n(p.count)}대"
    if p.storage_gb:
        f += f" + 스토리지 gp3 {_n(p.storage_gb)}GB × {_n(st)} $/GB"
    return h * p.hours_per_month * p.count + p.storage_gb * st, f


def _aurora(p: Pricing, t: dict) -> tuple[float, str]:
    if p.engine not in t["rds"] or "standard" not in t["rds"][p.engine]:
        raise Unpriced(f"가격표에 없음: Aurora 엔진 '{p.engine}'")
    h = _get(t["rds"][p.engine]["standard"], p.spec, f"{p.engine} 인스턴스 타입")
    a = t["aurora_storage"]
    f = f"{p.engine} {_n(h)} $/h × {_n(p.hours_per_month)}h × {_n(p.count)}대"
    if p.storage_gb:
        f += f" + 스토리지 {_n(p.storage_gb)}GB × {_n(a['standard'])} $/GB"
    if p.requests_million_month:
        f += f" + I/O {_n(p.requests_million_month)}백만 × {_n(a['io_per_million'])} $"
    return (h * p.hours_per_month * p.count + p.storage_gb * a["standard"]
            + p.requests_million_month * a["io_per_million"]), f


def _elasticache(p: Pricing, t: dict) -> tuple[float, str]:
    engine = p.engine or "Valkey"
    if engine not in t["elasticache"]:
        raise Unpriced(f"가격표에 없음: ElastiCache 엔진 '{engine}'")
    h = _get(t["elasticache"][engine], p.spec, f"ElastiCache {engine} 노드 타입")
    return (h * p.hours_per_month * p.count,
            f"{engine} {_n(h)} $/h × {_n(p.hours_per_month)}h × {_n(p.count)}노드")


def _elasticache_serverless(p: Pricing, t: dict) -> tuple[float, str]:
    s = t["elasticache_serverless_valkey"]
    v = p.storage_gb * s["gb_hour"] * p.hours_per_month + p.requests_million_month * s["ecpu_million"]
    return v, (f"{_n(p.storage_gb)}GB × {_n(s['gb_hour'])} $/GB·h × {_n(p.hours_per_month)}h"
               f" + ECPU {_n(p.requests_million_month)}백만 × {_n(s['ecpu_million'])} $")


def _lb(kind: str) -> Calc:
    def calc(p: Pricing, t: dict) -> tuple[float, str]:
        lb = t["load_balancer"][kind]
        v = (lb["hour"] + p.lcu * lb["lcu_hour"]) * p.hours_per_month * p.count
        return v, (f"({_n(lb['hour'])} $/h + {_n(p.lcu)}LCU × {_n(lb['lcu_hour'])} $/LCU·h)"
                   f" × {_n(p.hours_per_month)}h × {_n(p.count)}개")
    return calc


def _hour_plus_gb(key: str) -> Calc:
    def calc(p: Pricing, t: dict) -> tuple[float, str]:
        x = t[key]
        v = x["hour"] * p.hours_per_month * p.count + p.data_gb_month * x["gb"]
        return v, (f"{_n(x['hour'])} $/h × {_n(p.hours_per_month)}h × {_n(p.count)}개"
                   f" + 처리 {_n(p.data_gb_month)}GB × {_n(x['gb'])} $/GB")
    return calc


def _data_transfer_out(p: Pricing, t: dict) -> tuple[float, str]:
    v, f = _tiered(p.data_gb_month, t["data_transfer_out_tiers"])
    return v, f"인터넷 송신 {f} ($/GB, 구간 누진)"


def _storage(key: str, default: str, name: str) -> Calc:
    def calc(p: Pricing, t: dict) -> tuple[float, str]:
        spec = p.spec or default
        price = _get(t[key], spec, f"{name} 스토리지 클래스")
        return p.storage_gb * price, f"{name} {spec} {_n(p.storage_gb)}GB × {_n(price)} $/GB"
    return calc


def _arch(p: Pricing) -> str:
    spec = p.spec or "x86"
    if spec not in ("x86", "arm"):
        raise Unpriced(f"가격표에 없음: 아키텍처 '{p.spec}' (x86|arm)")
    return spec


def _lambda(p: Pricing, t: dict) -> tuple[float, str]:
    arch = _arch(p)
    lam = t["lambda"]
    gbs_price = lam[f"gb_second_{arch}"]
    req = p.requests_million_month * 1e6
    gb_sec = req * (p.duration_ms / 1000) * p.memory_gb
    v = req * lam["request"] + gb_sec * gbs_price
    return v, (f"요청 {_n(p.requests_million_month)}백만 × {_n(lam['request'])} $"
               f" + {_n(gb_sec)} GB·s ({_n(p.duration_ms)}ms × {_n(p.memory_gb)}GB) × {_n(gbs_price)} $ ({arch})")


def _fargate(p: Pricing, t: dict) -> tuple[float, str]:
    arch = _arch(p)
    fg = t["fargate"]
    vc, gb = fg[f"vcpu_hour_{arch}"], fg[f"gb_hour_{arch}"]
    v = p.count * p.hours_per_month * (p.vcpu * vc + p.memory_gb * gb)
    return v, (f"{_n(p.count)}태스크 × {_n(p.hours_per_month)}h × "
               f"({_n(p.vcpu)}vCPU × {_n(vc)} + {_n(p.memory_gb)}GB × {_n(gb)}) $ ({arch})")


def _eks(p: Pricing, t: dict) -> tuple[float, str]:
    h = t["eks_cluster_hour"]
    return h * p.hours_per_month * p.count, f"{_n(h)} $/h × {_n(p.hours_per_month)}h × {_n(p.count)}클러스터"


def _dynamodb(p: Pricing, t: dict) -> tuple[float, str]:
    d = t["dynamodb"]
    v = (p.requests_million_month * 1e6 * d["read_request_unit"]
         + p.write_requests_million_month * 1e6 * d["write_request_unit"]
         + p.storage_gb * d["storage_gb_month"])
    return v, (f"읽기 {_n(p.requests_million_month)}백만 × {_n(d['read_request_unit'])} $"
               f" + 쓰기 {_n(p.write_requests_million_month)}백만 × {_n(d['write_request_unit'])} $"
               f" + {_n(p.storage_gb)}GB × {_n(d['storage_gb_month'])} $/GB")


def _cloudfront(p: Pricing, t: dict) -> tuple[float, str]:
    c = t["cloudfront"]
    out, f = _tiered(p.data_gb_month, c["out_tiers"])
    req = p.requests_million_month * 100 * c["https_per_10k"]
    return out + req, (f"송신 {f} ($/GB, 구간 누진)"
                       f" + HTTPS {_n(p.requests_million_month)}백만 × 100 × {_n(c['https_per_10k'])} $/1만건")


def _waf(p: Pricing, t: dict) -> tuple[float, str]:
    w = t["waf"]
    v = w["web_acl_month"] * p.count + p.lcu * w["rule_month"] + p.requests_million_month * 1e6 * w["request"]
    return v, (f"Web ACL {_n(p.count)}개 × {_n(w['web_acl_month'])} $ + 규칙 {_n(p.lcu)}개 × {_n(w['rule_month'])} $"
               f" + 요청 {_n(p.requests_million_month)}백만 × {_n(w['request'])} $")


def _route53(p: Pricing, t: dict) -> tuple[float, str]:
    r = t["route53"]
    v = r["hosted_zone_month"] * p.count + p.requests_million_month * r["query_per_million"]
    return v, (f"호스팅 영역 {_n(p.count)}개 × {_n(r['hosted_zone_month'])} $"
               f" + 쿼리 {_n(p.requests_million_month)}백만 × {_n(r['query_per_million'])} $")


def _api_gateway(p: Pricing, t: dict) -> tuple[float, str]:
    spec = p.spec or "http"
    price = _get(t["api_gateway"], f"{spec}_per_million", "API Gateway 유형")
    return p.requests_million_month * price, f"{spec.upper()} API {_n(p.requests_million_month)}백만 × {_n(price)} $"


def _sqs(p: Pricing, t: dict) -> tuple[float, str]:
    price = t["sqs_per_million"]
    return p.requests_million_month * price, f"요청 {_n(p.requests_million_month)}백만 × {_n(price)} $"


CALC: dict[str, Calc] = {
    "ec2": _ec2,
    "rds": _rds,
    "aurora": _aurora,
    "elasticache": _elasticache,
    "elasticache_serverless": _elasticache_serverless,
    "alb": _lb("alb"),
    "nlb": _lb("nlb"),
    "nat_gateway": _hour_plus_gb("nat_gateway"),
    "vpc_interface_endpoint": _hour_plus_gb("vpc_interface_endpoint"),
    "data_transfer_out": _data_transfer_out,
    "s3": _storage("s3_gb_month", "standard", "S3"),
    "efs": _storage("efs_gb_month", "standard", "EFS"),
    "ebs": _storage("ebs_gb_month", "gp3", "EBS"),
    "lambda": _lambda,
    "fargate": _fargate,
    "eks": _eks,
    "dynamodb": _dynamodb,
    "cloudfront": _cloudfront,
    "waf": _waf,
    "route53": _route53,
    "api_gateway": _api_gateway,
    "sqs": _sqs,
}


def _item(n: Node, t: dict) -> CostItem:
    kind = n.pricing.kind
    base = dict(node_id=n.id, label=n.label, kind=kind)
    if kind == "free":
        return CostItem(**base, monthly_usd=0.0, formula="무료")
    if kind == "unpriced" or kind not in CALC:
        return CostItem(**base, monthly_usd=None, formula="가격표에 없음 — 추정 불가")
    try:
        v, f = CALC[kind](n.pricing, t)
    except Unpriced as e:
        return CostItem(**base, monthly_usd=None, formula=f"{e} — 추정 불가")
    return CostItem(**base, monthly_usd=round(v, 2), formula=f"{f} = {v:,.2f} $")


def estimate(nodes: list[Node], region: str = "ap-northeast-2") -> CostReport:
    t = load_pricing(region)
    items = [_item(n, t) for n in nodes]
    priced = [i.monthly_usd for i in items if i.monthly_usd is not None]
    unpriced = [i.label for i in items if i.monthly_usd is None]
    rname = REGION_NAMES.get(region)
    return CostReport(
        monthly_total_usd=round(sum(priced), 2),
        items=items,
        unpriced=unpriced,
        assumptions=[
            f"{rname} 리전({region}) 온디맨드 기준" if rname else f"{region} 리전 온디맨드 기준",
            "RI·Savings Plans·프리티어 미적용",
            f"가격표 생성일 {t.get('built', '?')}",
        ],
    )


if __name__ == "__main__":
    import sys

    from schemas import ArchitectureDraft

    path = sys.argv[1] if len(sys.argv) > 1 else "data/samples/architecture_3tier.json"
    d = ArchitectureDraft.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
    r = estimate(d.diagram.nodes)
    for i in r.items:
        v = "추정 불가" if i.monthly_usd is None else f"${i.monthly_usd:,.2f}"
        print(f"{i.node_id:10} {v:>12}  {i.formula}")
    print(f"\n합계 ${r.monthly_total_usd:,.2f}/월   추정 불가: {r.unpriced or '-'}")
    print(" · ".join(r.assumptions))
