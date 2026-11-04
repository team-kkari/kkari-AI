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
| Claude API 키 | `/recommend`, `/chat` 에 필요. `/search` 는 키 없이 동작 |
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
- API 키 없이 `/recommend`, `/chat` 을 부르면 503 과 함께 설정 방법이 응답으로 온다
- 브라우저에서 <http://localhost:8000/docs> → Swagger 에서 바로 테스트 가능
- 처음 실행 시 **Windows 방화벽 허용 창**이 뜨면 "개인 네트워크" 허용 → 같은 와이파이의 팀원이 `http://<노트북IP>:8000` 으로 호출 가능
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
  "recommend_request": null   // ready=true 가 되면 {infra, goal, priorities} → 그대로 /recommend 에 POST
}
```

- 흐름: `/intake` 반복 → `ready: true` → `recommend_request` 를 `/recommend` 로 보내 솔루션 생성
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

### `POST /chat` — 챗봇 (스트리밍 텍스트)

```json
{ "question": "NAT Gateway 비용 줄이는 방법?", "history": [], "categories": null }
```

- 응답 본문: 답변 텍스트가 스트리밍으로 옴 (본문 속 `[1]`, `[2]` 는 출처 번호)
- 응답 헤더 `X-Sources`: 출처 목록 JSON (URL 인코딩됨 → `JSON.parse(decodeURIComponent(h))`)
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
