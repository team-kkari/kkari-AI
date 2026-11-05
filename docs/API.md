# kkari-AI 서버 API 명세 (백엔드 연동용)

AWS Best Practice RAG 기반 AI 서버. 백엔드가 호출해서 **초기 상담 → 추천 아키텍처 → 챗봇 Q&A** 기능을 쓴다.

- 버전: 2026-11-05 (main `5cf7fbc` 기준)
- 실시간 스키마: 서버 실행 후 `http://<AI서버>:8000/docs` (Swagger), `http://<AI서버>:8000/openapi.json`

---

## 1. 공통

| 항목 | 값 |
|---|---|
| Base URL | `http://<AI서버 IP>:8000` (개발 시 `http://localhost:8000`) |
| 인증 | 없음 (내부망 전용. 외부에 직접 노출하지 말고 백엔드만 호출) |
| 형식 | 요청·응답 모두 `application/json; charset=utf-8` (`/chat` 응답만 `text/plain` 스트리밍) |
| 문자 | 한국어 UTF-8. 금액은 USD, 소수점 2자리 |
| CORS | 전체 허용 (`*`), `X-Sources` 헤더 노출 |
| 상태 | **서버는 상태를 저장하지 않는다.** 대화 기록·결과 저장은 백엔드 책임 |

### 1.1 엔드포인트 요약

| 메서드 | 경로 | 용도 | 소요 시간 | 권장 타임아웃 |
|---|---|---|---|---|
| GET | `/health` | 서버 준비 확인 | 즉시 | 5초 |
| POST | `/intake` | 초기 상담 — 챗봇이 질문하며 요구사항 수집 | 4~10초 / 턴 | 60초 |
| POST | `/architectures` | **추천 아키텍처 N개** (순위·장단점·월 비용·설계도 좌표) | 1~2.5분 | **300초** |
| POST | `/recommend` | 개선안 목록 (단일 솔루션, 구버전 기능) | 약 80초 | 180초 |
| POST | `/chat` | AWS Q&A 챗봇 (스트리밍 텍스트) | 10~20초 | 60초 |
| POST | `/search` | 근거 문서 검색만 (LLM 미사용) | 1초 미만 | 10초 |

> 서버 시작 직후 1~2분은 백그라운드 워밍업 중이라 첫 `/intake`, `/architectures` 가 더 느릴 수 있다.

### 1.2 연동 흐름

```
[프론트] ──> [백엔드] ──> [AI 서버]

1) 상담:      POST /intake  { history: [] }                 → reply(첫 질문)
              (고객 답변을 history 에 쌓아서 반복 호출)
              POST /intake  { history: [...], ... }         → ready: true + recommend_request
2) 추천:      POST /architectures  recommend_request + count → architectures[5] (rank 순)
3) 질문:      POST /chat  (아무 때나, 추천 결과에 대한 추가 질문 등)
```

- 백엔드가 AWS 에서 자동 수집한 인프라 요약이 있으면 `/intake` 의 `infra_context` 로 넘긴다 → 챗봇이 그 내용은 다시 묻지 않는다
- `/intake` 가 `ready: true` 를 주면 `recommend_request` 를 **수정 없이** `/architectures` body 로 보내면 된다 (`count` 만 추가)

### 1.3 에러 형식

| 코드 | 언제 | `detail` 형식 | 백엔드 처리 |
|---|---|---|---|
| 400 | 잘못된 category, history 형식 오류, count 범위 밖 | 문자열 | 요청 수정 (재시도 X) |
| 422 | 필수 필드 누락·타입 오류 (FastAPI 검증) | **배열** `[{"loc": [...], "msg": "...", "type": "..."}]` | 요청 수정 |
| 429 | Claude API 사용량 한도 | 문자열 | 10~30초 후 재시도 |
| 502 | Claude API 오류·연결 실패·모델 거절·생성 실패 | 문자열 | 1회 재시도 후 사용자에게 오류 표시 |
| 503 | AI 서버에 API 키 미설정 | 문자열 | AI 서버 설정 문제 → 담당자 호출 |

```json
{ "detail": "알 수 없는 category: ['foo']. 가능: ['cost', 'performance', ...]" }
```

> `detail` 이 문자열(400/429/502/503)일 때와 배열(422)일 때가 있으니 둘 다 처리한다.

### 1.4 공통 값

**category** (`priorities`, `categories` 에 사용)

| 값 | 의미 | | 값 | 의미 |
|---|---|---|---|---|
| `cost` | 비용 | | `compute` | 컴퓨팅 (EC2 등) |
| `performance` | 성능 | | `database` | 데이터베이스 |
| `reliability` | 안정성 | | `container` | 컨테이너 (ECS/EKS) |
| `security` | 보안 | | `network` | 네트워크 |
| `operations` | 운영 우수성 | | `serverless` | 서버리스 |
| `sustainability` | 지속 가능성 | | | |

`priorities` 에는 앞의 6개(기둥)를 쓰고, **앞에 둔 것이 우선순위가 높다.**

**Source** (근거 문서) — 응답의 `sources[]` 원소

| 필드 | 타입 | 설명 |
|---|---|---|
| `n` | int | 출처 번호. 본문·추천의 `sources: [1, 3]`, `[1]` 이 이 번호를 가리킨다 |
| `title` | string | 문서 제목 |
| `bp_id` | string \| null | Well-Architected BP ID (예: `COST06-BP03`) |
| `category` | string | 위 category |
| `service` | string \| null | 서비스별 가이드면 서비스명 (예: `EKS`) |
| `url` | string | AWS 공식 문서 링크 |

---

## 2. `GET /health`

```json
{ "ok": true, "chunks": 4417 }
```

`chunks` 는 검색 인덱스 크기. 응답이 오면 요청을 받을 수 있다.

---

## 3. `POST /intake` — 초기 상담

고객(인프라 전문가)에게 챗봇이 1~2개씩 질문하며 요구사항 프로필을 채운다. **매 턴 대화 전체를 보낸다.**

### 요청

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `history` | `{role, content}[]` | - | 지금까지 대화. 첫 턴은 `[]`. `role` 은 `"assistant"`(챗봇) \| `"user"`(고객) |
| `infra_context` | string | - | 백엔드가 자동 수집한 인프라 요약 (없으면 `""`) |
| `session_id` | string \| null | - | 상담 세션 ID (백엔드에서 생성, 로그 묶음용) |

```json
// 첫 턴
{ "history": [], "infra_context": "EC2 m5.2xlarge 10대, CPU 평균 12%, RDS MySQL 단일 AZ, 월 $4,200", "session_id": "c1f0e7b2" }

// 다음 턴: 직전 reply + 고객 답변을 뒤에 붙인다
{ "history": [
    { "role": "assistant", "content": "(1턴 reply)" },
    { "role": "user", "content": "B2C 이커머스, MAU 80만입니다" }
  ],
  "infra_context": "...", "session_id": "c1f0e7b2" }
```

### 응답

| 필드 | 타입 | 설명 |
|---|---|---|
| `reply` | string | 채팅창에 표시할 챗봇 메시지 (마크다운) |
| `quick_replies` | string[] | 버튼으로 보여줄 답변 후보 2~4개 (없으면 `[]`) |
| `profile` | object | 지금까지 모은 요구사항 (아래 표, 모르는 항목은 `null`) |
| `missing` | string[] | 아직 못 모은 필수 항목 (한국어 라벨, 예: `"가용성 목표"`) |
| `ready` | bool | 진단 시작 가능 여부 |
| `recommend_request` | object \| null | `ready=true` 일 때 `{infra, goal, priorities}` → 그대로 `/architectures` 로 |

**profile** 필드 (★ = 필수 항목)

| 필드 | 라벨 | | 필드 | 라벨 |
|---|---|---|---|---|
| ★ `workload` | 워크로드 | | `performance` | 성능 요구 |
| ★ `architecture` | 현재 구성 | | `goal` | 목표 |
| ★ `traffic` | 트래픽 | | `constraints` | 제약 |
| ★ `availability` | 가용성 목표 | | `operations` | 운영 체계 |
| ★ `cost` | 비용/예산 | | `security` | 보안 요구 |
| ★ `priorities` | 우선순위 (category 배열) | | | |
| ★ `change_tolerance` | 변경 허용 범위 | | | |

```json
{
  "reply": "확인했습니다. 이어서 트래픽 패턴을 여쭙겠습니다. 평시와 피크 RPS 는 어느 정도인가요?",
  "quick_replies": ["평시 일정, 세일 때 3~5배", "저녁 피크, RPS 모름"],
  "profile": { "workload": "B2C 이커머스, MAU 80만", "architecture": "EC2 m5.2xlarge 10대 ...", "traffic": null,
               "availability": null, "performance": null, "cost": "월 $4,200", "goal": null, "priorities": ["cost"],
               "change_tolerance": null, "constraints": null, "operations": null, "security": null },
  "missing": ["트래픽", "가용성 목표", "변경 허용 범위"],
  "ready": false,
  "recommend_request": null
}
```

- 고객이 "바로 진단해 줘" 라고 하면 필수 항목이 남아 있어도 `ready=true` 가 될 수 있다 (`missing` 에 남은 항목 표시)
- 보통 4~6턴이면 `ready=true`

---

## 4. `POST /architectures` — 추천 아키텍처 ★ 메인 기능

AI 가 서로 다른 설계 방향(축)으로 아키텍처를 N개 만들고, 코드가 AWS 공식 가격표로 월 비용을 계산하고, AI 가 순위를 매긴다.
**가격은 AI 가 쓰지 않고 코드가 계산한다** (서울 리전 온디맨드, `data/pricing/ap-northeast-2.json`).

### 요청

| 필드 | 타입 | 필수 | 기본 | 설명 |
|---|---|---|---|---|
| `infra` | string | ✔ | | 현재 인프라 + 상담 내용 (수치가 구체적일수록 정확) |
| `goal` | string | | `""` | 원하는 방향 한 줄 |
| `priorities` | string[] | | `[]` | 우선순위 (앞일수록 중요) |
| `count` | int | | `5` | 추천 개수 1~5. 줄이면 빨라짐 (2개 ≈ 70초) |

```json
{
  "infra": "[자동 수집]\nEC2 m5.2xlarge 10대 (ALB 뒤, ASG 없음), CPU 평균 12%. RDS MySQL db.r5.xlarge 단일 AZ ...\n\n[상담 내용]\n- 워크로드: B2C 이커머스 ...",
  "goal": "서비스 품질은 유지하면서 비용 절감",
  "priorities": ["cost", "reliability"],
  "count": 5
}
```

### 응답 (최상위)

| 필드 | 타입 | 설명 |
|---|---|---|
| `overall` | string | N개 안 비교 총평 (1위 선정 이유 포함) |
| `current` | object | 현재 인프라 계산 결과 `{cost: CostReport \| null, nodes: Node[]}` |
| `architectures` | Architecture[] | **rank 오름차순 정렬** (1위가 첫 번째) |
| `sources` | Source[] | 근거 문서 목록 |
| `failed` | string[] | 생성에 실패한 안 (있어도 나머지는 정상 반환). 보통 `[]` |
| `timings_s` | object | 단계별 누적 소요 시간 (디버깅용) |

> 응답 크기 약 100~130KB (설계도 좌표 포함). DB 에 저장할 때 JSON 컬럼/문서 저장 권장.

### Architecture

| 필드 | 타입 | 화면 용도 |
|---|---|---|
| `rank` | int | 순위 (1 = 가장 추천) |
| `axis` | string | 설계 축 코드 (아래 표) |
| `axis_label` | string | 설계 축 한국어 라벨 → 태그 |
| `signature_changes` | string[] | 이 안만의 대표 변경 2~3개 → 칩 |
| `name` | string | 안 이름 (제목) |
| `concept` | string | 한 줄 콘셉트 (부제) |
| `easy_explanation` | string | **비전공자용 쉬운 설명** 2~3문장 |
| `pros` | string[] | 장점 코멘트 3~5개 |
| `cons` | string[] | 단점·주의점 2~4개 |
| `migration_steps` | string[] | 현재 구조에서 전환 단계 |
| `rank_reason` | string | 이 순위인 이유 (계산된 비용 인용) |
| `best_for` | string | "이럴 때 적합" 한 문장 |
| `scores` | object | `{cost, performance, reliability, operations, migration}` 각 1~5, **높을수록 좋음** (운영=부담 적음, migration=전환 쉬움) → 레이더 차트 |
| `cost` | CostReport | 월 비용 (아래) |
| `vs_current` | `{usd, pct}` \| null | 현재 대비 증감. 음수 = 절감 (예: `{"usd": -2223.59, "pct": -43.8}`). 현재 비용 계산 불가 시 `null` |
| `diagram` | Diagram | 설계도 원본 (배치 정보 + 노드별 과금 정보) |
| `layout` | Layout | **설계도 화면 좌표** — 프론트는 이것만 그리면 됨 |
| `sources` | int[] | 근거 출처 번호 (`sources[].n`) |
| `bp_ids` | string[] | 근거 BP ID |

**axis 값**: `rightsize_current`(현 구조 적정화), `containers`(컨테이너 전환), `serverless`(서버리스 전환),
`managed_data`(데이터 계층 관리형 전환), `network_edge`(네트워크·엣지 최적화), `high_availability`(고가용성 강화), `minimum_cost`(최저 비용)

### CostReport

| 필드 | 타입 | 설명 |
|---|---|---|
| `monthly_total_usd` | number | 추정 가능한 항목의 월 합계 |
| `items[]` | CostItem[] | 노드별 비용 |
| `items[].node_id` | string | `diagram.nodes[].id` / `layout.nodes[].node_id` 와 연결 |
| `items[].label` | string | 노드 라벨 |
| `items[].kind` | string | 과금 종류 (`ec2`, `rds`, `nat_gateway`, `free`, `unpriced` …) |
| `items[].monthly_usd` | number \| null | **null = 가격표에 없어 추정 불가** (0 과 구분해서 "추정 불가"로 표시) |
| `items[].formula` | string | 계산식 (예: `0.1003 $/h × 730h × 4대 + EBS gp3 30GB × 4대 × 0.0912 $/GB = 303.82 $`) |
| `unpriced` | string[] | 추정 불가 항목 라벨 → "합계에 미포함" 안내 |
| `assumptions` | string[] | 계산 가정 (서울 리전 온디맨드, RI·Savings Plans·프리티어 미적용, 가격표 생성일) |

### Layout (설계도 좌표)

픽셀 단위 절대 좌표, 원점은 왼쪽 위. 그리는 순서: **`boxes` → `edges` → `nodes`**.

| 필드 | 타입 | 설명 |
|---|---|---|
| `width`, `height` | number | 캔버스 크기 |
| `boxes[]` | `{id, kind, label, x, y, w, h, style}` | 사각형 영역 |
| `nodes[]` | `{id, node_id, icon, label, role, x, y, w, h}` | 아이콘 (64×64). `label` 굵게, `role` 작게 아래에 |
| `edges[]` | `{source, target, label}` | 화살표. `source`/`target` 은 `nodes[].id` |
| `warnings` | string[] | 좌표 계산 중 자동 보정한 내용 (보통 `[]`) |

- `boxes[].kind`: `cloud`(AWS Cloud, 실선) · `edge_band`(Edge/Global, 점선) · `region_band`(Region services, 점선) · `vpc`(초록 실선) · `az`(파란 점선) · `subnet` · `group`
- `boxes[].style`: subnet 이면 `public`(초록 배경) \| `private`(파란 배경), group 이면 `auto_scaling`(주황 점선) \| `db_subnet_group`(남색 점선) \| `cluster` \| `other`
- `nodes[].id` 는 AZ 마다 복제된 노드면 `web@AZ1`, `web@AZ2` 처럼 붙는다. **비용 조회는 `node_id`** 로 (`cost.items[].node_id`)
- `nodes[].icon` → AWS Architecture Icons 매핑 키:
  `users, internet, route53, cloudfront, waf, shield, acm, api_gateway, igw, nat_gateway, alb, nlb, vpc_endpoint, ec2, auto_scaling,
  lambda, ecs, fargate, eks, ecr, rds, aurora, dynamodb, elasticache, s3, efs, ebs, sqs, sns, eventbridge, step_functions,
  cloudwatch, secrets_manager, kms, iam, cognito, backup`
- 참고 구현: 레포의 `preview.html` (Layout JSON 을 SVG 로 그림, 노드 클릭 시 비용 표시)

```jsonc
"layout": {
  "width": 1072, "height": 1360,
  "boxes": [
    { "id": "cloud", "kind": "cloud", "label": "AWS Cloud", "x": 40, "y": 200, "w": 992, "h": 1120, "style": "" },
    { "id": "subnet:public:AZ1", "kind": "subnet", "label": "Public subnet", "x": 372, "y": 560, "w": 204, "h": 176, "style": "public" }
    // ...
  ],
  "nodes": [
    { "id": "route53", "node_id": "route53", "icon": "route53", "label": "Amazon Route 53", "role": "DNS", "x": 504, "y": 268, "w": 64, "h": 64 },
    { "id": "web@AZ1", "node_id": "web", "icon": "ec2", "label": "EC2 m7g.xlarge", "role": "Web servers", "x": 440, "y": 800, "w": 64, "h": 64 }
  ],
  "edges": [ { "source": "users", "target": "route53", "label": "DNS" } ],
  "warnings": []
}
```

### Diagram (원본, 선택 사용)

`layout` 을 만든 원본. 직접 다른 방식으로 그리거나 노드 상세(과금 사양)를 보여줄 때 사용.

| 필드 | 설명 |
|---|---|
| `zones` | AZ 이름 목록 (서버리스면 `[]`) |
| `tiers[]` | `{id, label, kind: public\|private}` VPC 서브넷 층, 위→아래 |
| `nodes[]` | `{id, icon, label, role, place, tier, zone, group, pricing}` — `place`: `outside`\|`edge`\|`region`\|`vpc`\|`subnet`, `zone`: `AZ1`\|`each`\|`span` |
| `nodes[].pricing` | 과금 사양 `{kind, spec(인스턴스 타입 등), engine, multi_az, count(전체 대수), hours_per_month, storage_gb, data_gb_month, requests_million_month, ...}` |
| `groups[]` | `{id, kind, label}` |
| `edges[]` | `{source, target, label}` (복제 전 노드 id 기준) |

---

## 5. `POST /recommend` — 개선안 목록

`/architectures` 이전 버전. 아키텍처 대안 대신 **현재 구조의 개선안 목록**을 준다. 요청은 `/architectures` 와 같고 `count` 없음.

| 응답 필드 | 설명 |
|---|---|
| `solution.summary` | 진단 요약 |
| `solution.current_assessment` | Best Practice 기준 현재 평가 |
| `solution.recommendations[]` | `{title, category, target, problem, action, expected_impact, effort: low\|medium\|high, priority, sources: int[], bp_ids}` |
| `solution.caveats` | 확인 필요·주의 사항 |
| `sources` | Source[] |

---

## 6. `POST /chat` — AWS Q&A 챗봇

### 요청

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `question` | string | ✔ | 질문 |
| `history` | `{role, content}[]` | | 이전 대화 `[{"role":"user",...},{"role":"assistant",...}]` |
| `categories` | string[] \| null | | 검색 범위 제한 (null = 전체) |

```json
{ "question": "NAT Gateway 비용 줄이는 방법 알려줘", "history": [], "categories": null }
```

### 응답

- 본문: `text/plain; charset=utf-8` **스트리밍** (chunked). 문장 끝 `[1]`, `[2]` 는 출처 번호
- 헤더 `X-Sources`: Source[] 의 JSON 을 **URL 인코딩**한 문자열
  - JS: `JSON.parse(decodeURIComponent(res.headers.get("X-Sources")))`
  - Java: `URLDecoder.decode(header, StandardCharsets.UTF_8)` 후 JSON 파싱
- 백엔드가 중계할 때: 스트림을 그대로 프론트로 흘려보내려면 SSE/청크 중계, 아니면 끝까지 받아서 한 번에 전달
- 모델이 거절하면 본문 끝에 안내 문구가 붙는다 (HTTP 200)

---

## 7. `POST /search` — 근거 문서 검색 (LLM 미사용)

| 요청 필드 | 타입 | 필수 | 기본 | 설명 |
|---|---|---|---|---|
| `query` | string | ✔ | | 검색 문장 |
| `k` | int | | 8 | 결과 개수 |
| `categories` | string[] \| null | | null | 범위 제한 |

응답: `{ "results": [ Source + { "text": "본문 조각", "score": 0.0325 } ] }`

---

## 8. 백엔드 구현 가이드

- **타임아웃**: `/architectures` 는 HTTP 클라이언트 read timeout 을 **300초 이상**으로. 프록시(Nginx 등)를 거치면 `proxy_read_timeout` 도 같이 늘린다
- **긴 작업 처리**: 프론트 요청을 2분 넘게 붙잡지 않으려면 백엔드에서 비동기 작업으로 감싸는 것을 권장
  (`POST /jobs` → 작업 ID 즉시 반환 → 백엔드가 AI 서버 호출 → 프론트는 폴링/SSE 로 완료 확인)
- **중복 호출 방지**: `/architectures` 1회 = Claude API 약 8회 호출 (count=5 기준: 방향 1 + 설계 5 + 현재 인프라 1 + 순위 1) (비용 발생). 같은 상담 결과로 다시 요청하지 않도록 결과를 저장해 재사용
- **동시 요청**: AI 서버는 요청마다 내부적으로 병렬 호출한다. 동시에 3건 이상 몰리면 429 가능 → 백엔드에서 큐/세마포어로 2건 정도로 제한 권장
- **상담 기록**: `/intake` 는 상태가 없으므로 백엔드가 세션별 `history` 를 저장하고 매 턴 전체를 보낸다
- **재시도**: 429 → 지수 백오프 재시도, 502 → 1회 재시도, 400/422/503 → 재시도 금지
- **헬스 체크**: AI 서버 재시작 후 `/health` 가 200 이 될 때까지 대기. 이후 1~2분은 워밍업이라 첫 요청이 느릴 수 있음

---

## 9. 변경 이력

| 날짜 | 내용 |
|---|---|
| 2026-11-05 | `/architectures` 추가 (순위·장단점·비용·설계도), 응답에 `axis`, `axis_label`, `signature_changes` 추가 |
| 2026-11-04 | `/intake`, `/recommend`, `/chat`, `/search` 최초 공개 |
