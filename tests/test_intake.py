"""intake.missing / is_filled / to_recommend_request."""
import pytest

from conftest import TURN, llm_profile
from intake import CONSTRAINT_FIELDS, REQUIRED, Profile, Turn, is_filled, missing, to_recommend_request


# ───────────────────────── monthly_budget ─────────────────────────


def test_budget_zero_is_filled():
    assert is_filled(Profile(monthly_budget=0), "monthly_budget")  # 0 = 정해진 예산 없음


def test_budget_amount_is_filled_without_currency():
    """예산은 USD 로 통일 → 금액만 있으면 채워진 것."""
    assert is_filled(Profile(monthly_budget=3000), "monthly_budget")


def test_budget_none_is_missing():
    assert not is_filled(Profile(), "monthly_budget")
    assert not is_filled(Profile(currency="USD"), "monthly_budget")


# ───────────────────────── frozen_resources ─────────────────────────


def test_frozen_empty_list_is_filled():
    assert is_filled(Profile(frozen_resources=[]), "frozen_resources")  # [] = 없음 으로 확인됨
    assert is_filled(Profile(frozen_resources=["RDS prod-db"]), "frozen_resources")


def test_frozen_none_is_missing():
    assert not is_filled(Profile(), "frozen_resources")


# ───────────────────────── contains_personal_data ─────────────────────────


@pytest.mark.parametrize("v", [True, False])
def test_personal_data_bool_is_filled(v):
    assert is_filled(Profile(contains_personal_data=v), "contains_personal_data")


def test_personal_data_none_is_missing():
    assert not is_filled(Profile(), "contains_personal_data")


# ───────────────────────── missing / required_fields ─────────────────────────


def test_missing_default_is_required_list():
    assert missing(Profile()) == REQUIRED


def test_missing_only_checks_given_fields():
    p = Profile(workload="쇼핑몰", monthly_budget=0)
    assert missing(p, ["workload", "monthly_budget", "traffic"]) == ["traffic"]
    assert missing(p, ["workload"]) == []
    # required 에 없는 security 는 비어 있어도 안 나옴
    assert "security" not in missing(p, ["traffic"])


def test_missing_empty_required_is_empty():
    assert missing(Profile(), []) == []  # 빈 목록을 기본 목록으로 바꾸지 않음


def test_intake_endpoint_uses_required_fields(client, llm):
    llm.responses[Turn] = TURN.model_copy(update={"profile": llm_profile(workload="쇼핑몰")})
    r = client.post("/intake", json={"required_fields": ["workload", "traffic"]})
    assert r.status_code == 200, r.text
    assert r.json()["missing_fields"] == ["traffic"]
    assert r.json()["missing"] == ["트래픽"]


# ───────────────────────── to_recommend_request ─────────────────────────


def test_to_recommend_request_splits_constraints():
    p = Profile(workload="쇼핑몰", architecture="EC2 m5.large 4대", priorities=["cost", "nope"], goal="비용 절감",
                monthly_budget=3_000_000, currency="KRW", contains_personal_data=True,
                frozen_resources=["RDS prod-db 엔진·버전"])
    req = to_recommend_request(p, infra_context="CPU 12%")
    assert req["constraints"] == {"monthly_budget": 3_000_000, "currency": "KRW",
                                  "contains_personal_data": True, "frozen_resources": ["RDS prod-db 엔진·버전"]}
    assert set(req["constraints"]) == set(CONSTRAINT_FIELDS)
    infra = req["infra"]
    assert "쇼핑몰" in infra and "EC2 m5.large 4대" in infra and "CPU 12%" in infra
    for word in ("월 예산", "예산 통화", "개인정보", "변경 금지", "3000000", "KRW", "RDS prod-db"):
        assert word not in infra, word
    assert req["priorities"] == ["cost"]
    assert req["goal"] == "비용 절감"


def test_to_recommend_request_keeps_none_constraints():
    req = to_recommend_request(Profile(workload="x"))
    assert req["constraints"] == {k: None for k in CONSTRAINT_FIELDS}


def test_llm_profile_to_profile_converts_sentinels():
    """Claude 출력의 "" / -1 / unknown → API 응답의 null, none → [], yes/no → bool."""
    p = llm_profile().to_profile()
    assert p.workload is None and p.monthly_budget is None and p.currency is None
    assert p.contains_personal_data is None and p.frozen_resources is None
    p = llm_profile(workload="쇼핑몰", monthly_budget=0, contains_personal_data="no", frozen_status="none",
                    priorities=["cost", "nope"]).to_profile()
    assert p.workload == "쇼핑몰" and p.monthly_budget == 0 and p.contains_personal_data is False
    assert p.frozen_resources == [] and p.priorities == ["cost"]
    p = llm_profile(monthly_budget=3570, contains_personal_data="yes", frozen_status="listed",
                    frozen_resources=["RDS prod-db MySQL 8.0"]).to_profile()
    assert (p.monthly_budget, p.currency, p.contains_personal_data, p.frozen_resources) == \
        (3570, "USD", True, ["RDS prod-db MySQL 8.0"])


def test_company_name_is_required_first_and_mapped():
    """고객사명: 기본 필수 항목의 첫 번째, LLM 출력의 "" → null."""
    assert REQUIRED[0] == "company_name"
    assert "company_name" in missing(Profile())
    assert "company_name" not in missing(Profile(company_name="카리상사"))
    assert llm_profile().to_profile().company_name is None
    assert llm_profile(company_name="카리상사").to_profile().company_name == "카리상사"


def test_ready_forced_false_while_required_missing():
    """모델이 ready=true 라고 해도 필수(고객사명 등)가 남아 있으면 false. 고객이 건너뛰자고 하면 true."""
    from intake import decide_ready, skip_requested
    h = [{"role": "user", "content": "모든 전환 가능하고 비용 최우선이에요"}]
    assert decide_ready(True, ["company_name"], h) is False
    assert decide_ready(True, [], h) is True and decide_ready(False, [], h) is False
    for msg in ("고객사명은 나중에, 바로 진단해 주세요", "그냥 진행해 주세요", "검토 시작해 주세요", "그건 건너뛸게요", "다음으로 넘어갈게요"):
        assert skip_requested([{"role": "user", "content": msg}]), msg
        assert decide_ready(True, ["company_name"], [{"role": "user", "content": msg}]) is True


def test_intake_endpoint_ready_false_when_company_missing(client, llm):
    llm.responses[Turn] = TURN.model_copy(update={"ready": True, "profile": llm_profile(workload="데이터 파이프라인")})
    j = client.post("/intake", json={"history": [{"role": "user", "content": "비용 최우선이에요"}]}).json()
    assert j["ready"] is False and j["recommend_request"] is None and "company_name" in j["missing_fields"]


# ───────────────────────── infra_status (인프라 유무) ─────────────────────────


def test_default_required_by_infra_status():
    from intake import REQUIRED_EXISTING, REQUIRED_NEW, default_required
    assert default_required("EXISTING", "[AWS 분석] 리전: ap-northeast-1") == REQUIRED_EXISTING == ["monthly_budget", "priorities", "design_principles"]
    assert default_required("EXISTING", "") == REQUIRED          # 자동 수집 정보가 없으면 줄이지 않는다
    assert default_required("NONE", "") == REQUIRED_NEW
    assert "architecture" not in REQUIRED_NEW and "cost" not in REQUIRED_NEW and "frozen_resources" not in REQUIRED_NEW
    assert default_required(None, "x") == REQUIRED


def test_system_prompt_differs_by_infra_status():
    from intake import system_prompt
    existing = system_prompt(["monthly_budget", "priorities"], "EXISTING", "ctx")
    assert "이미 AWS 인프라가 있다" in existing and "고객사명은 의뢰를 만들 때 이미 받았으니 묻지 않는다" in existing
    assert "이미 AWS 인프라가 있다" not in system_prompt(["monthly_budget"], "EXISTING", "")
    new = system_prompt(["workload"], "NONE")
    assert "아직 AWS 인프라가 없다" in new and "워크로드" in new and "{required}" not in new
    assert "고객사명은 의뢰를 만들 때 이미 받았으니 묻지 않는다" in new


def test_intake_endpoint_existing_infra_asks_only_value_fields(client, llm):
    llm.responses[Turn] = TURN.model_copy(update={"ready": True, "profile": llm_profile(
        monthly_budget=500, priorities=["cost"], design_principles="Multi-AZ 필수")})
    j = client.post("/intake", json={"infra_status": "EXISTING", "infra_context": "[AWS 분석] EC2 2대",
                                     "history": [{"role": "user", "content": "비용이 제일 중요해요"}]}).json()
    assert j["missing_fields"] == [] and j["ready"] is True
    assert j["recommend_request"]["priorities"] == ["cost"]


def test_intake_endpoint_new_build_fills_defaults(client, llm):
    llm.responses[Turn] = TURN.model_copy(update={"ready": True, "profile": llm_profile(
        workload="쇼핑몰", traffic="동시 접속 100명", availability="하루 1시간 중단 가능", monthly_budget=300,
        contains_personal_data="yes", priorities=["reliability"], design_principles="특별한 원칙 없음")})
    j = client.post("/intake", json={"infra_status": "NONE", "history": [{"role": "user", "content": "x"}]}).json()
    assert j["missing_fields"] == [] and j["ready"] is True
    assert j["profile"]["frozen_resources"] == [] and "신규 구축" in j["profile"]["architecture"]
    assert "신규 구축" in j["recommend_request"]["infra"]


def test_intake_endpoint_rejects_unknown_infra_status(client):
    assert client.post("/intake", json={"infra_status": "MAYBE"}).status_code == 422


def test_design_principles_is_required_and_goes_to_goal():
    from intake import REQUIRED, REQUIRED_EXISTING, REQUIRED_NEW, LABELS
    assert all("design_principles" in r for r in (REQUIRED, REQUIRED_EXISTING, REQUIRED_NEW))
    assert LABELS["design_principles"] == "설계 원칙·고려사항"
    p = Profile(workload="쇼핑몰", goal="비용 절감", design_principles="Multi-AZ 필수, 서버리스 선호")
    req = to_recommend_request(p)
    assert req["goal"] == "비용 절감\n설계 원칙·고려사항: Multi-AZ 필수, 서버리스 선호"
    assert "Multi-AZ" not in req["infra"]           # 중복해서 infra 에 넣지 않는다
    assert to_recommend_request(Profile(goal="x"))["goal"] == "x"
    assert llm_profile(design_principles="리전 고정").to_profile().design_principles == "리전 고정"
    assert llm_profile().to_profile().design_principles is None


def test_system_prompt_asks_design_principles():
    from intake import system_prompt
    for prompt in (system_prompt(["design_principles"]), system_prompt(["design_principles"], "EXISTING", "ctx"),
                   system_prompt(["design_principles"], "NONE")):
        assert "설계 원칙" in prompt and "design_principles" in prompt
