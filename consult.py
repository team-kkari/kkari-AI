"""고객창구 — 비전공자용 검토 신청 상담 챗봇.

/intake 가 인프라 전문가용이라면, 고객창구는 클라우드를 잘 모르는 고객용이다.
- 쉬운 말로 한 번에 질문 하나, 선택형 질문은 버튼(options)으로
- 고객이 운영 중인 인프라 파일(Terraform·CloudFormation·비용 CSV·구성도 이미지·PDF 등)을 올리면
  파일에서 알 수 있는 질문은 건너뛴다. 안 올리면 질문이 더 많아진다
- 질문 목록은 팀 질문지(Q1~Q15, 필수/권장/선택/조건부) 기준

흐름 (서버는 상태 없음, 백엔드가 history 와 file_analysis 를 보관해 매 턴 보낸다)
    POST /consult/files  (선택) 파일 업로드 → file_analysis
    POST /consult        {history, file_analysis} 반복 → ready=true 면 recommend_request → /architectures
"""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

LOGS = Path(__file__).parent / "data" / "consult_logs"
WARMUP = "__warmup__"

# ───────────────────────── 질문지 (팀 정리본 기준) ─────────────────────────

Outcome = Literal["cost", "reliability", "performance", "security", "operations", "sustainability", "overall"]
OUTCOME_LABEL = {"cost": "요금 절감", "reliability": "안 멈추는 구조", "performance": "사람이 늘어도 버티는 구조",
                 "security": "보안 점검", "operations": "관리하기 편한 구조", "sustainability": "친환경 운영",
                 "overall": "전반 진단"}
ArchType = Literal["monolith", "three_tier", "serverless", "msa", "static_cdn", "container", "unknown", "not_asked"]
ARCH_LABEL = {"monolith": "서버 한 덩어리(모놀리식)", "three_tier": "웹·앱·DB 3단 구조", "serverless": "서버리스",
              "msa": "여러 작은 서비스(MSA)", "static_cdn": "정적 사이트 + CDN", "container": "컨테이너(쿠버네티스 등)",
              "unknown": "모름"}
Users = Literal["lt_100", "100_1k", "1k_10k", "10k_100k", "gt_100k", "unknown", "not_asked"]
USERS_LABEL = {"lt_100": "100명 미만", "100_1k": "100~1천 명", "1k_10k": "1천~1만 명", "10k_100k": "1만~10만 명",
               "gt_100k": "10만 명 이상", "unknown": "모름"}
Impact = Literal["minor", "some_loss", "major", "critical", "not_asked"]
IMPACT_LABEL = {"minor": "조금 불편한 정도", "some_loss": "일부 매출·업무 차질", "major": "큰 손해·고객 이탈",
                "critical": "법적 문제·안전 사고"}
Budget = Literal["amount", "keep_current", "reduce", "undecided", "not_asked"]
BUDGET_LABEL = {"amount": "금액을 정해 둠", "keep_current": "지금 수준이면 괜찮음", "reduce": "지금보다 줄었으면",
                "undecided": "아직 안 정함"}
Change = Literal["brief_downtime", "managed_services", "instance_change"]
CHANGE_LABEL = {"brief_downtime": "잠깐 서비스 중단", "managed_services": "AWS 관리형 서비스로 전환",
                "instance_change": "서버 종류 변경"}
Operator = Literal["self", "dev_team", "outsourced", "nobody", "not_asked"]
OPERATOR_LABEL = {"self": "제가 직접", "dev_team": "개발팀이 같이", "outsourced": "외주 업체", "nobody": "따로 없음"}
Connect = Literal["now", "later", "hard", "not_asked"]
CONNECT_LABEL = {"now": "지금 연결할게요", "later": "나중에 할게요", "hard": "방법을 잘 모르겠어요"}

QuestionId = Literal["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8", "Q9", "Q9-1", "Q10", "Q11", "Q12", "Q13",
                     "Q14", "Q15", "none"]


def _opts(d: dict) -> list[dict]:
    return [{"value": k, "label": v} for k, v in d.items()]


# 프론트가 질문을 그릴 때 쓰는 정보. input: text | single | multi | rank | budget | yes_no
QUESTIONS: dict[str, dict] = {
    "Q1": {"step": 1, "label": "어떤 고민이 있어서 검토를 신청하셨나요?", "priority": "required", "input": "text"},
    "Q2": {"step": 1, "label": "검토가 끝나면 어떤 걸 받아 보고 싶으세요? (여러 개 선택)", "priority": "required",
           "input": "multi", "options": _opts(OUTCOME_LABEL)},
    "Q3": {"step": 1, "label": "그중 가장 중요한 것부터 순서를 정해 주세요 (최대 3개)", "priority": "required", "input": "rank"},
    "Q4": {"step": 1, "label": "한 달 AWS 비용은 어느 정도까지 괜찮으세요?", "priority": "required", "input": "budget",
           "options": _opts(BUDGET_LABEL)},
    "Q5": {"step": 2, "label": "검토받을 서비스를 간단히 소개해 주세요", "priority": "required", "input": "text"},
    "Q6": {"step": 2, "label": "서비스는 어떤 구조로 만들어져 있나요?", "priority": "recommended", "input": "single",
           "options": _opts(ARCH_LABEL)},
    "Q7": {"step": 2, "label": "하루에 쓰는 사람은 대략 몇 명쯤 되나요?", "priority": "recommended", "input": "single",
           "options": _opts(USERS_LABEL)},
    "Q8": {"step": 2, "label": "사용자가 몰리는 때가 있나요? 1년 뒤에는 얼마나 늘 것 같나요?", "priority": "recommended",
           "input": "text"},
    "Q9": {"step": 3, "label": "서비스가 갑자기 멈추면 어떤 일이 생기나요?", "priority": "required", "input": "single",
           "options": _opts(IMPACT_LABEL)},
    "Q9-1": {"step": 3, "label": "최대 얼마나 멈춰도 되나요? 데이터는 얼마나 잃어도 되나요?", "priority": "conditional",
             "input": "text"},
    "Q10": {"step": 3, "label": "개인정보를 다루나요? 다룬다면 어떤 정보인가요?", "priority": "required", "input": "yes_no"},
    "Q11": {"step": 3, "label": "지켜야 하는 규정이나 인증이 있나요?", "priority": "optional", "input": "text"},
    "Q12": {"step": 3, "label": "이번 개선에서 손대면 안 되는 부분이 있나요?", "priority": "recommended", "input": "text"},
    "Q13": {"step": 3, "label": "이런 변화는 괜찮으세요? (여러 개 선택)", "priority": "recommended", "input": "multi",
            "options": _opts(CHANGE_LABEL)},
    "Q14": {"step": 3, "label": "지금 서버 관리는 누가 하고 계세요?", "priority": "optional", "input": "single",
            "options": _opts(OPERATOR_LABEL)},
    "Q15": {"step": 4, "label": "AWS 계정을 연결해 주세요 (읽기 권한만 사용해요)", "priority": "required", "input": "single",
            "options": _opts(CONNECT_LABEL)},
}

# ───────────────────────── 프로필 ─────────────────────────


class ConsultProfileLLM(BaseModel):
    """Claude 출력용. nullable 을 쓰지 않는다 (필드가 많으면 'Schema is too complex' 로 거부됨).
    모름/안 물어봄은 "" / -1 / "not_asked" 로 표시하고 to_public() 에서 null 로 바꾼다."""
    concern: str = Field(description="Q1 검토를 신청한 고민 (고객 말 요약). 모르면 빈 문자열")
    desired_outcomes: list[Outcome] = Field(description="Q2 받고 싶은 결과 (여러 개)")
    priority_order: list[Outcome] = Field(description="Q3 desired_outcomes 중 중요한 순서 (최대 3개)")
    budget_stance: Budget = Field(description="Q4 예산 태도")
    monthly_budget: float = Field(description="Q4 budget_stance=amount 일 때 월 금액(원화면 원 단위). 그 외 -1")
    currency: Literal["USD", "KRW", "unknown"] = Field(description="monthly_budget 통화. 원·만원이면 KRW")
    service_description: str = Field(description="Q5 서비스 소개 (무엇을 하는 서비스인지). 모르면 빈 문자열")
    architecture_type: ArchType = Field(description="Q6 서비스 구조. 고객이 모른다고 하면 unknown, 안 물어봤으면 not_asked")
    daily_users: Users = Field(description="Q7 하루 사용자 수 구간")
    traffic_pattern: str = Field(description="Q8 몰리는 시간·시즌, 1년 뒤 성장 예상. 모르면 빈 문자열")
    outage_impact: Impact = Field(description="Q9 서비스가 멈추면 생기는 일")
    recovery_target: str = Field(description="Q9-1 허용 중단 시간·데이터 손실 (예: '1시간 이내, 데이터는 거의 잃으면 안 됨')")
    personal_data: Literal["yes", "no", "not_asked"] = Field(description="Q10 개인정보 처리 여부")
    sensitive_data: str = Field(description="Q10 다루는 민감 정보 종류 (예: 이름·전화번호·결제정보). 없으면 빈 문자열")
    compliance: str = Field(description="Q11 지켜야 하는 규정·인증 (예: ISMS, 개인정보보호법). 모르면 빈 문자열")
    frozen_status: Literal["not_asked", "none", "listed"] = Field(description="Q12 손대면 안 되는 부분: 없음=none, 있음=listed")
    frozen_resources: list[str] = Field(description="Q12 frozen_status=listed 일 때 손대면 안 되는 부분")
    change_tolerance: list[Change] = Field(description="Q13 괜찮다고 한 변화")
    change_asked: bool = Field(description="Q13 을 물어봤고 고객이 답했으면 true (아무것도 안 된다고 해도 true)")
    operator: Operator = Field(description="Q14 서버 관리 담당")
    aws_connection: Connect = Field(description="Q15 AWS 계정 연결 의사")

    def to_public(self) -> dict:
        d = self.model_dump()
        out = {}
        for k, v in d.items():
            if v in ("", "not_asked", "unknown") and k not in ("architecture_type", "daily_users"):
                v = None
            out[k] = v
        out["architecture_type"] = None if self.architecture_type == "not_asked" else self.architecture_type
        out["daily_users"] = None if self.daily_users == "not_asked" else self.daily_users
        out["monthly_budget"] = None if self.monthly_budget < 0 else self.monthly_budget
        out["contains_personal_data"] = {"yes": True, "no": False}.get(self.personal_data)
        out["frozen_resources"] = {"not_asked": None, "none": []}.get(self.frozen_status, self.frozen_resources)
        out["change_tolerance"] = self.change_tolerance if self.change_asked else None
        out["priority_order"] = self.priority_order or (self.desired_outcomes[:1] if len(self.desired_outcomes) == 1 else [])
        for k in ("personal_data", "frozen_status", "change_asked"):
            out.pop(k)
        return out


def filled(p: ConsultProfileLLM) -> dict[str, bool]:
    """질문 ID → 답을 받았는지."""
    return {
        "Q1": bool(p.concern), "Q2": bool(p.desired_outcomes),
        "Q3": bool(p.priority_order) or len(p.desired_outcomes) == 1,
        "Q4": p.budget_stance != "not_asked" and (p.budget_stance != "amount" or (p.monthly_budget >= 0 and p.currency != "unknown")),
        "Q5": bool(p.service_description), "Q6": p.architecture_type != "not_asked", "Q7": p.daily_users != "not_asked",
        "Q8": bool(p.traffic_pattern), "Q9": p.outage_impact != "not_asked",
        "Q9-1": bool(p.recovery_target), "Q10": p.personal_data != "not_asked", "Q11": bool(p.compliance),
        "Q12": p.frozen_status != "not_asked", "Q13": p.change_asked, "Q14": p.operator != "not_asked",
        "Q15": p.aws_connection != "not_asked",
    }


def needs(p: ConsultProfileLLM, qid: str) -> bool:
    """조건부 질문이 지금 필요한지."""
    if qid == "Q9-1":
        return p.outage_impact in ("major", "critical")
    if qid == "Q3":
        return len(p.desired_outcomes) > 1
    return True


def missing(p: ConsultProfileLLM, priority: str) -> list[str]:
    f = filled(p)
    want = ("required", "conditional") if priority == "required" else (priority,)
    return [q for q, meta in QUESTIONS.items() if meta["priority"] in want and needs(p, q) and not f[q]]


def question_meta(qid: str, p: ConsultProfileLLM) -> dict | None:
    if qid not in QUESTIONS:
        return None
    m = {"id": qid, **QUESTIONS[qid]}
    if qid == "Q3":  # Q2 에서 고른 것만 보여 준다
        m["options"] = [{"value": o, "label": OUTCOME_LABEL[o]} for o in p.desired_outcomes]
    return m


CATEGORY = {"cost": "cost", "reliability": "reliability", "performance": "performance", "security": "security",
            "operations": "operations", "sustainability": "sustainability"}


def to_recommend_request(p: ConsultProfileLLM, analysis: dict | None = None) -> dict:
    """상담 결과 → /architectures 요청 body."""
    pub = p.to_public()
    lines = []
    label = {"service_description": "서비스", "concern": "고민", "architecture_type": "구조", "daily_users": "하루 사용자",
             "traffic_pattern": "트래픽·성장", "outage_impact": "멈추면 생기는 일", "recovery_target": "복구 목표",
             "sensitive_data": "민감 정보", "compliance": "규정·인증", "change_tolerance": "허용하는 변화",
             "operator": "서버 관리 담당", "budget_stance": "예산 태도"}
    enum_label = {"architecture_type": ARCH_LABEL, "daily_users": USERS_LABEL, "outage_impact": IMPACT_LABEL,
                  "operator": OPERATOR_LABEL, "budget_stance": BUDGET_LABEL}
    for k, name in label.items():
        v = pub.get(k)
        if v in (None, "", []):
            continue
        if k in enum_label:
            v = enum_label[k].get(v, v)
        if k == "change_tolerance":
            v = ", ".join(CHANGE_LABEL[c] for c in v) or "변경 불가"
        lines.append(f"- {name}: {v}")
    infra = []
    if analysis and analysis.get("technical_summary"):
        infra += ["[업로드 파일 분석]", analysis["technical_summary"]]
        if analysis.get("resources"):
            infra += ["리소스: " + "; ".join(analysis["resources"])]
        infra.append("")
    infra += ["[상담 내용 — 비전공 고객]"] + lines
    order = pub["priority_order"] or p.desired_outcomes
    priorities = list(dict.fromkeys(CATEGORY[o] for o in order if o in CATEGORY))
    budget = pub["monthly_budget"] if p.budget_stance == "amount" else (0 if p.budget_stance == "undecided" else None)
    cost = (analysis or {}).get("monthly_cost_usd")
    return {
        "infra": "\n".join(infra),
        "goal": p.concern + (" (지금보다 비용을 줄이고 싶음)" if p.budget_stance == "reduce" else ""),
        "priorities": priorities,
        "constraints": {"monthly_budget": budget, "currency": pub["currency"] if p.budget_stance == "amount" else None,
                        "contains_personal_data": pub["contains_personal_data"],
                        "frozen_resources": pub["frozen_resources"]},
        "current_monthly_usd": cost if cost and cost > 0 else None,
    }


# ───────────────────────── 대화 ─────────────────────────


class ConsultTurn(BaseModel):
    reply: str = Field(description="고객에게 보낼 메시지. 직전 답을 쉬운 말로 확인하고 질문 하나. 마크다운 가능")
    question_id: QuestionId = Field(description="이번 메시지에서 묻는 질문 ID. 요약·확인만 하면 none")
    quick_replies: list[str] = Field(description="눌러서 답할 수 있는 짧은 답 2~6개 (선택형 질문이면 보기 그대로)")
    profile: ConsultProfileLLM = Field(description="대화 전체와 업로드 파일 분석으로 채운 프로필 (매 턴 처음부터 다시 채운다)")
    ready: bool = Field(description="필수 질문이 다 끝나 요약을 보여 주고 검토를 시작해도 되면 true")


def _catalog() -> str:
    rows = []
    for q, m in QUESTIONS.items():
        opts = f" [보기: {', '.join(o['label'] for o in m.get('options', []))}]" if m.get("options") else ""
        rows.append(f"- {q} ({m['priority']}, {m['step']}단계) {m['label']}{opts}")
    return "\n".join(rows)


SYSTEM = f"""너는 클라우드 비용·구조 검토 서비스의 '고객창구' 상담원이다. 고객은 클라우드를 잘 모르는 비전공자다.
고객이 검토를 신청하면 아래 질문지로 필요한 정보를 모은다. 이 정보로 전문가 AI 가 AWS 아키텍처를 추천한다.

[질문지] (required=필수, recommended=권장, optional=선택, conditional=조건부)
{_catalog()}

[말투]
- 전문 용어를 쓰지 않는다. 꼭 써야 하면 괄호로 쉽게 풀어 준다 (예: "가용 영역(서로 떨어진 데이터센터)").
- 정중하고 따뜻하게, 짧게. 한 번에 질문 하나만 한다. 선택형 질문은 보기를 quick_replies 에 그대로 넣는다.
- "모름", "잘 모르겠어요" 도 좋은 답이다. 모른다고 하면 다그치지 말고 넘어간다.

[순서]
- 1단계 → 2단계 → 3단계 → 4단계 순서로 묻는다. 각 단계에서 required 를 먼저 묻는다.
- Q3 은 Q2 에서 2개 이상 골랐을 때만 묻는다. Q9-1 은 Q9 가 '큰 손해·고객 이탈' 또는 '법적 문제·안전 사고' 일 때만 묻고,
  "몇 시간까지 멈춰도 되는지", "몇 분 전 데이터까지는 잃어도 되는지" 처럼 쉬운 말로 묻는다.
- recommended 는 <업로드_파일_분석> 으로 이미 알 수 있으면 묻지 않는다. 파일이 없으면 recommended 도 묻는다.
- optional(Q11, Q14) 은 고객이 바로 답할 수 있게 짧게 한 번만 묻거나, 대화가 길어졌으면 건너뛴다.
- Q15(AWS 계정 연결)는 마지막에 묻는다. 읽기 권한만 쓰고 언제든 끊을 수 있다고 안심시킨다. '나중에', '어려워요' 도 괜찮다.

[업로드 파일]
- <업로드_파일_분석> 이 있으면 거기서 알 수 있는 것(구조·규모·사용 중인 서비스·현재 비용 등)은 묻지 않고 profile 에 채운다.
- 첫 메시지에서 "올려 주신 파일을 보니 ~ 로 보여요" 처럼 쉬운 말로 한두 줄 알려 준다. 틀렸으면 고객이 고칠 수 있게 한다.
- 파일 분석에 발견된 문제점(concerns)이 있으면 요약할 때 쉬운 말로 언급한다.

[첫 턴]
- 대화가 비어 있으면 짧게 인사하고, 파일을 올리면 질문이 줄어든다고 한 줄 안내한 뒤 Q1 을 묻는다
  (파일 분석이 이미 있으면 그 내용을 알려 주고 Q1 을 묻는다).

[마무리]
- 필수 질문이 모두 채워지면 지금까지 내용을 쉬운 말로 3~5줄 요약하고 "이대로 검토를 시작할까요?" 라고 묻고 ready=true.
- 고객이 "바로 검토해 주세요" 처럼 원하면 남은 질문이 있어도 ready=true 로 하고 무엇이 비었는지 알려 준다.

[profile]
- 매 턴 대화 전체와 파일 분석을 다시 읽어 처음부터 채운다 (이전 턴 값은 저장되지 않는다).
- 안 물어본 항목은 not_asked / 빈 문자열 / -1. 고객이 모른다고 한 Q6·Q7 은 unknown.
- 모든 메시지는 한국어."""


class Consult:
    def __init__(self, llm):
        self.llm = llm

    def turn(self, history: list[dict], file_analysis: dict | None = None, session_id: str | None = None) -> dict:
        convo = "\n".join(f"{'상담원' if m['role'] == 'assistant' else '고객'}: {m['content']}" for m in history)
        fa = json.dumps({k: v for k, v in (file_analysis or {}).items() if k != "files"}, ensure_ascii=False, indent=1) \
            if file_analysis else "(없음 — 고객이 파일을 올리지 않음)"
        user = (f"<업로드_파일_분석>\n{fa}\n</업로드_파일_분석>\n\n"
                f"<대화>\n{convo or '(아직 없음 — 첫 턴)'}\n</대화>\n\n다음 상담원 메시지를 만들어라.")
        t0 = time.time()
        out: ConsultTurn = self.llm.structured(SYSTEM, user, ConsultTurn, effort="low")
        p = out.profile
        req = missing(p, "required")
        result = {
            "reply": out.reply,
            "question": question_meta(out.question_id, p),
            "quick_replies": out.quick_replies,
            "profile": p.to_public(),
            "missing": req,
            "missing_labels": [QUESTIONS[q]["label"] for q in req],
            "recommended_missing": missing(p, "recommended"),
            "ready": out.ready,
            "recommend_request": to_recommend_request(p, file_analysis) if out.ready else None,
        }
        if session_id != WARMUP:
            _log(session_id, history, file_analysis, result, time.time() - t0)
        return result


def _log(session_id, history, file_analysis, result, elapsed):
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "session_id": session_id,
               "turn": sum(m["role"] == "user" for m in history), "elapsed_s": round(elapsed, 1),
               "has_files": bool(file_analysis), "history": history, **result}
        with (LOGS / f"{datetime.now():%Y-%m-%d}.jsonl").open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ───────────────────────── 파일 분석 ─────────────────────────

TEXT_EXT = {".tf", ".tfvars", ".hcl", ".json", ".yaml", ".yml", ".csv", ".tsv", ".txt", ".md", ".log", ".ini",
            ".conf", ".cfg", ".toml", ".xml", ".env", ".sh", ".dockerfile"}
IMAGE_TYPE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_FILES, MAX_BYTES, MAX_TEXT_CHARS = 10, 5 * 1024 * 1024, 150_000

# 비밀값은 Claude 에 보내기 전에 지운다
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[비공개 키 삭제됨]"),
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "[AWS 액세스 키 삭제됨]"),
    (re.compile(r"(?i)(aws_secret_access_key|secret_key|secret|password|passwd|pwd|token|api_key|apikey)"
                r"(\s*[=:]\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s,#\n]+)"), r"\1\2\"[삭제됨]\""),
]


def redact(text: str) -> tuple[str, int]:
    n = 0
    for pat, rep in SECRET_PATTERNS:
        text, k = pat.subn(rep, text)
        n += k
    return text, n


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


class FileNote(BaseModel):
    name: str
    note: str = Field(description="이 파일에서 무엇을 알았는지 한 줄 (쉬운 말)")


class FileAnalysis(BaseModel):
    summary: str = Field(description="고객에게 보여 줄 쉬운 요약 2~4문장 (전문 용어 최소화)")
    technical_summary: str = Field(description="전문가 AI 용 기술 요약: 리전, 서비스별 사양·대수, 네트워크, DB, 이중화 여부, 수치")
    architecture_type: ArchType = Field(description="구조 유형. 판단 불가면 unknown")
    region: str = Field(description="AWS 리전 코드 (예: ap-northeast-2). 모르면 빈 문자열")
    resources: list[str] = Field(description="주요 리소스와 사양·대수 (예: 'EC2 t3.large x2', 'RDS PostgreSQL db.t4g.medium 단일 AZ')")
    monthly_cost_usd: float = Field(description="파일에 나온 현재 월 비용(USD). 원화면 1400원/$ 로 환산. 없으면 -1")
    traffic_hint: str = Field(description="사용자 수·트래픽에 대한 단서. 없으면 빈 문자열")
    personal_data_hint: Literal["yes", "no", "unknown"] = Field(description="개인정보를 다루는 흔적 (users 테이블, 결제 등)")
    concerns: list[str] = Field(description="눈에 띄는 위험·낭비 (예: 'DB 가 한 곳에만 있음', 'S3 공개 설정') 쉬운 말로 0~5개")
    answered_questions: list[QuestionId] = Field(description="이 파일로 답이 되는 질문지 ID (예: Q6, Q7, Q8)")
    files: list[FileNote]


FILE_SYSTEM = """너는 AWS 인프라 분석가다. 고객(비전공자)이 올린 파일을 읽고 현재 클라우드 구성을 파악한다.
파일은 Terraform, CloudFormation, 쿠버네티스 YAML, 비용 CSV(Cost Explorer·청구서), 구성도 이미지, PDF 보고서 등일 수 있다.
- 파일에 실제로 있는 내용만 쓴다. 추측은 '추정' 이라고 밝힌다.
- '[삭제됨]' 표시는 보안을 위해 지운 비밀값이다. 언급하지 않는다.
- summary 는 비전공자가 이해할 쉬운 말, technical_summary 는 전문가용 (사양·대수·리전·이중화·월 비용 수치).
- 한국어로 쓴다. AWS 서비스명·설정값은 영어 그대로."""


def prepare_files(files: list[tuple[str, bytes]]) -> tuple[str, list[dict], list[dict], int]:
    """(텍스트 묶음, Claude 첨부 블록, 파일별 상태, 지운 비밀값 수). 지원하지 않는 형식은 상태에 사유를 적고 건너뛴다."""
    texts, blocks, status, redacted = [], [], [], 0
    budget = MAX_TEXT_CHARS
    for name, raw in files[:MAX_FILES]:
        ext = Path(name.lower()).suffix or ("." + name.lower())
        if name.lower() in ("dockerfile", ".env"):
            ext = "." + name.lower().lstrip(".")
        if len(raw) > MAX_BYTES:
            status.append({"name": name, "used": False, "reason": f"파일이 너무 큽니다 (최대 {MAX_BYTES // 1024 // 1024}MB)"})
        elif ext in IMAGE_TYPE:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": IMAGE_TYPE[ext],
                                                       "data": base64.b64encode(raw).decode()}})
            blocks.append({"type": "text", "text": f"(위 이미지 파일 이름: {name})"})
            status.append({"name": name, "used": True, "reason": ""})
        elif ext == ".pdf":
            blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                          "data": base64.b64encode(raw).decode()}, "title": name})
            status.append({"name": name, "used": True, "reason": ""})
        elif ext in TEXT_EXT:
            text, n = redact(decode(raw))
            redacted += n
            cut = len(text) > budget
            text = text[:max(budget, 0)]
            budget -= len(text)
            if not text.strip():
                status.append({"name": name, "used": False, "reason": "내용이 비어 있거나 분량 한도를 넘었습니다"})
                continue
            texts.append(f'<파일 이름="{name}">\n{text}\n</파일>')
            status.append({"name": name, "used": True, "reason": "앞부분만 사용 (분량 한도)" if cut else ""})
        elif ext in (".xlsx", ".xls"):
            status.append({"name": name, "used": False, "reason": "엑셀은 CSV 로 저장해서 올려 주세요"})
        else:
            status.append({"name": name, "used": False, "reason": "지원하지 않는 형식입니다"})
    for name, _ in files[MAX_FILES:]:
        status.append({"name": name, "used": False, "reason": f"한 번에 최대 {MAX_FILES}개까지 분석합니다"})
    return "\n\n".join(texts), blocks, status, redacted


def analyze_files(llm, files: list[tuple[str, bytes]]) -> dict:
    """업로드 파일 → file_analysis (POST /consult 에 그대로 넘긴다). 쓸 수 있는 파일이 없으면 ValueError."""
    text, blocks, status, redacted = prepare_files(files)
    if not text and not blocks:
        raise ValueError("분석할 수 있는 파일이 없습니다: " + "; ".join(f"{s['name']}({s['reason']})" for s in status))
    user = (f"<텍스트_파일>\n{text or '(없음)'}\n</텍스트_파일>\n\n"
            "위 파일(과 첨부한 이미지·PDF)로 고객의 현재 클라우드 구성을 분석해라.")
    out: FileAnalysis = llm.structured(FILE_SYSTEM, user, FileAnalysis, effort="medium", attachments=blocks or None)
    d = out.model_dump()
    d["monthly_cost_usd"] = None if out.monthly_cost_usd < 0 else out.monthly_cost_usd
    d["region"] = out.region or None
    notes = {n.name: n.note for n in out.files}
    d["files"] = [{**s, "note": notes.get(s["name"], "")} for s in status]
    d["redacted_secrets"] = redacted
    return d
