# /architectures 기능 — 두 노트북 분담

사용자 요청에 맞춰 **아키텍처 5개**를 추천하고, AI 가 **순위**를 매기고, 각 안에 **장단점 코멘트**, **월 비용**,
**아키텍처 설계도(AZ × 서브넷 층 도식)** 를 붙여서 돌려준다.

```
요청(infra, goal, priorities — /intake 의 recommend_request 그대로)
 → ① RAG 검색                                   [B]
 → ② Claude: 서로 다른 방향 5개 결정 → 각 안 상세 생성(병렬)   [B]
 → ③ cost.estimate(nodes)   → 월 비용 (가격표 기반)   [A]
    layout.layout(diagram)  → 화면 좌표               [A]
 → ④ Claude: 비용·우선순위 보고 1~5위 + 점수 + 이유    [B]
 → 응답
```

**계약은 `schemas.py`** 다. 두 파트는 이 파일의 모델만 주고받는다. 바꿔야 하면 상대에게 먼저 알린다.

| 파트 | 담당 | 파일 | API 키 / 임베딩 모델 |
|---|---|---|---|
| **A — 비용·도식** | 새 노트북 | `cost.py`, `layout.py`, `preview.html`, `tests/` | **불필요** |
| **B — AI 생성** | 기존 맥 | `architect.py`, `server.py` (`/architectures`), INSTALL.md | 필요 |

B 는 A 가 끝나기 전에도 `cost.py`, `layout.py` 의 임시 구현으로 돌아간다. A 가 머지되면 자동으로 진짜 값이 나온다.

---

## A 파트 (새 노트북) — 시작하기

```bash
git clone https://github.com/team-kkari/kkari-AI.git
cd kkari-AI
git checkout feat/architectures
git checkout -b feat/architectures-cost-layout
uv sync
uv run python -c "import json;from schemas import ArchitectureDraft;ArchitectureDraft.model_validate(json.load(open('data/samples/architecture_3tier.json',encoding='utf-8')));print('ok')"
```

모델 다운로드·API 키 필요 없음. 입력 예시: `data/samples/architecture_3tier.json` (첨부 이미지의 3-Tier 구조를 옮긴 것).
가격표: `data/pricing/ap-northeast-2.json` (이미 생성됨. 다시 만들려면 `uv run python build_pricing.py`).

### A-1. `cost.py` — `estimate(nodes: list[Node]) -> CostReport`

노드마다 `node.pricing` 을 읽어 가격표로 월 비용 계산. `CostItem.formula` 에 계산식을 사람이 읽을 수 있게 남긴다.

| kind | 계산 (H = hours_per_month) | 가격표 키 |
|---|---|---|
| ec2 | hourly × H × count + storage_gb × count × gp3 | `ec2[spec].hourly`, `ebs_gb_month.gp3` |
| rds | 인스턴스(single_az/multi_az) × H × count + storage_gb × 스토리지 단가 | `rds[engine][mode][spec]`, `rds_storage_gb_month[mode].gp3` |
| aurora | standard × H × count + storage_gb × 0.12 + requests_million × io_per_million | `rds[engine].standard`, `aurora_storage` |
| elasticache | hourly × H × count (engine 기본 Valkey) | `elasticache[engine][spec]` |
| elasticache_serverless | storage_gb × gb_hour × H + requests_million × ecpu_million | `elasticache_serverless_valkey` |
| alb / nlb | (hour + lcu × lcu_hour) × H × count | `load_balancer.alb` |
| nat_gateway | hour × H × count + data_gb × gb | `nat_gateway` |
| vpc_interface_endpoint | hour × H × count + data_gb × gb | `vpc_interface_endpoint` |
| data_transfer_out | data_gb 구간별 누진 | `data_transfer_out_tiers` |
| s3 / efs / ebs | storage_gb × 단가(spec) | `s3_gb_month`, `efs_gb_month`, `ebs_gb_month` |
| lambda | req_million×1e6×request + req×(duration_ms/1000)×memory_gb×gb_second_(x86/arm) | `lambda` |
| fargate | count × H × (vcpu × vcpu_hour + memory_gb × gb_hour) (spec: x86/arm) | `fargate` |
| eks | eks_cluster_hour × H × count | `eks_cluster_hour` |
| dynamodb | 읽기 백만×1e6×RRU + 쓰기 백만×1e6×WRU + storage_gb × storage | `dynamodb` |
| cloudfront | data_gb 구간별 누진 + requests_million × 100 × https_per_10k | `cloudfront` |
| waf | web_acl × count + 규칙 수(lcu 필드) × rule + req_million×1e6×request | `waf` |
| route53 | hosted_zone × count + req_million × query_per_million | `route53` |
| api_gateway | req_million × (http|rest)_per_million | `api_gateway` |
| sqs | req_million × sqs_per_million | `sqs_per_million` |
| free | 0 | |
| unpriced / 가격표에 없는 spec | `monthly_usd=None`, `unpriced` 에 추가 (0 으로 합산 금지) | |

- 인스턴스 타입이 가격표에 없으면(오타·미출시) None + 경고. 가까운 타입으로 바꾸지 않는다.
- `assumptions` 에 고정 문구: "서울 리전 온디맨드 기준", "RI·Savings Plans·프리티어 미적용", "가격표 생성일 <built>".
- 3-Tier 샘플 합계가 대략 맞는지 AWS Pricing Calculator 로 1~2개 항목 교차 확인 (예: m7g.large 4대 = 0.1003 × 730 × 4 ≈ $292.9).

### A-2. `layout.py` — `layout(diagram: Diagram) -> Layout`

첨부 이미지처럼 그릴 수 있는 **절대 좌표**를 만든다. 프론트는 `boxes` → `edges` → `nodes` 순서로 그리기만 한다.

```
┌ outside (users) ─────────── 맨 위 가운데
┌ AWS Cloud box ─────────────────────────────────────────────┐
│  edge band: Route53 · CloudFront · WAF · Shield (가로 한 줄)   │
│ ┌ region column ┐ ┌ VPC box ───────────────────────────────┐ │
│ │ S3            │ │   ┌ AZ1 (점선) ┐      ┌ AZ2 (점선) ┐      │ │
│ │ EFS           │ │ public │ subnet │ ALB(span) │ subnet │      │ │
│ │ ElastiCache   │ │ web    │ [ASG 점선 박스가 두 AZ 를 가로지름]   │ │
│ │ (연결된 VPC 노드 │ │ app    │ subnet │ ALB(span) │ subnet │      │ │
│ │  높이에 맞춰 정렬)│ │ db     │ [DB subnet group]               │ │
│ └───────────────┘ └───────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────┘
```

- **행** = `tiers` 순서, **열** = `zones` 순서. 칸마다 `subnet` 박스 (style public|private), AZ 마다 세로 점선 `az` 박스.
- `zone="AZ1"` 은 그 칸에. `"each"` 는 AZ 마다 복제 (PlacedNode.id = `web@AZ1`). `"span"` 은 그 층의 AZ 경계 가운데.
- `group` 이 있으면 멤버 노드들(복제본 포함)을 감싸는 `group` 박스.
- `edge` 노드는 VPC 위 한 줄, `region` 노드는 VPC 왼쪽 세로 열 — 연결된 VPC 노드의 y 평균에 맞추고 겹치지 않게.
- **VPC 가 없는 구조**(서버리스: zones/tiers 빈 목록)는 region 노드를 연결선 기준 왼→오 층(layer)으로 배치.
- 선 펼치기: 양쪽 다 `each` → 같은 AZ 끼리만. 한쪽만 `each` → 전부 연결. 그 외 1:1.
- 검증 후 고친 것은 `warnings`: 없는 노드를 가리키는 선 제거, 없는 tier/zone → region 으로 이동, 중복 id 정리.
- 노드 크기 고정 (아이콘 64×64 + 아래 라벨 두 줄), 글자 겹치지 않게 칸 간격 확보.

### A-3. `preview.html` — 확인용 뷰어 (서버 없이 열기)

`Layout` JSON 을 붙여 넣으면 SVG 로 그려 주는 단일 HTML. 아이콘은 색 사각형 + 서비스 약어면 충분 (실제 아이콘은 프론트 팀).
비용은 노드 클릭 시 해당 `CostItem` 표시. 레이아웃이 이미지와 비슷하게 나오는지 눈으로 확인하는 용도.

### A-4. 테스트 `tests/test_cost.py`, `tests/test_layout.py` (pytest)

- m7g.large 4대, NAT 2개 + 300GB, RDS Multi-AZ 등 손계산 값과 일치
- 가격표에 없는 타입 → None + unpriced
- 샘플 3-Tier: 노드 겹침 없음, 모든 노드가 cloud 박스 안, each 노드가 AZ 수만큼, 끊어진 선 없음
- 서버리스 샘플(직접 하나 작성): VPC 박스 없이 배치

### A 완료 기준

`feat/architectures-cost-layout` → `feat/architectures` 로 PR. 위 테스트 통과 + preview 스크린샷 첨부.

---

## B 파트 (기존 맥)

- `architect.py`: ① 방향 5개 결정(서로 겹치지 않게, 고객 제약 반영) ② 방향별 `ArchitectureDraft` 병렬 생성 + 현재 인프라 `CurrentInfra`
  ③ A 함수 호출 ④ 순위·점수(cost/performance/reliability/operations/migration 1~5)·이유 생성
- 도식 검증 실패(없는 노드, 잘못된 tier) 시 layout 의 warnings 로 처리, 심하면 해당 안만 재생성
- `server.py`: `POST /architectures` (요청 = RecommendReq), 시작 시 스키마 워밍업
- 응답 형식과 예시는 INSTALL.md 에 추가
