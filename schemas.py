"""/architectures 공통 데이터 형식 (계약).

두 파트가 이 파일만 보고 따로 개발한다. 바꿀 때는 양쪽에 알리고 같이 바꾼다.

  A 파트 (비용·도식): cost.py   estimate(components) -> CostReport
                      layout.py layout(diagram) -> Layout
  B 파트 (AI):        architect.py — Claude 가 Architecture 5개 생성 → A 함수로 비용·좌표 계산 → 순위

Claude 가 만드는 것은 ArchitectureDraft (구성요소 + 도식 배치 정보 + 코멘트).
비용(cost)과 좌표(layout)는 코드가 계산해서 응답에 붙인다 — LLM 이 가격·좌표를 지어내지 않게.

구조화 출력 제약 때문에 Claude 가 채우는 모델은 Optional 을 최소화하고 "안 쓰면 0 / 빈 문자열" 규칙을 쓴다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ───────────────────────── 아이콘 (프론트: AWS Architecture Icons 키 이름 그대로 매핑) ─────────────────────────

Icon = Literal[
    "users", "internet",
    "route53", "cloudfront", "waf", "shield", "acm", "api_gateway",
    "igw", "nat_gateway", "alb", "nlb", "vpc_endpoint",
    "ec2", "auto_scaling", "lambda", "ecs", "fargate", "eks", "ecr",
    "rds", "aurora", "dynamodb", "elasticache", "s3", "efs", "ebs",
    "sqs", "sns", "eventbridge", "step_functions",
    "cloudwatch", "secrets_manager", "kms", "iam", "cognito", "backup",
]

# ───────────────────────── 비용 계산 입력 (A 파트 cost.py 가 해석) ─────────────────────────

PricingKind = Literal[
    "ec2", "rds", "aurora", "elasticache", "elasticache_serverless",
    "alb", "nlb", "nat_gateway", "vpc_interface_endpoint", "data_transfer_out",
    "s3", "efs", "ebs", "lambda", "fargate", "eks", "dynamodb",
    "cloudfront", "waf", "route53", "api_gateway", "sqs",
    "free",       # 비용 없음: IGW, Shield Standard, 보안그룹, ASG 자체, Gateway Endpoint(S3/DynamoDB), IAM
    "unpriced",   # 가격표에 없음 → 비용 "추정 불가"로 표시 (0 으로 합산하지 않음)
]


class Pricing(BaseModel):
    """구성요소 하나의 과금 정보. 해당 kind 에 안 쓰는 필드는 0 / "" / false."""
    kind: PricingKind
    spec: str = Field(description=(
        "ec2/rds/aurora/elasticache: 인스턴스 타입 (m7g.large, db.r6g.large, cache.r7g.large). "
        "ebs/rds 스토리지: 볼륨 타입 gp3|gp2|io1|io2|st1|sc1. s3: standard|standard_ia|onezone_ia|glacier_ir|"
        "glacier_flexible|intelligent_tiering. efs: standard|ia|one_zone. lambda/fargate: x86|arm. "
        "api_gateway: http|rest. 그 외 빈 문자열"))
    engine: str = Field(description="rds: MySQL|PostgreSQL|MariaDB, aurora: Aurora MySQL|Aurora PostgreSQL, "
                                    "elasticache: Valkey|Redis|Memcached. 그 외 빈 문자열")
    multi_az: bool = Field(description="rds: Multi-AZ 배포 여부. 그 외 false")
    count: float = Field(description="전체 개수(모든 AZ 합계): 인스턴스·노드·태스크·NAT·LB·엔드포인트(ENI)·클러스터·"
                                     "Web ACL·호스팅 영역 수. aurora 는 writer+reader 인스턴스 수")
    hours_per_month: float = Field(description="월 가동 시간. 상시 730, 업무시간만이면 약 220")
    storage_gb: float = Field(description="ec2: 인스턴스당 EBS GB, rds/aurora/s3/efs/ebs/dynamodb: 총 저장 GB. 없으면 0")
    data_gb_month: float = Field(description="월 처리/전송 GB: nat_gateway 처리량, data_transfer_out 인터넷 송신, "
                                             "cloudfront 송신, vpc_interface_endpoint 처리량. 없으면 0")
    requests_million_month: float = Field(description="월 요청 수(백만): lambda·api_gateway·sqs·cloudfront·waf·"
                                                      "route53 쿼리·dynamodb 읽기·aurora I/O. 없으면 0")
    write_requests_million_month: float = Field(description="dynamodb 쓰기 요청(백만). 그 외 0")
    vcpu: float = Field(description="fargate 태스크당 vCPU. 그 외 0")
    memory_gb: float = Field(description="fargate 태스크당 메모리 GB, lambda 함수 메모리 GB (예: 0.5). 그 외 0")
    duration_ms: float = Field(description="lambda 평균 실행 시간 ms. 그 외 0")
    lcu: float = Field(description="alb/nlb 평균 LCU (모르면 트래픽 보고 추정, 소규모 1~5). waf: 규칙 수. 그 외 0")


# ───────────────────────── 도식 (A 파트 layout.py 가 좌표 계산) ─────────────────────────


class Tier(BaseModel):
    """VPC 안의 서브넷 층 (위→아래 순서대로 나열)."""
    id: str = Field(description="예: public, web, app, db")
    label: str = Field(description="예: Public subnet, App subnet")
    kind: Literal["public", "private"]


class Node(BaseModel):
    id: str = Field(description="영문 소문자 고유 ID (예: web, rds, nat)")
    icon: Icon
    label: str = Field(description="박스 아래 굵은 글씨 (예: 'EC2 m7g.large', 'Amazon RDS')")
    role: str = Field(description="박스 아래 작은 글씨 (예: 'Web servers', 'Database (primary/standby)'). 없으면 빈 문자열")
    place: Literal["outside", "edge", "region", "vpc", "subnet"] = Field(description=(
        "outside: AWS 바깥(사용자·외부 시스템). edge: 글로벌/엣지 서비스(Route 53, CloudFront, WAF, Shield). "
        "region: VPC 밖 리전 서비스(S3, DynamoDB, Lambda, SQS, API Gateway 등). "
        "vpc: VPC 에 붙지만 서브넷 아님(IGW). subnet: tier+zone 으로 위치 지정"))
    tier: str = Field(description="place=subnet 일 때 Tier.id. 그 외 빈 문자열")
    zone: str = Field(description="place=subnet 일 때: 'AZ1' 같은 특정 AZ, 'each'(AZ마다 하나씩 그림), "
                                  "'span'(AZ 경계에 걸쳐 하나, 예: ALB). 그 외 빈 문자열")
    group: str = Field(description="속한 Group.id (Auto Scaling 그룹 등). 없으면 빈 문자열")
    pricing: Pricing


class Group(BaseModel):
    id: str
    kind: Literal["auto_scaling", "db_subnet_group", "cluster", "other"]
    label: str = Field(description="예: 'Auto Scaling group · Web tier'")


class Edge(BaseModel):
    source: str = Field(description="Node.id")
    target: str = Field(description="Node.id")
    label: str = Field(description="선 위 글씨 (도메인·프로토콜 등). 없으면 빈 문자열")


class Diagram(BaseModel):
    zones: list[str] = Field(description="AZ 이름 목록. VPC 를 안 쓰는 서버리스 구조면 빈 목록")
    tiers: list[Tier] = Field(description="VPC 서브넷 층. VPC 없으면 빈 목록")
    nodes: list[Node]
    groups: list[Group]
    edges: list[Edge]


# ───────────────────────── 고객 제약 (요청 입력) ─────────────────────────


class Constraints(BaseModel):
    """/intake 의 recommend_request.constraints 와 같은 형식. 모두 선택."""
    monthly_budget: float | None = Field(None, description="월 예산 (USD). 0 = 정해진 예산 없음, null = 모름")
    currency: Literal["USD", "KRW"] | None = Field(None, description="monthly_budget 통화. 금액은 USD 로 통일 — 생략하면 USD (KRW 는 하위 호환용, 환산해서 사용)")
    contains_personal_data: bool | None = Field(None, description="개인정보 저장·처리 여부")
    frozen_resources: list[str] | None = Field(None, description="변경하면 안 되는 리소스")
    krw_per_usd: float = Field(1400, description="currency=KRW 일 때 쓰는 환율 (원/달러)")


class ConstraintCheck(BaseModel):
    """설계한 안이 고객 제약을 지켰는지 AI 가 스스로 점검한 결과."""
    constraint: Literal["frozen_resources", "personal_data"]
    satisfied: bool = Field(description="제약을 지켰으면 true")
    detail: str = Field(description="어떻게 지켰는지 또는 무엇을 어겼는지 한 문장 (예: 'RDS prod-db 엔진 버전 유지')")


# ───────────────────────── Claude 가 생성하는 아키텍처 (B 파트) ─────────────────────────


class ArchitectureDraft(BaseModel):
    name: str = Field(description="아키텍처 이름 (예: 'Graviton + Aurora Serverless 전환')")
    concept: str = Field(description="한 줄 콘셉트")
    easy_explanation: str = Field(description="비전공자·주니어도 이해할 수 있는 2~3문장 설명")
    pros: list[str] = Field(description="장점 코멘트 3~5개, 각 한 문장")
    cons: list[str] = Field(description="단점·주의점 코멘트 2~4개, 각 한 문장")
    migration_steps: list[str] = Field(description="현재 구조에서 전환하는 단계 3~6개")
    diagram: Diagram
    sources: list[int] = Field(description="근거로 쓴 [자료 번호]")
    bp_ids: list[str] = Field(description="근거 BP ID (예: COST06-BP03)")
    constraint_checks: list[ConstraintCheck] = Field(default_factory=list, description=(
        "<고객_제약> 에 변경 금지 리소스나 개인정보 포함이 있으면 각각 점검 결과 1개씩. 해당 제약이 없으면 빈 목록"))


class CurrentInfra(BaseModel):
    """현재 인프라를 비용 계산용 구성요소로 옮긴 것 (현재 대비 증감 계산용)."""
    nodes: list[Node]


# ───────────────────────── 코드가 계산해서 붙이는 결과 (A 파트 출력) ─────────────────────────


class CostItem(BaseModel):
    node_id: str
    label: str
    kind: str
    monthly_usd: float | None = Field(description="None = 가격표에 없어 추정 불가")
    formula: str = Field(description="계산식 (예: '0.1003 $/h × 730h × 4대')")


class CostReport(BaseModel):
    monthly_total_usd: float = Field(description="추정 가능한 항목 합계")
    items: list[CostItem]
    unpriced: list[str] = Field(description="추정 불가 항목 label 목록")
    assumptions: list[str] = Field(description="계산 가정 (온디맨드, 서울 리전, 프리티어 미적용 등)")


class Box(BaseModel):
    """그릴 사각형: cloud / vpc / az / subnet / group / edge_band / region_band."""
    id: str
    kind: str
    label: str
    x: float
    y: float
    w: float
    h: float
    style: str = Field("", description="subnet: public|private, group: auto_scaling 등")


class PlacedNode(BaseModel):
    id: str = Field(description="그려지는 인스턴스 ID. zone=each 면 '<node_id>@<zone>'")
    node_id: str
    icon: str
    label: str
    role: str
    x: float
    y: float
    w: float
    h: float


class PlacedEdge(BaseModel):
    source: str = Field(description="PlacedNode.id")
    target: str = Field(description="PlacedNode.id")
    label: str


class Layout(BaseModel):
    width: float
    height: float
    boxes: list[Box]
    nodes: list[PlacedNode]
    edges: list[PlacedEdge]
    warnings: list[str] = Field(description="검증 중 고친 것 (없는 노드를 가리키는 선 제거 등)")
