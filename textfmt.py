"""모든 AI 응답의 출력 규칙 (서식·통화) + 안전장치.

피드백
- 마크다운 서식이 깨진다: 프론트가 '**굵게**' 를 그대로 보여 주거나, '**고객사명**과' 처럼 한글 조사가 붙으면
  마크다운 렌더러가 굵게 처리하지 못한다 → 마크다운을 쓰지 않는 일반 텍스트로 통일
- 달러와 원화를 섞어 쓴다 → 금액은 항상 달러($). 원화로 들은 금액은 KRW_PER_USD 로 환산

프롬프트에 OUTPUT_RULES 를 붙이고, 그래도 섞여 나온 마크다운 강조 기호는 plain() 으로 지운다.
"""
from __future__ import annotations

import re
from typing import Iterator

KRW_PER_USD = 1400  # 원화 → 달러 환산 기준 (원/달러)

OUTPUT_RULES = f"""
[출력 형식 — 반드시 지킨다]
- 마크다운을 쓰지 않는다: **굵게**, __밑줄__, # 제목, `코드`, 표, 링크 문법 금지. 화면이 일반 텍스트로 보여 준다.
  강조가 필요하면 '작은따옴표' 로 감싸고, 목록은 줄바꿈 후 '- ' 로 시작한다.
- 금액은 항상 미국 달러($)로만 쓴다 (예: $3,570, 월 $245). 원화 금액은 한 글자도 쓰지 않는다.
  고객이 원화로 말해도 그 원화 금액을 되풀이하지 말고, 1달러 = {KRW_PER_USD:,}원으로 환산한 달러만 말한다.
  좋은 예: "월 예산은 약 $3,570 으로 정리할게요." / 나쁜 예: "500만 원(약 $3,570)", "500만 원이면 $3,570".
"""

# 챗봇(상담) 답변 전용 규칙: 핵심 단어만 굵게 허용. 프론트가 **굵게** 만 굵은 글씨로 보여 준다.
CHAT_OUTPUT_RULES = f"""
[출력 형식 — 반드시 지킨다]
- 마크다운 중 '**굵게**' 만 쓴다. 제목(#), 기울임(*), 밑줄(__), `코드`, 표, 링크 문법은 쓰지 않는다. 목록은 줄바꿈 후 '- ' 로 시작한다.
- 굵게는 질문의 핵심 단어나 중요한 수치·선택지에만 쓴다. 한 메시지에 1~4곳, 한 단어에서 짧은 구절까지만 굵게 한다. 문장 전체를 굵게 하지 않는다.
- 굵은 글씨 바로 뒤에 글자(조사·어미)를 붙이지 않는다. 닫는 ** 뒤에는 공백이나 문장부호(. , ? ! : ) 만 온다.
  굵은 구절이 쉼표·물음표·공백 앞에 오도록 문장을 바꾼다.
  좋은 예: "우선순위는 **고가용성**, **비용 절감** 중 무엇인가요?" / 나쁜 예: "**고객사명**을", "**RDS**가".
- 금액은 항상 미국 달러($)로만 쓴다 (예: $3,570, 월 $245). 원화 금액은 한 글자도 쓰지 않는다.
  고객이 원화로 말해도 그 원화 금액을 되풀이하지 말고, 1달러 = {KRW_PER_USD:,}원으로 환산한 달러만 말한다.
  좋은 예: "월 예산은 약 $3,570 으로 정리할게요." / 나쁜 예: "500만 원(약 $3,570)", "500만 원이면 $3,570".
"""

_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?!\*)")  # 뒤에 한글 조사가 붙어도 처리
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_CODE = re.compile(r"`([^`\n]+)`")


# 원화 금액: "500만 원", "3,000,000원", "1.5억 원", "300만원이면" (뒤에 조사가 붙어도). "원화"·"원권" 은 제외
_KRW = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(억|천만|백만|만|천)?\s*원(?!화|권)")
_UNIT = {None: 1, "천": 1e3, "만": 1e4, "백만": 1e6, "천만": 1e7, "억": 1e8}
_PAREN_USD = re.compile(r"\s*\(\s*(약\s*)?(\$[\d,.]+)\s*\)")            # 500만 원(약 $3,570)
_NEAR_USD = re.compile(r"(이면|이라면|은|는|이|을|으로|정도면|정도|,)?\s*")   # 500만 원, 약 $3,570 / 500만 원이면 '약 $3,570'


def usd_only(text: str) -> str:
    """본문에 남은 원화 금액을 달러로 바꾼다 (프롬프트 규칙의 안전장치).
    바로 뒤에 모델이 쓴 달러 환산이 있으면 원화만 지우고, 없으면 1달러 = KRW_PER_USD 원으로 환산한다."""
    out, pos = [], 0
    for m in _KRW.finditer(text):
        if m.start() < pos:
            continue
        rest = text[m.end():]
        out.append(text[pos:m.start()])
        paren = _PAREN_USD.match(rest)
        near = _NEAR_USD.match(rest)
        if paren:                                   # (약 $N) → 약 $N
            out.append(f"약 {paren.group(2)}")
            pos = m.end() + paren.end()
        elif re.match(r"['\"]?\s*(월\s*)?(약\s*)?\$", rest[near.end():]):   # 뒤에 달러가 이어짐 → 원화만 삭제
            pos = m.end() + near.end()
        else:
            won = float(m.group(1).replace(",", "")) * _UNIT[m.group(2)]
            out.append(f"약 ${won / KRW_PER_USD:,.0f}")
            pos = m.end()
    out.append(text[pos:])
    return re.sub(r"약\s+약\s+", "약 ", "".join(out))


def plain(text: str) -> str:
    """마크다운 강조·제목·인라인 코드 기호를 지우고, 원화 금액은 달러로 바꾼다. 목록('- ')과 줄바꿈은 유지."""
    if not text:
        return text
    text = usd_only(text)
    text = _BOLD.sub(r"\2", text)
    text = _ITALIC.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = _CODE.sub(r"\1", text)
    return text.replace("**", "").replace("__", "")


_BOLD_KEEP = re.compile(r"\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*")
_BOLD_UNDER = re.compile(r"(?<!\w)__(?=\S)([^_\n]+?)(?<=\S)__(?!\w)")
_WORD = re.compile(r"\w")  # 한글·영문·숫자


def rich(text: str) -> str:
    """챗봇 답변용 정리: **굵게** 는 남기고 나머지 마크다운(제목·기울임·코드)과 원화 금액은 plain() 처럼 처리한다.
    굵은 글씨 앞뒤에 글자가 바로 붙으면 공백을 넣고(렌더러가 굵게 못 알아보는 경우 방지), 짝이 없는 ** 는 지운다."""
    if not text:
        return text
    text = usd_only(text)
    text = _BOLD_UNDER.sub(r"**\1**", text)
    text = _ITALIC.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = _CODE.sub(r"\1", text)

    def bold(m: re.Match) -> str:
        before = m.string[m.start() - 1] if m.start() > 0 else ""
        after = m.string[m.end()] if m.end() < len(m.string) else ""
        lead = " " if before and _WORD.match(before) else ""
        tail = " " if after and _WORD.match(after) else ""
        return f"{lead}**{m.group(1)}**{tail}"

    text = _BOLD_KEEP.sub(bold, text)
    # 짝이 맞는 굵게만 남기고 나머지 ** 는 지운다
    parts = re.split(r"(\*\*[^*\n]+\*\*)", text)
    return "".join(p if i % 2 else p.replace("**", "") for i, p in enumerate(parts))


def plain_tree(obj):
    """dict·list 안의 모든 문자열에 plain() 적용 (구조화 출력 전체 정리용)."""
    if isinstance(obj, str):
        return plain(obj)
    if isinstance(obj, list):
        return [plain_tree(x) for x in obj]
    if isinstance(obj, dict):
        return {k: plain_tree(v) for k, v in obj.items()}
    return obj


def plain_stream(chunks: Iterator[str]) -> Iterator[str]:
    """스트리밍 조각에서 마크다운 강조(**, __)·제목(#) 기호를 지운다.
    조각 경계에 걸친 기호를 위해 끝의 '*', '_', '#' 는 다음 조각까지 보류한다."""
    carry, at_line_start = "", True
    for chunk in chunks:
        buf = carry + chunk
        keep = len(buf) - len(buf.rstrip("*_#"))
        buf, carry = (buf[:-keep], buf[-keep:]) if keep else (buf, "")
        out = _clean_piece(buf, at_line_start)
        if buf:
            at_line_start = buf.endswith("\n")
        if out:
            yield out
    if carry:
        out = _clean_piece(carry, at_line_start)
        if out:
            yield out


def _clean_piece(text: str, at_line_start: bool) -> str:
    text = usd_only(text.replace("**", "").replace("__", ""))
    if at_line_start:
        return _HEADING.sub("", text)
    head, sep, rest = text.partition("\n")  # 줄 중간에서 시작한 조각은 첫 줄의 '#' 를 건드리지 않는다
    return head + sep + _HEADING.sub("", rest) if sep else head


def krw_to_usd(amount: float) -> float:
    return round(amount / KRW_PER_USD, 2)
