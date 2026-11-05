"""출력 규칙: 마크다운 제거(한글 조사 붙은 경우 포함), 스트림 조각 경계, 통화 규칙."""
from textfmt import KRW_PER_USD, OUTPUT_RULES, krw_to_usd, plain, plain_stream, plain_tree


def test_plain_removes_markdown_even_next_to_korean():
    assert plain("**고객사명**과 *서비스*를 알려주세요") == "고객사명과 서비스를 알려주세요"
    assert plain("## 요약\n- `t4g.small` 2대\n__중요__") == "요약\n- t4g.small 2대\n중요"
    assert plain("**닫히지 않은 굵게") == "닫히지 않은 굵게"
    assert plain("2 * 3 = 6, 비용 $245") == "2 * 3 = 6, 비용 $245"   # 수식·금액은 그대로


def test_plain_stream_handles_split_markers():
    chunks = ["## 제목\n안녕 *", "*굵게*", "* 끝 #1번 ", "\n### 둘째", " 줄 **"]
    assert "".join(plain_stream(chunks)) == "제목\n안녕 굵게 끝 #1번 \n둘째 줄 "


def test_plain_tree_and_rules():
    assert plain_tree({"a": ["**x**", 3], "b": {"c": "*y*"}}) == {"a": ["x", 3], "b": {"c": "y"}}
    assert "달러($)" in OUTPUT_RULES and "마크다운을 쓰지 않는다" in OUTPUT_RULES
    assert krw_to_usd(5_000_000) == round(5_000_000 / KRW_PER_USD, 2)


def test_won_amounts_become_dollars_without_duplicates():
    assert plain("월 예산은 300만 원, 약 $2,140 으로") == "월 예산은 약 $2,140 으로"       # 모델 환산이 이어지면 원화만 삭제
    assert plain("월 200만 원(약 $1,430)의 EC2") == "월 약 $1,430의 EC2"
    assert plain("300만원이면 괜찮아요") == "약 $2,143이면 괜찮아요"                       # 환산이 없으면 직접 환산
    assert plain("3,000,000원 이하") == "약 $2,143 이하"
    assert plain("예산은 1.5억 원입니다") == "예산은 약 $107,143입니다"
    for keep in ("원화로 말씀해 주세요", "사원 3명", "5만원권"):                          # 금액이 아닌 '원' 은 그대로
        assert plain(keep) == keep


def test_rich_keeps_bold_and_spaces_attached_text():
    from textfmt import CHAT_OUTPUT_RULES, rich
    assert rich("우선순위는 **고가용성**, **비용 절감** 중 무엇인가요?") == "우선순위는 **고가용성**, **비용 절감** 중 무엇인가요?"
    assert rich("**고객사명**과 *서비스*를 알려주세요") == "**고객사명** 과 서비스를 알려주세요"   # 글자가 붙으면 공백
    assert rich("a**b**c") == "a **b** c"
    assert rich("## 요약\n- **t4g.small** 2대\n__중요__ 합니다") == "요약\n- **t4g.small** 2대\n**중요** 합니다"
    assert rich("**닫히지 않은 굵게") == "닫히지 않은 굵게"          # 짝 없는 ** 는 지움
    assert rich("월 **300만 원**이면") == "월 **약 $2,143** 이면"      # 원화는 달러로
    assert rich("2 * 3 = 6, 비용 $245") == "2 * 3 = 6, 비용 $245"
    assert "**굵게**" in CHAT_OUTPUT_RULES and "달러($)" in CHAT_OUTPUT_RULES and "글자(조사·어미)를 붙이지 않는다" in CHAT_OUTPUT_RULES
