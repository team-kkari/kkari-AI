"""intake.missing / is_filled / to_recommend_request."""
import pytest

from conftest import TURN, llm_profile
from intake import CONSTRAINT_FIELDS, REQUIRED, Profile, Turn, is_filled, missing, to_recommend_request


# ───────────────────────── monthly_budget ─────────────────────────


def test_budget_zero_is_filled():
    assert is_filled(Profile(monthly_budget=0), "monthly_budget")  # 0 = 정해진 예산 없음


def test_budget_without_currency_is_missing():
    assert not is_filled(Profile(monthly_budget=3_000_000), "monthly_budget")
    assert is_filled(Profile(monthly_budget=3_000_000, currency="KRW"), "monthly_budget")


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
    p = llm_profile(monthly_budget=5_000_000, currency="KRW", contains_personal_data="yes", frozen_status="listed",
                    frozen_resources=["RDS prod-db MySQL 8.0"]).to_profile()
    assert (p.monthly_budget, p.currency, p.contains_personal_data, p.frozen_resources) == \
        (5_000_000, "KRW", True, ["RDS prod-db MySQL 8.0"])
