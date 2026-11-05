"""AWS 공식 Price List(요금표 원본, 인증 불필요) → 비용 계산용 가격표 data/pricing/<region>.json

AWS Pricing Calculator 와 같은 원본 데이터다. 서비스별 CSV 를 받아서 우리가 계산에 쓰는 항목만 남긴다.
EC2 원본은 200MB 라 한 번만 받아 data/raw/pricing 에 캐시한다 (git 미포함). 결과 JSON 만 레포에 넣는다.

    uv run python build_pricing.py                    # 서울(ap-northeast-2)
    uv run python build_pricing.py --region us-east-1
    uv run python build_pricing.py --refresh          # 캐시 무시하고 다시 받기
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).parent
BASE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws"
REGIONAL = ["AmazonEC2", "AmazonRDS", "AmazonElastiCache", "AWSELB", "AmazonS3", "AWSLambda",
            "AmazonDynamoDB", "AmazonECS", "AmazonEKS", "AmazonVPC", "AWSDataTransfer", "AmazonEFS",
            "awswaf", "AmazonApiGateway", "AWSQueueService"]
GLOBAL = ["AmazonCloudFront", "AmazonRoute53"]  # 리전 파일에 전송 요금이 없어서 전체 파일 사용
CF_ZONE = {"ap-northeast-2": "AP", "ap-northeast-1": "JP", "us-east-1": "US", "eu-west-1": "EU"}


def fetch(service: str, region: str | None, refresh: bool) -> Path:
    raw = ROOT / "data" / "raw" / "pricing" / (region or "global")
    raw.mkdir(parents=True, exist_ok=True)
    path = raw / f"{service}.csv"
    if path.exists() and not refresh:
        return path
    url = f"{BASE}/{service}/current/{region + '/' if region else ''}index.csv"
    print(f"  받는 중 {service} …", flush=True)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        tmp = path.with_suffix(".part")
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        tmp.replace(path)
    return path


def rows(path: Path):
    """OnDemand 요금 행만. CSV 앞 5줄은 메타데이터."""
    with path.open(encoding="utf-8", newline="") as f:
        for _ in range(5):
            next(f)
        for r in csv.DictReader(f):
            if r["TermType"] == "OnDemand":
                r["price"] = float(r["PricePerUnit"] or 0)
                yield r


def tiers(rs) -> list[list]:
    """[(시작 GB, 끝 GB 또는 None, 단가)] 시작 순 정렬."""
    out = sorted({(float(r["StartingRange"] or 0),
                   None if r["EndingRange"] in ("", "Inf") else float(r["EndingRange"]),
                   r["price"]) for r in rs})
    return [list(t) for t in out]


def build(region: str, refresh: bool) -> dict:
    p = {s: fetch(s, region, refresh) for s in REGIONAL}
    p.update({s: fetch(s, None, refresh) for s in GLOBAL})
    t: dict = {"region": region, "currency": "USD", "hours_per_month": 730,
               "source": f"{BASE}/<service>/current/{region}/index.csv (AWS Price List, OnDemand)",
               "built": date.today().isoformat()}

    # EC2 인스턴스 (Linux, 공유 테넌시, 소프트웨어 없음) + EBS + NAT
    ec2, ebs, nat = {}, {}, {}
    for r in rows(p["AmazonEC2"]):
        fam, ut = r["Product Family"], r["usageType"]
        if (fam == "Compute Instance" and r["Operating System"] == "Linux" and r["Tenancy"] == "Shared"
                and r["Pre Installed S/W"] == "NA" and r["CapacityStatus"] == "Used"
                and r["License Model"] == "No License required" and ":" in ut and "BoxUsage" in ut):
            ec2[r["Instance Type"]] = {"hourly": r["price"], "vcpu": r["vCPU"], "memory": r["Memory"],
                                       "arch": "arm64" if "Graviton" in r["Physical Processor"] or "AWS" in r["Physical Processor"] else "x86_64"}
        elif fam == "Storage" and "EBS:VolumeUsage" in ut:
            ebs[{"gp2": "gp2", "gp3": "gp3", "piops": "io1", "io2": "io2", "st1": "st1", "sc1": "sc1"}
                .get(ut.rsplit(".", 1)[-1] if "." in ut else "standard", "standard")] = r["price"]
        elif ut.endswith("EBS:VolumeP-IOPS.gp3"):
            ebs["gp3_iops"] = r["price"]
        elif ut.endswith("EBS:VolumeP-Throughput.gp3"):
            ebs["gp3_throughput_gibps"] = r["price"]
        elif fam == "NAT Gateway" and "Regional" not in ut:
            nat["hour" if ut.endswith("Hours") else "gb"] = r["price"]
    t["ec2"], t["ebs_gb_month"], t["nat_gateway"] = ec2, ebs, nat

    # RDS / Aurora 인스턴스 + 스토리지
    rds, rds_storage, aurora_storage = {}, {}, {}
    engines = {"MySQL", "PostgreSQL", "MariaDB", "Aurora MySQL", "Aurora PostgreSQL"}
    for r in rows(p["AmazonRDS"]):
        fam, ut, eng, dep = r["Product Family"], r["usageType"], r["Database Engine"], r["Deployment Option"]
        if fam == "Database Instance" and eng in engines and r["License Model"] == "No license required":
            if "ExtendedSupport" in ut or "UnusedBox" in ut:
                continue
            if eng.startswith("Aurora"):
                mode = "io_optimized" if "IOOptimized" in ut else "standard" if "InstanceUsage:" in ut else None
                if mode:
                    rds.setdefault(eng, {}).setdefault(mode, {})[r["Instance Type"]] = r["price"]
            else:
                mode = {"Single-AZ": "single_az", "Multi-AZ": "multi_az",
                        "Multi-AZ (readable standbys)": "multi_az_cluster"}.get(dep)
                if mode and ("InstanceUsage:" in ut or "Multi-AZUsage:" in ut or "Multi-AZClusterUsage:" in ut):
                    rds.setdefault(eng, {}).setdefault(mode, {})[r["Instance Type"]] = r["price"]
        elif fam == "Database Storage" and eng in ("Any", "MySQL"):
            vt = r["Volume Type"]
            if "Aurora" in vt:
                aurora_storage["io_optimized" if "IO Optimized" in vt else "standard"] = r["price"]
            else:
                key = {"General Purpose": "gp2", "General Purpose-GP3": "gp3", "Provisioned IOPS": "io1",
                       "Provisioned IOPS-IO2": "io2", "Magnetic": "magnetic"}.get(vt)
                mode = {"Single-AZ": "single_az", "Multi-AZ": "multi_az",
                        "Multi-AZ (readable standbys)": "multi_az_cluster"}.get(dep)
                if key and mode:
                    rds_storage.setdefault(mode, {})[key] = r["price"]
        elif fam == "System Operation" and ut.endswith("Aurora:StorageIOUsage"):
            aurora_storage["io_per_million"] = r["price"] * 1e6
    t["rds"], t["rds_storage_gb_month"], t["aurora_storage"] = rds, rds_storage, aurora_storage

    # ElastiCache 노드 + Serverless
    ec, ecs_less = {}, {}
    for r in rows(p["AmazonElastiCache"]):
        ut = r["usageType"]
        if r["Product Family"] == "Cache Instance" and ut.split(":")[0].endswith("-NodeUsage") \
                and "Outpost" not in ut and "ExtendedSupport" not in ut and "SyncDurability" not in ut:
            ec.setdefault(r["Cache Engine"], {})[r["Instance Type"]] = r["price"]
        elif r["Product Family"] == "ElastiCache Serverless" and r["Cache Engine"] == "Valkey":
            ecs_less["gb_hour" if "CachedData" in ut else "ecpu_million" if "ProcessingUnits" in ut else "backup"] = \
                r["price"] * (1e6 if "ProcessingUnits" in ut else 1)
    t["elasticache"], t["elasticache_serverless_valkey"] = ec, ecs_less

    lb = {}
    for r in rows(p["AWSELB"]):
        kind = {"Load Balancer-Application": "alb", "Load Balancer-Network": "nlb"}.get(r["Product Family"])
        ut = r["usageType"]
        if kind and "Outposts" not in ut and "Reserved" not in ut and "TS-" not in ut:
            lb.setdefault(kind, {})["lcu_hour" if "LCU" in ut else "hour"] = r["price"]
    t["load_balancer"] = lb

    s3 = {}
    cls = {"Standard": "standard", "Standard - Infrequent Access": "standard_ia",
           "One Zone - Infrequent Access": "onezone_ia", "Glacier Instant Retrieval": "glacier_ir",
           "Amazon Glacier": "glacier_flexible", "Intelligent-Tiering Frequent Access": "intelligent_tiering"}
    for r in rows(p["AmazonS3"]):
        if r["Product Family"] == "Storage" and r["Volume Type"] in cls and float(r["StartingRange"] or 0) == 0:
            s3[cls[r["Volume Type"]]] = r["price"]
    t["s3_gb_month"] = s3

    lam = {}
    for r in rows(p["AWSLambda"]):
        ut = r["usageType"]
        if ut.startswith("Global") or float(r["StartingRange"] or 0) != 0:
            continue
        if ut.endswith("Lambda-GB-Second"):
            lam["gb_second_x86"] = r["price"]
        elif ut.endswith("Lambda-GB-Second-ARM"):
            lam["gb_second_arm"] = r["price"]
        elif ut.endswith("-Request") and r["Group"] == "AWS-Lambda-Requests":
            lam["request"] = r["price"]
    t["lambda"] = lam

    ddb = {}
    for r in rows(p["AmazonDynamoDB"]):
        ut = r["usageType"]
        key = {"ReadRequestUnits": "read_request_unit", "WriteRequestUnits": "write_request_unit",
               "ReadCapacityUnit-Hrs": "rcu_hour", "WriteCapacityUnit-Hrs": "wcu_hour",
               "TimedStorage-ByteHrs": "storage_gb_month"}.get(ut.split("-", 1)[1] if "-" in ut else ut)
        if key and r["price"] > 0:
            ddb[key] = r["price"]
    t["dynamodb"] = ddb

    far = {}
    for r in rows(p["AmazonECS"]):
        ut = r["usageType"]
        if "Windows" in ut or "Fargate" not in ut:
            continue
        arch = "arm" if "ARM" in ut else "x86"
        if "vCPU-Hours" in ut:
            far[f"vcpu_hour_{arch}"] = r["price"]
        elif ut.endswith("GB-Hours") and "Ephemeral" not in ut:
            far[f"gb_hour_{arch}"] = r["price"]
    t["fargate"] = far

    for r in rows(p["AmazonEKS"]):
        if r["usageType"].endswith("AmazonEKS-Hours:perCluster"):
            t["eks_cluster_hour"] = r["price"]

    vpce = {}
    for r in rows(p["AmazonVPC"]):
        ut = r["usageType"]
        if ut.endswith("VpcEndpoint-Hours"):
            vpce["hour"] = r["price"]
        elif ut.endswith("VpcEndpoint-Bytes"):
            vpce["gb"] = max(vpce.get("gb", 0), r["price"])  # 첫 구간(가장 비싼 단가)
    t["vpc_interface_endpoint"] = vpce

    t["data_transfer_out_tiers"] = tiers(r for r in rows(p["AWSDataTransfer"])
                                         if r["usageType"].endswith("DataTransfer-Out-Bytes")
                                         and r["To Location"] == "External" and r["price"] > 0)

    efs = {}
    for r in rows(p["AmazonEFS"]):
        key = {"General Purpose": "standard", "Infrequent Access": "ia", "One Zone-General Purpose": "one_zone"} \
            .get(r["Storage Class"])
        if key and "TimedStorage" in r["usageType"]:
            efs[key] = r["price"]
    t["efs_gb_month"] = efs

    waf = {}
    for r in rows(p["awswaf"]):
        ut = r["usageType"].split("-", 1)[-1]
        if ut in ("WebACL", "Rule", "Request"):
            waf[{"WebACL": "web_acl_month", "Rule": "rule_month", "Request": "request"}[ut]] = r["price"]
    t["waf"] = waf

    apigw = {}
    for r in rows(p["AmazonApiGateway"]):
        ut, start = r["usageType"], float(r["StartingRange"] or 0)
        if start != 0:
            continue
        if ut.endswith("ApiGatewayHttpRequest"):
            apigw["http_per_million"] = r["price"] * 1e6
        elif ut.endswith("ApiGatewayRequest"):
            apigw["rest_per_million"] = r["price"] * 1e6
    t["api_gateway"] = apigw

    for r in rows(p["AWSQueueService"]):
        if r["usageType"].endswith("-Requests-Tier1") and float(r["StartingRange"] or 0) == 0:
            t["sqs_per_million"] = r["price"] * 1e6

    zone = CF_ZONE.get(region, "US")
    cf = {"zone": zone, "out_tiers": tiers(r for r in rows(p["AmazonCloudFront"])
                                           if r["usageType"] == f"{zone}-DataTransfer-Out-Bytes")}
    for r in rows(p["AmazonCloudFront"]):
        if r["usageType"] == f"{zone}-Requests-Tier2-HTTPS":
            cf["https_per_10k"] = r["price"] * 1e4
    t["cloudfront"] = cf

    r53 = {}
    for r in rows(p["AmazonRoute53"]):
        if r["usageType"] == "HostedZone" and float(r["StartingRange"] or 0) == 0:
            r53["hosted_zone_month"] = r["price"]
        elif r["usageType"] == "DNS-Queries" and float(r["StartingRange"] or 0) == 0:
            r53["query_per_million"] = r["price"] * 1e6
    t["route53"] = r53
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    t = build(a.region, a.refresh)
    out = ROOT / "data" / "pricing" / f"{a.region}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    t = json.loads(json.dumps(t), parse_float=lambda x: round(float(x), 10))  # 0.39999999 → 0.4
    out.write_text(json.dumps(t, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8", newline="\n")
    print(f"저장: {out} ({out.stat().st_size // 1024} KB) — EC2 {len(t['ec2'])}종, "
          f"RDS 엔진 {len(t['rds'])}개, ElastiCache 엔진 {len(t['elasticache'])}개")
    empty = [k for k, v in t.items() if isinstance(v, dict) and not v]
    if empty:
        print("경고: 비어 있는 항목", empty, file=sys.stderr)


if __name__ == "__main__":
    main()
