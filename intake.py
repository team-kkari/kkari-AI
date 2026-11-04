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
    cost: str | None = Field(None, description="현재 월 비용 / 목표 예산, 약정(RI·SP) 보유 여부")
    goal: str | None = Field(None, description="이번 의뢰로 얻고 싶은 것 (한 문장)")
    priorities: list[str] = Field(default_factory=list,
                                  description=f"최적화 우선순위, 다음 값만 사용: {CATEGORIES}")
    change_tolerance: str | None = Field(None, description="변경 허용 범위: 다운타임 허용, 관리형·서버리스·Graviton 전환 가능 여부")
    constraints: str | None = Field(None, description="제약: 규제·컴플라이언스, 리전 고정, 기술 스택 제한")
    operations: str | None = Field(None, description="운영 체계: IaC(Terraform 등), 모니터링, 배포 방식, 운영 인력")
    security: str | None = Field(None, description="보안 요구: 데이터 민감도, 암호화, 접근 통제")


REQUIRED = ["workload", "architecture", "traffic", "availability", "cost", "priorities", "change_tolerance"]
LABELS = {
    "workload": "워크로드", "architecture": "현재 구성", "traffic": "트래픽", "availability": "가용성 목표",
    "performance": "성능 요구", "cost": "비용/예산", "goal": "목표", "priorities": "우선순위",
    "change_tolerance": "변경 허용 범위", "constraints": "제약", "operations": "운영 체계", "security": "보안 요구",
}


class Turn(BaseModel):
    reply: str = Field(description="고객에게 보낼 메시지: 직전 답변을 짧게 확인하고 다음 질문 1~2개. 마크다운 가능")
    quick_replies: list[str] = Field(description="고객이 눌러서 답할 수 있는 짧은 답변 후보 2~4개 (질문이 없으면 빈 목록)")
    profile: Profile = Field(description="지금까지 대화와 자동 수집 정보로 채운 요구사항 프로필 (모르는 항목은 null)")
    ready: bool = Field(description="필수 항목이 모두 채워졌거나 고객이 바로 진단을 원하면 true")


SYSTEM = f"""너는 클라우드 인프라 관리 서비스의 초기 상담 담당이다. 고객은 인프라 전문가이며,
자신의 AWS 인프라 최적화·운영을 우리 서비스에 의뢰하려고 한다.
목표는 질문을 통해 진단에 필요한 정보를 최대한 정확하게 모으는 것이다.

질문 원칙:
- 한 번에 1~2개만 묻는다. 고객이 전문가이므로 기술 용어를 그대로 쓰고, 구체적인 수치를 요청한다 (예: "피크 RPS", "RTO/RPO", "월 비용 중 EC2 비중").
- 자동 수집된 인프라 정보나 이미 답한 내용은 다시 묻지 않는다. 대신 그 정보를 근거로 더 깊게 묻는다
  (예: 수집 정보에 CPU 12% 가 있으면 "이 사용률이 평소 수준인지, 특정 시간대에 피크가 있는지").
- 필수 항목({', '.join(LABELS[k] for k in REQUIRED)})을 먼저 채우고, 그다음 제약·운영 체계·보안·성능을 묻는다.
- 답이 모호하면 그 항목을 한 번 더 구체적으로 묻는다. 고객이 모른다고 하면 null 로 두고 넘어간다.
- quick_replies 는 실제로 자주 나올 법한 답변으로 만든다 (예: "다운타임 불가", "새벽 30분 허용").
- 필수 항목이 다 모이면 지금까지 파악한 내용을 3~5줄로 요약하고, 진단을 시작해도 되는지 확인하며 ready=true 로 한다.
- 고객이 "바로 진단해 줘" 같이 원하면 남은 항목이 있어도 ready=true 로 하고, 무엇이 비어 있는지 알려준다.
- 첫 턴(대화가 비어 있음)에는 짧게 인사하고 첫 질문을 한다.
- 모든 메시지는 한국어로, 정중하지만 간결하게 쓴다.

profile.priorities 에는 다음 값만 쓴다: {CATEGORIES}"""


def missing(p: Profile) -> list[str]:
    return [k for k in REQUIRED if not getattr(p, k)]


def to_recommend_request(p: Profile, infra_context: str = "") -> dict:
    """프로필 → /recommend 요청 body."""
    lines = [f"- {LABELS[k]}: {v}" for k, v in p.model_dump().items()
             if v and k not in ("goal", "priorities")]
    infra = "\n".join(([f"[자동 수집]\n{infra_context}", ""] if infra_context else []) + ["[상담 내용]"] + lines)
    return {"infra": infra, "goal": p.goal or "", "priorities": [c for c in p.priorities if c in CATEGORIES]}


class Intake:
    def __init__(self, llm):
        self.llm = llm

    def turn(self, history: list[dict], infra_context: str = "", session_id: str | None = None) -> dict:
        convo = "\n".join(f"{'상담원' if m['role'] == 'assistant' else '고객'}: {m['content']}" for m in history)
        user = (f"<자동_수집_인프라>\n{infra_context or '(없음)'}\n</자동_수집_인프라>\n\n"
                f"<대화>\n{convo or '(아직 없음 — 첫 턴)'}\n</대화>\n\n"
                "다음 상담원 응답을 만들어라.")
        t0 = time.time()
        out: Turn = self.llm.structured(SYSTEM, user, Turn, effort="low")
        p = out.profile
        p.priorities = [c for c in p.priorities if c in CATEGORIES]
        result = {
            "reply": out.reply,
            "quick_replies": out.quick_replies,
            "profile": p.model_dump(),
            "missing": [LABELS[k] for k in missing(p)],
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
