"""초기 상담 인터뷰 챗봇.

고객(인프라 전문가)이 클라우드 관리를 의뢰하러 왔을 때, 챗봇이 먼저 질문하면서
요구사항 프로필을 채운다. 필수 항목이 모이면 /recommend 요청 데이터를 만들어 준다.

서버는 상태를 저장하지 않는다. 클라이언트가 매 턴 대화 기록 전체를 보낸다.

    from intake import Intake
    bot = Intake(llm)
    r = bot.turn(history=[], infra_context="(백엔드가 수집한 인프라 요약)")
    print(r["reply"], r["quick_replies"])
    history += [{"role": "assistant", "content": r["reply"]}, {"role": "user", "content": "답변"}]
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from typing import Literal

from pydantic import BaseModel, Field

from retriever import CATEGORIES

LOGS = Path(__file__).parent / "data" / "intake_logs"
WARMUP = "__warmup__"  # 서버 시작 시 미리 한 번 호출 (로그 제외)

# ───────────────────────── 프로필 ─────────────────────────


class Profile(BaseModel):
    workload: str | None = Field(None, description="서비스/워크로드 설명 (무엇을 하는 시스템인지, 사용자 규모)")
    architecture: str | None = Field(None, description="현재 구성: 주요 AWS 서비스·인스턴스 타입·대수·리전")
    traffic: str | None = Field(None, description="트래픽 패턴: 평균/피크, 시간대·계절성, 증가 추세")
    availability: str | None = Field(None, description="가용성 목표: SLA, RTO/RPO, 허용 장애 범위")
    performance: str | None = Field(None, description="성능 요구: 응답 지연, 처리량 목표")
    cost: str | None = Field(None, description="현재 월 비용과 비용 구성, 약정(RI·SP) 보유 여부 (예산 금액은 monthly_budget 에)")
    monthly_budget: float | None = Field(None, description=(
        "고객이 밝힌 월 예산 금액 (숫자만, 예: 3000000). 정해진 예산이 없다고 답하면 0, 아직 안 물어봤으면 null"))
    currency: Literal["USD", "KRW"] | None = Field(None, description="monthly_budget 의 통화. 원/만원이면 KRW(금액은 원 단위로 환산), 달러면 USD")
    contains_personal_data: bool | None = Field(None, description="개인정보(이름·연락처·주소·결제정보 등)를 저장·처리하는지. 확인 전이면 null")
    frozen_resources: list[str] | None = Field(None, description=(
        "변경하면 안 되는 리소스 목록 (예: 'RDS prod-db 엔진·버전', '온프레미스 연동 VPN'). "
        "고객이 '없다'고 답하면 빈 목록 [], 아직 안 물어봤으면 null"))
    goal: str | None = Field(None, description="이번 의뢰로 얻고 싶은 것 (한 문장)")
    priorities: list[str] = Field(default_factory=list,
                                  description=f"최적화 우선순위, 다음 값만 사용: {CATEGORIES}")
    change_tolerance: str | None = Field(None, description="변경 허용 범위: 다운타임 허용, 관리형·서버리스·Graviton 전환 가능 여부")
    constraints: str | None = Field(None, description="제약: 규제·컴플라이언스, 리전 고정, 기술 스택 제한")
    operations: str | None = Field(None, description="운영 체계: IaC(Terraform 등), 모니터링, 배포 방식, 운영 인력")
    security: str | None = Field(None, description="보안 요구: 데이터 민감도, 암호화, 접근 통제")


REQUIRED = ["workload", "architecture", "traffic", "availability", "cost", "monthly_budget",
            "contains_personal_data", "frozen_resources", "priorities", "change_tolerance"]
LABELS = {
    "workload": "워크로드", "architecture": "현재 구성", "traffic": "트래픽", "availability": "가용성 목표",
    "performance": "성능 요구", "cost": "현재 비용", "monthly_budget": "월 예산", "currency": "예산 통화",
    "contains_personal_data": "개인정보 포함 여부", "frozen_resources": "변경 금지 리소스", "goal": "목표",
    "priorities": "우선순위", "change_tolerance": "변경 허용 범위", "constraints": "제약", "operations": "운영 체계",
    "security": "보안 요구",
}
FIELDS = list(Profile.model_fields)  # required_fields 로 지정 가능한 키
CONSTRAINT_FIELDS = ["monthly_budget", "currency", "contains_personal_data", "frozen_resources"]


class Turn(BaseModel):
    reply: str = Field(description="고객에게 보낼 메시지: 직전 답변을 짧게 확인하고 다음 질문 1~2개. 마크다운 가능")
    quick_replies: list[str] = Field(description="고객이 눌러서 답할 수 있는 짧은 답변 후보 2~4개 (질문이 없으면 빈 목록)")
    profile: Profile = Field(description="지금까지 대화와 자동 수집 정보로 채운 요구사항 프로필 (모르는 항목은 null)")
    ready: bool = Field(description="필수 항목이 모두 채워졌거나 고객이 바로 진단을 원하면 true")


SYSTEM_TMPL = """너는 클라우드 인프라 관리 서비스의 초기 상담 담당이다. 고객은 인프라 전문가이며,
자신의 AWS 인프라 최적화·운영을 우리 서비스에 의뢰하려고 한다.
목표는 질문을 통해 진단에 필요한 정보를 최대한 정확하게 모으는 것이다.

질문 원칙:
- 한 번에 1~2개만 묻는다. 고객이 전문가이므로 기술 용어를 그대로 쓰고, 구체적인 수치를 요청한다 (예: "피크 RPS", "RTO/RPO", "월 비용 중 EC2 비중").
- 자동 수집된 인프라 정보나 이미 답한 내용은 다시 묻지 않는다. 대신 그 정보를 근거로 더 깊게 묻는다
  (예: 수집 정보에 CPU 12% 가 있으면 "이 사용률이 평소 수준인지, 특정 시간대에 피크가 있는지").
- 필수 항목({required})을 먼저 채우고, 그다음 나머지 항목을 묻는다.
- 월 예산은 금액과 통화를 함께 확인한다 (예: "월 예산이 있으신가요? 원화/달러 중 어느 기준인가요?"). 예산이 없다고 하면
  monthly_budget 을 0 으로 하고 다시 묻지 않는다.
- 개인정보 포함 여부는 예/아니오로 확인한다. 변경 금지 리소스는 "절대 바꾸면 안 되는 리소스(엔진·버전·네트워크 연동 등)"가
  있는지 묻고, 없다고 하면 frozen_resources 를 빈 목록 [] 으로 한다.
- 답이 모호하면 그 항목을 한 번 더 구체적으로 묻는다. 고객이 모른다고 하면 null 로 두고 넘어간다.
- quick_replies 는 실제로 자주 나올 법한 답변으로 만든다 (예: "다운타임 불가", "새벽 30분 허용").
- 필수 항목이 다 모이면 지금까지 파악한 내용을 3~5줄로 요약하고, 진단을 시작해도 되는지 확인하며 ready=true 로 한다.
- 고객이 "바로 진단해 줘" 같이 원하면 남은 항목이 있어도 ready=true 로 하고, 무엇이 비어 있는지 알려준다.
- 첫 턴(대화가 비어 있음)에는 짧게 인사하고 첫 질문을 한다.
- 모든 메시지는 한국어로, 정중하지만 간결하게 쓴다.

profile.priorities 에는 다음 값만 쓴다: {categories}"""


def system_prompt(required: list[str]) -> str:
    return SYSTEM_TMPL.format(required=", ".join(LABELS.get(k, k) for k in required), categories=CATEGORIES)


def is_filled(p: Profile, key: str) -> bool:
    v = getattr(p, key)
    if key == "priorities":
        return bool(v)
    if key == "frozen_resources":
        return v is not None               # [] = "없음" 으로 확인됨
    if key == "monthly_budget":            # 0 = "정해진 예산 없음" 으로 확인됨
        return v == 0 or (v is not None and p.currency is not None)
    if isinstance(v, bool):
        return True
    return bool(v)


def missing(p: Profile, required: list[str] | None = None) -> list[str]:
    return [k for k in (required or REQUIRED) if not is_filled(p, k)]


def to_recommend_request(p: Profile, infra_context: str = "") -> dict:
    """프로필 → /architectures (또는 /recommend) 요청 body. 제약 4종은 문장이 아니라 constraints 로 분리."""
    lines = [f"- {LABELS[k]}: {v}" for k, v in p.model_dump().items()
             if v and k not in ("goal", "priorities", *CONSTRAINT_FIELDS)]
    infra = "\n".join(([f"[자동 수집]\n{infra_context}", ""] if infra_context else []) + ["[상담 내용]"] + lines)
    return {"infra": infra, "goal": p.goal or "", "priorities": [c for c in p.priorities if c in CATEGORIES],
            "constraints": {k: getattr(p, k) for k in CONSTRAINT_FIELDS}}


class Intake:
    def __init__(self, llm):
        self.llm = llm

    def turn(self, history: list[dict], infra_context: str = "", session_id: str | None = None,
             required: list[str] | None = None) -> dict:
        required = required or REQUIRED
        convo = "\n".join(f"{'상담원' if m['role'] == 'assistant' else '고객'}: {m['content']}" for m in history)
        user = (f"<자동_수집_인프라>\n{infra_context or '(없음)'}\n</자동_수집_인프라>\n\n"
                f"<대화>\n{convo or '(아직 없음 — 첫 턴)'}\n</대화>\n\n"
                f"<필수_항목>{', '.join(required)}</필수_항목>\n\n"
                "다음 상담원 응답을 만들어라.")
        t0 = time.time()
        out: Turn = self.llm.structured(system_prompt(required), user, Turn, effort="low")
        p = out.profile
        p.priorities = [c for c in p.priorities if c in CATEGORIES]
        result = {
            "reply": out.reply,
            "quick_replies": out.quick_replies,
            "profile": p.model_dump(),
            "missing": [LABELS.get(k, k) for k in missing(p, required)],
            "missing_fields": missing(p, required),
            "ready": out.ready,
            "recommend_request": to_recommend_request(p, infra_context) if out.ready else None,
        }
        if session_id != WARMUP:
            self._log(session_id, history, infra_context, result, time.time() - t0)
        return result

    @staticmethod
    def _log(session_id, history, infra_context, result, elapsed):
        """대화 데이터 축적 (추후 분석/학습용). 실패해도 응답에는 영향 없음."""
        try:
            LOGS.mkdir(parents=True, exist_ok=True)
            rec = {"ts": datetime.now().isoformat(timespec="seconds"), "session_id": session_id,
                   "turn": sum(m["role"] == "user" for m in history), "elapsed_s": round(elapsed, 1),
                   "infra_context": infra_context, "history": history, **result}
            with (LOGS / f"{datetime.now():%Y-%m-%d}.jsonl").open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
