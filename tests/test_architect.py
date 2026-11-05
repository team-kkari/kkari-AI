"""architect: budget_usd / violations / region_warnings / Architect.run (가짜 LLM)."""
from types import SimpleNamespace

import anthropic
import pytest

from architect import Architect, Directions, Ranking, budget_usd, region_warnings, violations
from conftest import FakeLLM, FakeRetriever, api_status_error, direction_of, directions, ranking, sample_draft
from errors import ApiError, LLMOutputError
from generator import RAG
from schemas import ArchitectureDraft, ConstraintCheck, Constraints, CurrentInfra


# ───────────────────────── budget_usd ─────────────────────────


def test_budget_krw_converted():
    assert budget_usd(Constraints(monthly_budget=1_400_000, currency="KRW")) == pytest.approx(1000)
    assert budget_usd(Constraints(monthly_budget=1_400_000, currency="KRW", krw_per_usd=1000)) == pytest.approx(1400)


def test_budget_usd_as_is():
    assert budget_usd(Constraints(monthly_budget=500, currency="USD")) == 500


@pytest.mark.parametrize("c", [None, Constraints(), Constraints(monthly_budget=0, currency="KRW"),
                               Constraints(monthly_budget=None, currency="USD")])
def test_budget_none_or_zero(c):
    assert budget_usd(c) is None


# ───────────────────────── violations ─────────────────────────


def cost(total: float):
    return SimpleNamespace(monthly_total_usd=total)


def draft_with(*checks: tuple[str, bool]) -> ArchitectureDraft:
    return sample_draft(constraint_checks=[ConstraintCheck(constraint=k, satisfied=ok, detail=f"{k} 점검")
                                           for k, ok in checks])


def test_over_budget_checked_by_code():
    v = violations(draft_with(), cost(1500), Constraints(monthly_budget=1_400_000, currency="KRW"))
    assert len(v) == 1
    assert v[0]["constraint"] == "monthly_budget" and v[0]["checked_by"] == "code"
    assert "$500" in v[0]["detail"]


def test_within_budget_no_violation():
    assert violations(draft_with(), cost(999), Constraints(monthly_budget=1000, currency="USD")) == []


def test_frozen_and_personal_checked_by_ai():
    c = Constraints(frozen_resources=["RDS prod-db"], contains_personal_data=True)
    v = violations(draft_with(("frozen_resources", False), ("personal_data", False)), cost(1), c)
    assert [(x["constraint"], x["checked_by"]) for x in v] == [("frozen_resources", "ai"), ("personal_data", "ai")]
    assert v[0]["detail"] == "frozen_resources 점검"


def test_satisfied_checks_are_not_violations():
    c = Constraints(frozen_resources=["RDS prod-db"], contains_personal_data=True)
    assert violations(draft_with(("frozen_resources", True), ("personal_data", True)), cost(1), c) == []


@pytest.mark.parametrize("c", [None, Constraints(), Constraints(frozen_resources=[], contains_personal_data=False),
                               Constraints(monthly_budget=0)])
def test_irrelevant_checks_ignored(c):
    """해당 제약이 없으면 AI 점검 결과가 '미충족'이어도, 예산이 없으면 비용이 커도 무시."""
    assert violations(draft_with(("frozen_resources", False), ("personal_data", False)), cost(10**6), c) == []


# ───────────────────────── region_warnings ─────────────────────────


@pytest.mark.parametrize("infra, region", [("us-east-1 에 EC2 4대", "us-east-1"), ("도쿄 리전 RDS", "도쿄"),
                                           ("EU: eu-west-1", "eu-west-1")])
def test_other_region_warns(infra, region):
    w = region_warnings(infra)
    assert len(w) == 1 and region in w[0] and "ap-northeast-2" in w[0]


def test_seoul_region_ignored():
    assert region_warnings("ap-northeast-2 에 EC2 4대, 서울 리전") == []


def test_mixed_regions_lists_only_others():
    w = region_warnings("ap-northeast-2 운영, DR 은 ap-northeast-1")
    assert "ap-northeast-1" in w[0] and "ap-northeast-2," not in w[0]


# ───────────────────────── Architect.run ─────────────────────────

TITLES = ["안 A", "안 B", "안 C"]


def make_llm(drafts: dict, ranks=(1, 2, 3)) -> FakeLLM:
    """drafts: 방향 title → ArchitectureDraft 또는 예외."""
    return FakeLLM({
        Directions: directions(list(drafts)),
        ArchitectureDraft: lambda system, user: drafts[direction_of(user)],
        CurrentInfra: CurrentInfra(nodes=sample_draft().diagram.nodes[:3]),
        Ranking: ranking(list(ranks)),
    })


def run(llm, **kwargs):
    return Architect(RAG(retriever=FakeRetriever(), llm=llm), n=len(llm.responses[Directions].directions)).run(
        "EC2 m5.large 4대, RDS MySQL", "비용 절감", ["cost"], **kwargs)


def test_current_monthly_usd_skips_estimate():
    llm = make_llm({t: sample_draft(t) for t in TITLES})
    out = run(llm, current_monthly_usd=1000)
    assert CurrentInfra not in llm.calls
    assert out["current"]["source"] == "input"
    assert out["current"]["monthly_usd"] == 1000
    assert out["current"]["estimated_cost"] is None and out["current"]["nodes"] == []
    a = out["architectures"][0]
    assert a["vs_current"]["usd"] == pytest.approx(a["cost"]["monthly_total_usd"] - 1000, abs=0.01)


def test_without_current_monthly_usd_estimates():
    llm = make_llm({t: sample_draft(t) for t in TITLES})
    out = run(llm)
    assert llm.calls.count(CurrentInfra) == 1
    assert out["current"]["source"] == "estimated"
    assert out["current"]["monthly_usd"] > 0 and len(out["current"]["nodes"]) == 3


def test_model_rank_order_without_violations():
    llm = make_llm({t: sample_draft(t) for t in TITLES}, ranks=(3, 1, 2))
    out = run(llm)
    assert [a["name"] for a in out["architectures"]] == ["안 B", "안 C", "안 A"]
    assert [a["rank"] for a in out["architectures"]] == [1, 2, 3]


def test_violating_architecture_goes_last():
    drafts = {t: sample_draft(t) for t in TITLES}
    drafts["안 A"] = sample_draft("안 A", constraint_checks=[
        ConstraintCheck(constraint="frozen_resources", satisfied=False, detail="prod-db 엔진 변경")])
    out = run(make_llm(drafts, ranks=(1, 2, 3)), constraints=Constraints(frozen_resources=["RDS prod-db"]))
    names = [a["name"] for a in out["architectures"]]
    assert names == ["안 B", "안 C", "안 A"]  # 모델은 A 를 1위로 줬지만 위반이라 맨 뒤
    assert [a["rank"] for a in out["architectures"]] == [1, 2, 3]
    assert out["architectures"][-1]["violations"][0]["constraint"] == "frozen_resources"
    assert all(not a["violations"] for a in out["architectures"][:-1])
    assert out["constraints"]["frozen_resources"] == ["RDS prod-db"]


def test_budget_violation_in_run():
    out = run(make_llm({t: sample_draft(t) for t in TITLES}),
              constraints=Constraints(monthly_budget=1, currency="USD"))
    assert all(a["violations"][0]["checked_by"] == "code" for a in out["architectures"])
    assert [a["name"] for a in out["architectures"]] == TITLES  # 전부 위반이면 모델 순위대로


def test_partial_failure_reported():
    drafts = {t: sample_draft(t) for t in TITLES}
    drafts["안 B"] = api_status_error(anthropic.RateLimitError, 429)
    out = run(make_llm(drafts))
    assert out["failed"] == [{"title": "안 B", "code": "RATE_LIMITED"}]
    assert [a["name"] for a in out["architectures"]] == ["안 A", "안 C"]


def test_all_failed_raises_first_error():
    drafts = {"안 A": api_status_error(anthropic.RateLimitError, 429), "안 B": LLMOutputError("잘림")}
    llm = make_llm(drafts, ranks=(1, 2))
    with pytest.raises(ApiError) as ei:
        run(llm)
    assert (ei.value.status, ei.value.code, ei.value.retryable) == (429, "RATE_LIMITED", True)
    assert Ranking not in llm.calls


def test_all_failed_via_endpoint(client, monkeypatch):
    import server
    drafts = {"안 A": api_status_error(anthropic.RateLimitError, 429), "안 B": LLMOutputError("잘림")}
    monkeypatch.setattr(server.rag, "_llm", make_llm(drafts, ranks=(1, 2)))
    r = client.post("/architectures", json={"infra": "EC2", "count": 2})
    assert r.status_code == 429
    assert r.json()["code"] == "RATE_LIMITED" and r.json()["retryable"] is True


def test_region_warning_and_pricing_meta():
    llm = make_llm({t: sample_draft(t) for t in TITLES})
    out = Architect(RAG(retriever=FakeRetriever(), llm=llm), n=3).run("us-east-1 EC2 4대", current_monthly_usd=100)
    assert len(out["warnings"]) == 1 and "us-east-1" in out["warnings"][0]
    assert out["pricing"]["currency"] == "USD" and out["pricing"]["region"] == "ap-northeast-2"
