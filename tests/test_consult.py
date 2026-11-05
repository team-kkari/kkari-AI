"""고객창구 — 질문 진행 로직, 프로필 변환, /architectures 요청 변환, 파일 처리(비밀값 삭제), 엔드포인트."""
import pytest

from consult import (ConsultProfileLLM, ConsultTurn, FileAnalysis, analyze_files, filled, missing, prepare_files,
                     question_meta, redact, to_recommend_request)


def prof(**kw) -> ConsultProfileLLM:
    base = dict(concern="", desired_outcomes=[], priority_order=[], budget_stance="not_asked", monthly_budget=-1,
                service_description="", architecture_type="not_asked", daily_users="not_asked",
                traffic_pattern="", outage_impact="not_asked", recovery_target="", personal_data="not_asked",
                sensitive_data="", compliance="", frozen_status="not_asked", frozen_resources=[], change_tolerance=[],
                change_asked=False, operator="not_asked", aws_connection="not_asked")
    base.update(kw)
    return ConsultProfileLLM(**base)


FULL = dict(concern="요금이 많이 나와요", desired_outcomes=["cost", "reliability"], priority_order=["cost", "reliability"],
            budget_stance="amount", monthly_budget=200, service_description="꽃집 주문 사이트",
            outage_impact="minor", personal_data="yes", sensitive_data="이름·전화번호", aws_connection="later")


def test_required_questions_and_conditionals():
    assert missing(prof(), "required") == ["Q1", "Q2", "Q4", "Q5", "Q9", "Q10", "Q15"]  # Q3·Q9-1 은 조건부
    assert missing(prof(**FULL), "required") == []
    # Q3: 2개 이상 골랐을 때만
    assert "Q3" in missing(prof(**{**FULL, "priority_order": []}), "required")
    assert "Q3" not in missing(prof(**{**FULL, "desired_outcomes": ["cost"], "priority_order": []}), "required")
    # Q9-1: 영향이 클 때만
    assert "Q9-1" in missing(prof(**{**FULL, "outage_impact": "major"}), "required")
    assert "Q9-1" not in missing(prof(**{**FULL, "outage_impact": "major", "recovery_target": "1시간"}), "required")
    # Q4: 금액을 정했다면 금액·통화까지
    assert not filled(prof(budget_stance="amount"))["Q4"]          # 금액을 정했다면 금액까지
    assert filled(prof(budget_stance="amount", monthly_budget=200))["Q4"]
    assert filled(prof(budget_stance="reduce"))["Q4"]


def test_recommended_missing_shrinks_when_known():
    assert missing(prof(), "recommended") == ["Q6", "Q7", "Q8", "Q12", "Q13"]
    p = prof(architecture_type="unknown", daily_users="100_1k", traffic_pattern="명절에 몰림", frozen_status="none",
             change_asked=True)
    assert missing(p, "recommended") == []  # '모름'(unknown)도 답으로 인정


def test_to_public_turns_sentinels_into_null():
    pub = prof().to_public()
    for k in ("concern", "monthly_budget", "currency", "architecture_type", "contains_personal_data",
              "frozen_resources", "change_tolerance", "aws_connection"):
        assert pub[k] is None, k
    pub = prof(**FULL, frozen_status="none", change_asked=True, architecture_type="unknown").to_public()
    assert pub["contains_personal_data"] is True and pub["frozen_resources"] == [] and pub["change_tolerance"] == []
    assert pub["architecture_type"] == "unknown" and pub["currency"] == "USD" and pub["monthly_budget"] == 200


def test_recommend_request_for_architectures():
    analysis = {"technical_summary": "EC2 t3.large x2, RDS 단일 AZ", "resources": ["EC2 t3.large x2"],
                "monthly_cost_usd": 245.0}
    req = to_recommend_request(prof(**FULL, frozen_status="listed", frozen_resources=["주문 DB"]), analysis)
    assert req["priorities"] == ["cost", "reliability"]
    assert req["constraints"] == {"monthly_budget": 200, "currency": "USD", "contains_personal_data": True,
                                  "frozen_resources": ["주문 DB"]}
    assert req["current_monthly_usd"] == 245.0
    assert "[업로드 파일 분석]" in req["infra"] and "꽃집 주문 사이트" in req["infra"]
    # 예산 '줄였으면' → 금액 제약 없음 + 목표에 반영, '전반 진단' 은 카테고리 없음
    req = to_recommend_request(prof(**{**FULL, "budget_stance": "reduce", "desired_outcomes": ["overall"],
                                       "priority_order": []}))
    assert req["constraints"]["monthly_budget"] is None and "줄이고" in req["goal"] and req["priorities"] == []
    assert req["current_monthly_usd"] is None


def test_question_meta_rank_shows_only_picked():
    m = question_meta("Q3", prof(desired_outcomes=["cost", "security"]))
    assert [o["value"] for o in m["options"]] == ["cost", "security"] and m["input"] == "rank"
    assert question_meta("none", prof()) is None


def test_redact_secrets():
    text, n = redact('db_password = "Hunter2!"\nkey = AKIAABCDEFGHIJKLMNOP\ntoken: abc123\n'
                     "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\nregion = \"ap-northeast-2\"")
    assert n == 4 and "Hunter2" not in text and "AKIAABCDEFGHIJKLMNOP" not in text and "abc123" not in text
    assert "MIIE" not in text and 'region = "ap-northeast-2"' in text


def test_prepare_files_types_and_limits():
    files = [("main.tf", b'resource "aws_instance" "a" {}'), ("diagram.png", b"\x89PNG"), ("report.pdf", b"%PDF"),
             ("cost.xlsx", b"x"), ("app.exe", b"x"), ("big.csv", b"a" * (5 * 1024 * 1024 + 1)),
             ("euckr.csv", "서비스,비용\nEC2,100".encode("cp949"))]
    text, blocks, status, _ = prepare_files(files)
    st = {s["name"]: s for s in status}
    assert st["main.tf"]["used"] and st["diagram.png"]["used"] and st["report.pdf"]["used"]
    assert not st["cost.xlsx"]["used"] and "CSV" in st["cost.xlsx"]["reason"]
    assert not st["app.exe"]["used"] and not st["big.csv"]["used"]
    assert "서비스,비용" in text                              # cp949(엑셀 CSV) 도 읽힘
    assert [b["type"] for b in blocks if b["type"] != "text"] == ["image", "document"]


def _analysis(**kw):
    base = dict(summary="3단 구조예요", technical_summary="ALB → EC2 x2 → RDS", architecture_type="three_tier",
                region="", resources=["EC2 t3.large x2"], monthly_cost_usd=-1, traffic_hint="", personal_data_hint="unknown",
                concerns=[], answered_questions=["Q6"], files=[{"name": "main.tf", "note": "Terraform 구성"}])
    base.update(kw)
    return FileAnalysis(**base)


def test_analyze_files_sends_attachments_and_cleans_output(llm):
    llm.responses[FileAnalysis] = _analysis()
    out = analyze_files(llm, [("main.tf", b'password = "x"'), ("d.png", b"\x89PNG")])
    assert out["monthly_cost_usd"] is None and out["region"] is None and out["redacted_secrets"] == 1
    assert out["files"][0] == {"name": "main.tf", "used": True, "reason": "", "note": "Terraform 구성"}
    assert llm.attachments and llm.attachments[0]["type"] == "image"
    with pytest.raises(ValueError):
        analyze_files(llm, [("a.exe", b"x")])


def _turn(**kw):
    return ConsultTurn(reply="어떤 고민이 있으세요?", question_id="Q1", quick_replies=["요금이 많이 나와요"],
                       profile=prof(**kw.pop("profile", {})), ready=kw.pop("ready", False))


def test_consult_endpoint_first_turn(client, llm):
    llm.responses[ConsultTurn] = _turn()
    r = client.post("/consult", json={"history": [], "session_id": "t"})
    assert r.status_code == 200
    j = r.json()
    assert j["question"]["id"] == "Q1" and j["question"]["input"] == "text"
    assert j["missing"][0] == "Q1" and j["recommend_request"] is None and j["profile"]["concern"] is None


def test_consult_endpoint_ready_returns_recommend_request(client, llm):
    llm.responses[ConsultTurn] = _turn(profile=FULL, ready=True)
    r = client.post("/consult", json={"history": [{"role": "user", "content": "네 시작해 주세요"}],
                                      "file_analysis": {"technical_summary": "EC2 x2", "monthly_cost_usd": 200}})
    j = r.json()
    assert j["ready"] and j["missing"] == []
    assert j["recommend_request"]["current_monthly_usd"] == 200 and j["recommend_request"]["priorities"][0] == "cost"


def test_consult_files_endpoint(client, llm):
    llm.responses[FileAnalysis] = _analysis(monthly_cost_usd=245)
    r = client.post("/consult/files", files=[("files", ("main.tf", b'resource "x" "y" {}', "text/plain"))])
    assert r.status_code == 200 and r.json()["file_analysis"]["monthly_cost_usd"] == 245
    r = client.post("/consult/files", files=[("files", ("virus.exe", b"x", "application/octet-stream"))])
    assert r.status_code == 400 and r.json()["code"] == "INVALID_REQUEST"


def test_consult_rejects_bad_history(client):
    r = client.post("/consult", json={"history": [{"role": "bot", "content": "x"}]})
    assert r.status_code == 400
