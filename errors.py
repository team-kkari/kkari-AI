"""API 에러 형식 통일.

모든 에러 응답:  {"code": "...", "message": "...", "retryable": true|false}
  - retryable=true 인 것만 백엔드가 재시도한다 (같은 요청을 다시 보내면 성공할 수 있는 경우)
  - message 는 사용자에게 보여줘도 되는 고정 문구. 내부 예외 메시지는 서버 로그에만 남긴다

| code                  | HTTP | retryable | 언제 |
|-----------------------|------|-----------|------|
| INVALID_REQUEST       | 400  | false     | 잘못된 값 (category, count, history 형식 등) |
| NOT_FOUND             | 404  | false     | 없는 경로 |
| METHOD_NOT_ALLOWED    | 405  | false     | 경로는 있지만 지원하지 않는 HTTP 메서드 |
| VALIDATION_ERROR      | 422  | false     | 필수 필드 누락·타입 오류 (errors 에 필드별 상세) |
| REFUSED               | 422  | false     | 모델이 정책상 거절 — 같은 요청은 다시 보내도 거절됨 |
| RATE_LIMITED          | 429  | true      | Claude API 사용량 한도 — 잠시 후 재시도 |
| UPSTREAM_UNAVAILABLE  | 503  | true      | Claude API 연결 실패·타임아웃·과부하·5xx |
| NOT_CONFIGURED        | 500  | false     | AI 서버에 API 키 미설정 |
| LLM_AUTH              | 500  | false     | API 키가 유효하지 않거나 권한 없음 |
| GENERATION_FAILED     | 502  | false     | 출력 생성·파싱 실패, Claude 가 요청을 거부(4xx) |
| INTERNAL_ERROR        | 500  | false     | 그 외 서버 버그 |
"""
from __future__ import annotations

import logging

log = logging.getLogger("kkari")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.retryable, self.extra = status, code, message, retryable, extra

    def body(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, **self.extra}


class LLMRefusal(Exception):
    """모델이 정책상 답변을 거절."""


class LLMOutputError(Exception):
    """구조화 출력이 비었거나 잘림 (max_tokens 등)."""


def invalid(message: str) -> ApiError:
    return ApiError(400, "INVALID_REQUEST", message)


def classify(e: Exception) -> ApiError:
    """예외 → ApiError. anthropic SDK 예외는 재시도 가능 여부로 나눈다."""
    if isinstance(e, ApiError):
        return e
    if isinstance(e, LLMRefusal):
        return ApiError(422, "REFUSED", "모델이 요청을 정책상 거절했습니다. 요청 내용을 바꿔 다시 시도하세요.")
    if isinstance(e, LLMOutputError):
        return ApiError(502, "GENERATION_FAILED", "AI 응답 생성에 실패했습니다.")
    try:
        import anthropic
    except ImportError:  # Local LLM 백엔드 등
        anthropic = None
    if anthropic is not None:
        if isinstance(e, anthropic.RateLimitError):
            return ApiError(429, "RATE_LIMITED", "AI 사용량 한도를 초과했습니다. 잠시 후 다시 시도하세요.", True)
        if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return ApiError(500, "LLM_AUTH", "AI 서버의 API 키 설정에 문제가 있습니다. 관리자에게 문의하세요.")
        if isinstance(e, (anthropic.APIConnectionError, anthropic.InternalServerError)):  # 타임아웃 포함
            return ApiError(503, "UPSTREAM_UNAVAILABLE", "AI 서비스에 일시적으로 연결할 수 없습니다. 잠시 후 다시 시도하세요.", True)
        if isinstance(e, anthropic.APIStatusError):
            if e.status_code in (408, 409) or e.status_code >= 500:  # 529 overloaded 등
                return ApiError(503, "UPSTREAM_UNAVAILABLE", "AI 서비스가 일시적으로 혼잡합니다. 잠시 후 다시 시도하세요.", True)
            return ApiError(502, "GENERATION_FAILED", "AI 응답 생성에 실패했습니다.")
    return ApiError(500, "INTERNAL_ERROR", "서버 내부 오류가 발생했습니다.")
