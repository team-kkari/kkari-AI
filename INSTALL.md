# 설치 가이드 (Windows)

kkari-AI RAG 서버를 Windows 노트북에서 실행하는 방법.
수집 데이터와 임베딩 인덱스가 레포에 포함되어 있어서 **clone → 설치 → 서버 실행**만 하면 된다.

> 모든 명령은 **PowerShell** 기준. macOS 도 명령은 동일하다(`setx` 대신 `export`).

---

## 0. 준비물

| 항목 | 비고 |
|---|---|
| Git | <https://git-scm.com/download/win> |
| uv | Python 설치·가상환경·패키지 관리를 한 번에 해준다 (Python 따로 설치 불필요) |
| 디스크 여유 | 약 4GB (임베딩 모델 470MB + PyTorch 등) |
| Claude API 키 | `/intake`, `/architectures`, `/recommend`, `/chat` 에 필요. `/search` 는 키 없이 동작 |
| GPU | **필요 없음.** 인덱스는 미리 만들어져 있고, 질문 한 줄 임베딩은 CPU 로 충분 |

---

## 1. uv 설치

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

설치 후 **PowerShell 을 닫았다가 다시 연다** (PATH 반영).

```powershell
uv --version
```

## 2. (권장) 환경 변수

```powershell
# 한글 출력/파일이 cp949 로 깨지지 않도록 Python 을 UTF-8 모드로
setx PYTHONUTF8 1

# Windows 사용자 이름이 한글이면 모델 캐시 경로 문제를 피하려고 영문 경로로 지정
setx HF_HOME "C:\hf"

# Claude API 키 (본인 키로 교체)
setx ANTHROPIC_API_KEY "sk-ant-..."
```

`setx` 는 **새로 연 PowerShell 부터** 적용된다. 설정 후 창을 다시 열 것.

## 3. 코드 받기 + 패키지 설치

```powershell
cd $HOME\Desktop
git clone https://github.com/team-kkari/kkari-AI.git
cd kkari-AI
uv sync
```

`uv sync` 가 Python 3.11 과 의존성(PyTorch, sentence-transformers, FastAPI, anthropic 등)을 `.venv` 에 설치한다. 처음엔 몇 분 걸린다.

## 4. 임베딩 모델 받기 (470MB, 1회)

**방법 A — USB 로 복사 (가장 빠름):** 이미 받은 PC 의 `kkari-AI\models\` 폴더를 통째로 이 PC 의 `kkari-AI\models\` 에 복사. 끝.

**방법 B — 다운로드:**

```powershell
uv run python download_model.py
```

- `models\multilingual-e5-small\` 에 필요한 파일만 받는다. 진행률(KB/s)이 출력된다
- 끊기거나 멈추면 Ctrl+C 후 **같은 명령을 다시 실행하면 이어받는다**
- 해커톤장 와이파이처럼 느린 망에서는 수십 분 걸릴 수 있다 → **휴대폰 핫스팟** 권장 (1~2분)

> `hf download` 나 서버 첫 실행 시 자동 다운로드는 느린 망에서 0KB/s 로 멈추는 경우가 있어서 이 스크립트를 쓴다.

## 5. 동작 확인 (API 키 불필요)

```powershell
uv run python eval_search.py
```

질문 12개에 대해 검색 결과 상위 3개가 출력되고 마지막에 `11/12 통과` 정도가 나오면 정상 (RDS 고가용성 1건은 수집 범위 밖이라 실패가 정상).

## 6. 서버 실행

```powershell
uv run uvicorn server:app --host 0.0.0.0 --port 8000
```

- `Application startup complete` 가 뜨면 준비 완료 (모델·인덱스 로딩에 약 1분)
- API 키 없이 AI 기능을 부르면 500 `{"code": "NOT_CONFIGURED"}` 가 온다 (서버 로그에 원인 출력)
- 브라우저에서 <http://localhost:8000/docs> → Swagger 에서 바로 테스트 가능
- 처음 실행 시 **Windows 방화벽 허용 창**이 뜨면 "개인 네트워크" 허용 → 같은 와이파이의 백엔드 서버가 `http://<노트북IP>:8000` 으로 호출 가능
- **CORS 는 기본으로 꺼져 있다** (브라우저에서 직접 호출 불가, 백엔드만 호출). 꼭 필요하면 `setx ALLOWED_ORIGINS "http://백엔드주소"`
- 에러 응답은 모두 `{"code", "message", "retryable"}` — `retryable: true` 일 때만 재시도
  - 노트북 IP 확인: `ipconfig` → "IPv4 주소"

---

## API 요약 (백엔드 연동용)

### `POST /search` — 근거 자료 검색만 (API 키 불필요)

```json
{ "query": "EC2 CPU 사용률이 낮은데 비용 줄이는 법", "k": 5, "categories": ["cost"] }
```

### `POST /intake` — 초기 상담 인터뷰 챗봇 (챗봇이 질문)

고객(인프라 전문가)에게 챗봇이 1~2개씩 질문하며 요구사항을 모은다. 서버는 상태를 저장하지 않으므로 **매 턴 대화 기록 전체를 보낸다.**

```jsonc
// 첫 턴: history 비움 → 인사 + 첫 질문
{ "history": [], "infra_context": "(백엔드가 수집한 인프라 요약, 없으면 빈 문자열)", "session_id": "uuid" }

// 다음 턴: 직전 상담원 메시지 + 고객 답변을 붙여서 다시 호출
{ "history": [
    {"role": "assistant", "content": "(직전 reply)"},
    {"role": "user", "content": "B2C 이커머스, MAU 80만입니다"}
  ], "infra_context": "...", "session_id": "uuid" }
```

응답:

```jsonc
{
  "reply": "확인했습니다. 이어서 ... 피크 RPS 는 어느 정도인가요?",   // 채팅창에 표시
  "quick_replies": ["평시 일정, 세일 때 3~5배", "저녁 피크, RPS 모름"],  // 버튼으로 표시
  "profile": { "workload": "...", "traffic": null, "priorities": ["cost"], ... },  // 모은 정보
  "missing": ["가용성 목표", "변경 허용 범위"],   // 아직 못 모은 필수 항목
  "ready": false,
  "recommend_request": null   // ready=true 가 되면 {infra, goal, priorities, constraints} → 그대로 /architectures 에 POST
}
```

- 흐름: `/intake` 반복 → `ready: true` → `recommend_request` 를 `/architectures` 로 보내 추천 생성
- profile 의 `company_name`(고객사명)은 기본 필수 항목이라 첫 질문에서 함께 묻는다 (첫 입력에 있으면 자동 추출)
- profile 의 제약 필드 (타입 고정): `monthly_budget`(number, 0=예산 없음), `currency`(`USD`|`KRW`), `contains_personal_data`(bool),
  `frozen_resources`(string[], `[]`=없음). null 은 아직 모름
- 요청에 `required_fields: ["workload", "monthly_budget", ...]` 를 주면 필수 질문 목록을 바꿀 수 있다. 응답 `missing_fields` 는 영문 키
- `infra_context` 로 넘긴 정보는 다시 묻지 않고, 그걸 근거로 더 깊게 묻는다
- 한 턴 약 4~10초. 서버 시작 직후 첫 호출이 느린 것을 막으려고 시작 시 백그라운드 워밍업을 1회 한다
- 대화·프로필은 `data/intake_logs/날짜.jsonl` 에 쌓인다 (git 미포함, 추후 분석/학습용)

### `POST /recommend` — 인프라 정보 → 솔루션(JSON)

```json
{
  "infra": "EC2 m5.2xlarge 10대(ASG 없음), CloudWatch CPU 평균 12%, RDS db.r5.xlarge 단일 AZ, 월 비용 $4,200 (EC2 62%)",
  "goal": "서비스 품질은 유지하면서 비용 절감",
  "priorities": ["cost", "reliability"]
}
```

응답:

```jsonc
{
  "solution": {
    "summary": "...",
    "current_assessment": "...",
    "recommendations": [
      {
        "title": "EC2 라이트사이징", "category": "cost", "target": "EC2 m5.2xlarge x10",
        "problem": "...", "action": "...", "expected_impact": "월 약 ...% 절감 (가정: ...)",
        "effort": "low", "priority": 1, "sources": [1, 3], "bp_ids": ["COST06-BP03"]
      }
    ],
    "caveats": ["..."]
  },
  "sources": [ { "n": 1, "title": "...", "bp_id": "COST06-BP03", "category": "cost", "url": "https://docs.aws.amazon.com/..." } ]
}
```

`recommendations[].sources` 의 숫자 = `sources[].n` → 프론트에서 "참고한 Best Practice" 링크로 표시.

### `POST /architectures` — 추천 아키텍처 5개 (순위·장단점·비용·설계도)

요청은 `/intake` 의 `recommend_request` 를 그대로 보내면 된다. 선택 입력:
`count`(1~5, 기본 5), `constraints`(예산·개인정보·변경 금지 리소스, `krw_per_usd` 환율 기본 1400),
`current_monthly_usd`(백엔드가 분석한 현재 월 비용 — 주면 현재 대비 증감의 기준, 응답 `current.source = "input"`).
안마다 `violations[]` (`{constraint, checked_by: code|ai, detail}`) 가 붙고, 위반 안은 순위가 뒤로 간다.

```json
{ "infra": "EC2 m5.2xlarge 10대 ...", "goal": "비용 절감", "priorities": ["cost", "reliability"], "count": 5 }
```

응답 (`architectures` 는 rank 순으로 정렬됨):

```jsonc
{
  "overall": "5개 안 비교 총평 …",
  "current": { "monthly_usd": 4200, "source": "input",      // input = 요청값, estimated = AI 가 구성으로 추정
               "estimated_cost": null, "nodes": [] },        // source=estimated 일 때만 채워짐
  "pricing": { "region": "ap-northeast-2", "built": "2026-11-05", "currency": "USD", "basis": "온디맨드 …" },
  "warnings": [],            // 서울 외 리전 언급 시 경고
  "architectures": [
    {
      "rank": 1, "rank_reason": "…", "best_for": "…",
      "scores": { "cost": 5, "performance": 4, "reliability": 4, "operations": 3, "migration": 4 },  // 1~5
      "name": "…", "concept": "…", "easy_explanation": "비전공자용 설명",
      "pros": ["…"], "cons": ["…"], "migration_steps": ["…"],
      "diagram": { "zones": ["AZ1","AZ2"], "tiers": [...], "nodes": [...], "groups": [...], "edges": [...] },
      "layout":  { "width": 1200, "height": 900, "boxes": [...], "nodes": [...], "edges": [...] },  // 그대로 그리면 되는 좌표
      "cost": { "monthly_total_usd": 2610.2, "items": [{ "node_id": "web", "monthly_usd": 292.9, "formula": "…" }],
                "unpriced": [], "assumptions": ["서울 리전 온디맨드 기준", …] },
      "vs_current": { "usd": -1570.3, "pct": -37.6 },
      "sources": [1, 4], "bp_ids": ["COST06-BP03"],
      "constraint_checks": [{ "constraint": "frozen_resources", "satisfied": true, "detail": "…" }],
      "violations": []       // [{ "constraint": "monthly_budget", "checked_by": "code", "detail": "…" }]
    }
  ],
  "sources": [ { "n": 1, "title": "…", "url": "…" } ],
  "failed": [],              // 생성 실패한 안 (있으면 나머지만 반환)
  "timings_s": { … }
}
```

- 설계도 그리기: `layout.boxes`(cloud·VPC·AZ·subnet·Auto Scaling 그룹 사각형) → `layout.edges` → `layout.nodes`(아이콘) 순서로.
  `nodes[].icon` 은 AWS Architecture Icons 키 (`ec2`, `rds`, `alb` …, 전체 목록은 `schemas.py` 의 `Icon`)
- 노드를 누르면 `cost.items` 에서 같은 `node_id` 를 찾아 비용·계산식을 보여주면 된다
- 비용은 `data/pricing/ap-northeast-2.json`(AWS 공식 Price List) 로 코드가 계산한다 — AI 가 가격을 쓰지 않는다. 가격표 갱신: `uv run python build_pricing.py`
- 1~3분 걸린다 (아키텍처 5개를 병렬 생성 후 순위 비교). 프론트에 로딩 화면 필요

### `POST /terraform` — 추천 아키텍처 → Terraform 초안 (즉시, AI 호출 없음)

`/architectures` 응답의 `architectures[i]` 를 그대로 보내면 된다 (`name`, `diagram` 사용). 선택: `constraints`, `format`(`json`|`zip`).

```json
{ "name": "Graviton ASG + RDS Multi-AZ", "diagram": { ... }, "constraints": { "frozen_resources": ["RDS prod-db"] }, "format": "json" }
```

- `json` 응답: `{ "project", "files": {"versions.tf", "variables.tf", "main.tf", "outputs.tf", "README.md"}, "warnings": [...] }`
- `zip` 응답: `terraform/*.tf` 가 든 압축 파일 다운로드
- 사양(인스턴스 타입·대수·엔진·Multi-AZ·스토리지)은 diagram 그대로 = 비용 계산과 같은 값. VPC·서브넷·NAT·보안 그룹(연결선대로)·ALB·ASG·RDS/Aurora·ElastiCache·S3·EFS·CloudFront·WAF·Fargate·Lambda·SQS·DynamoDB 등 변환
- 보안 기본값: 저장 암호화, S3 퍼블릭 차단, IMDSv2, RDS 비밀번호 Secrets Manager 자동 관리, deletion_protection
- 변경 금지 리소스는 `prevent_destroy` + `terraform import` 안내 주석. 자동 적용(apply)은 하지 않는다
- 변환 못 한 노드·사람이 채울 값(도메인, Lambda 코드 등)은 `warnings` 와 README.md 에

### `POST /chat` — 챗봇 (NDJSON 스트리밍)

```json
{ "question": "NAT Gateway 비용 줄이는 방법?", "history": [], "categories": null }
```

응답 `application/x-ndjson` — 한 줄에 JSON 하나:

```
{"type":"sources","sources":[{"n":1,"title":"...","url":"..."}]}
{"type":"delta","text":"NAT Gateway 비용은 "}
{"type":"delta","text":"... [1]"}
{"type":"done","refused":false}
```

- 본문 속 `[1]` 은 sources 의 `n`. `refused: true` 면 모델이 정책상 답변을 거절한 것
- 스트림 도중 오류는 마지막 줄 `{"type":"error","code":...,"retryable":...}`. 시작 전 오류는 일반 에러 응답(HTTP 4xx/5xx)
- 다음 질문 때 `history` 에 `[{"role":"user","content":"..."},{"role":"assistant","content":"..."}]` 로 이전 대화를 넣는다

`categories` 가능 값: `cost, performance, reliability, security, operations, sustainability, serverless, compute, database, container, network`

---

## (선택) 데이터 다시 만들기

레포의 데이터를 그대로 쓰면 필요 없다. 수집 범위를 바꿨을 때만:

```powershell
uv run python collect.py        # AWS 문서 수집 → data/docs.jsonl   (수 분)
uv run python chunk.py          # → data/chunks.jsonl
uv run python build_index.py    # → data/index/   (CPU 면 수십 분, GPU 면 수 분)
```

수집 대상은 `sources.py` 에서 한 줄씩 추가/삭제.

## (선택) LLM 교체

`generator.py` 의 `LLM` 인터페이스(`stream_text`, `structured` 두 메서드)만 구현하면 Claude 대신 Local LLM 으로 바꿀 수 있다. 환경 변수 `LLM_BACKEND` 로 선택. 모델은 `CLAUDE_MODEL` 로 변경 가능(기본 `claude-opus-5-5`).

---

## 문제 해결

| 증상 | 해결 |
|---|---|
| `uv` 를 찾을 수 없음 | PowerShell 재시작. 그래도 안 되면 `$env:Path += ";$HOME\.local\bin"` |
| 한글이 `�` 또는 이상한 문자로 깨짐 | `setx PYTHONUTF8 1` 후 창 재시작. 직접 파일 열 때는 `open(..., encoding="utf-8")` |
| 모델 다운로드가 0KB/s 로 멈춤 | Ctrl+C 후 `uv run python download_model.py` 재실행(이어받기). 계속 느리면 핫스팟 또는 USB 복사 |
| `/recommend` 가 401/인증 오류 | `ANTHROPIC_API_KEY` 설정 확인: `echo $env:ANTHROPIC_API_KEY` |
| 팀원이 서버에 접속 못 함 | 같은 네트워크인지, 방화벽에서 Python 허용했는지, `--host 0.0.0.0` 으로 띄웠는지 확인 |
| `인덱스와 청크 수가 다름` 오류 | `chunk.py` 를 다시 돌렸다면 `build_index.py` 도 다시 실행 |
