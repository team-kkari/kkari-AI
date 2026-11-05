"""RAG API 서버 — 백엔드가 호출 (브라우저 직접 호출용 아님).

    uv run uvicorn server:app --host 0.0.0.0 --port 8000
    문서: http://localhost:8000/docs

엔드포인트
    POST /search         검색만 (API 키 불필요) — 근거 자료 확인/디버깅용
    POST /intake         초기 상담 인터뷰 — 챗봇이 질문하며 요구사항 프로필을 채움, 완료 시 /architectures 요청 데이터
    POST /architectures  추천 아키텍처 N개 — AI 순위·점수·장단점·월 비용·제약 위반·설계도 좌표 (1~3분)
    POST /recommend      인프라 정보 → 개선안 목록(JSON) + 출처
    POST /chat           챗봇 — NDJSON 스트리밍 (sources → delta… → done)
    POST /terraform      추천 아키텍처 하나(diagram) → Terraform 초안 파일 (AI 호출 없음, 즉시)
    POST /consult/files  고객창구: (선택) 운영 중인 인프라 파일 업로드 → file_analysis
    POST /consult        고객창구: 비전공자용 상담 — 쉬운 질문 하나씩, 파일이 있으면 질문이 줄어듦

에러는 모두 {"code", "message", "retryable"} (errors.py 참고).

환경 변수
    ANTHROPIC_API_KEY   Claude API 키
    ALLOWED_ORIGINS     CORS 허용 출처(쉼표 구분). 비우면 CORS 미적용 = 브라우저에서 직접 호출 불가 (기본)
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from typing import Literal
import os
import threading

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from architect import Architect, constraints_text
from consult import MAX_BYTES, MAX_FILES, Consult, analyze_files
from consult import WARMUP as CONSULT_WARMUP
from errors import ApiError, classify, invalid
from generator import RAG, source_refs
from intake import FIELDS, WARMUP, Intake
from retriever import CATEGORIES
from schemas import Constraints, Diagram
from terraform_gen import combine as combine_terraform
from terraform_gen import generate as generate_terraform

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kkari")

app = FastAPI(title="AWS Best Practice RAG")
_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"])
rag = RAG()  # 시작 시 인덱스·임베딩 모델 로드 (수십 초)

# ───────────────────────── 에러 응답 통일 ─────────────────────────


@app.exception_handler(ApiError)
async def _api_error(_: Request, e: ApiError):
    return JSONResponse(e.body(), status_code=e.status)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, e: RequestValidationError):
    errs = [{"field": ".".join(str(x) for x in err["loc"] if x != "body"), "message": err["msg"]} for err in e.errors()]
    return JSONResponse({"code": "VALIDATION_ERROR", "message": "요청 형식이 올바르지 않습니다.",
                         "retryable": False, "errors": errs}, status_code=422)


HTTP_ERRORS = {  # 프레임워크가 내는 HTTP 오류 → 고정 문구 (Starlette 원문 "Not Found" 등은 노출하지 않음)
    404: ("NOT_FOUND", "요청한 경로가 없습니다."),
    405: ("METHOD_NOT_ALLOWED", "이 경로에서 지원하지 않는 HTTP 메서드입니다."),
}


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, e: StarletteHTTPException):
    code, message = HTTP_ERRORS.get(e.status_code) or (
        ("INVALID_REQUEST", "잘못된 요청입니다.") if e.status_code < 500 else ("INTERNAL_ERROR", "서버 내부 오류가 발생했습니다."))
    return JSONResponse({"code": code, "message": message, "retryable": False},
                        status_code=e.status_code, headers=getattr(e, "headers", None))


@app.exception_handler(Exception)
async def _unhandled(_: Request, e: Exception):
    log.exception("처리되지 않은 오류")
    err = classify(e)
    return JSONResponse(err.body(), status_code=err.status)


def _warmup():
    """구조화 출력은 스키마를 처음 쓸 때 수십 초~1분 넘게 걸린다(이후 캐시돼 수 초).
    고객이 처음 보는 상담 첫 턴이 느리지 않도록 서버 시작 시 백그라운드로 한 번 호출해 둔다."""
    try:
        Intake(rag.llm).turn([], "", session_id=WARMUP)
        log.info("[warmup] /intake 준비 완료")
    except Exception as e:  # noqa: BLE001 — 워밍업 실패는 서비스에 영향 없음
        log.warning("[warmup] /intake 건너뜀: %s", type(e).__name__)
    try:
        Consult(rag.llm).turn([], None, session_id=CONSULT_WARMUP)
        log.info("[warmup] /consult 준비 완료")
    except Exception as e:  # noqa: BLE001
        log.warning("[warmup] /consult 건너뜀: %s", type(e).__name__)
    try:  # /architectures 스키마도 미리 컴파일 (첫 요청이 수 분 걸리는 것 방지)
        Architect(rag, n=1).run("EC2 t4g.small 1대", "워밍업", [])
        log.info("[warmup] /architectures 준비 완료")
    except Exception as e:  # noqa: BLE001
        log.warning("[warmup] /architectures 건너뜀: %s", type(e).__name__)


if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
    threading.Thread(target=_warmup, daemon=True).start()

# ───────────────────────── 요청 모델 ─────────────────────────


class SearchReq(BaseModel):
    query: str
    k: int = 8
    categories: list[str] | None = Field(None, description=f"필터: {CATEGORIES}")


class RecommendReq(BaseModel):
    infra: str = Field(description="현재 인프라 요약 (리소스 목록, CloudWatch 사용률, 비용 등 — 텍스트나 JSON 문자열)")
    goal: str = Field("", description="사용자가 원하는 구조/목표")
    priorities: list[str] = Field(default_factory=list, description="우선 최적화 방향, 예: ['cost', 'performance']")
    constraints: Constraints | None = Field(None, description="고객 제약 (/intake recommend_request.constraints 그대로)")


class ArchitecturesReq(RecommendReq):
    count: int = Field(5, description="추천 개수 (1~5)")
    current_monthly_usd: float | None = Field(
        None, description="백엔드가 분석한 현재 월 비용(USD). 주면 현재 대비 증감의 기준이 되고 AI 추정을 건너뜀")


class TerraformReq(BaseModel):
    """/architectures 응답의 architectures[i] 를 그대로 보내도 된다 (name, diagram 만 사용)."""
    name: str = Field("kkari", description="아키텍처 이름 (리소스 이름 접두사로 변환)")
    diagram: Diagram = Field(description="/architectures 응답의 architectures[i].diagram")
    constraints: Constraints | None = Field(None, description="고객 제약 — 개인정보·변경 금지 리소스 반영")
    format: Literal["json", "zip", "tf"] = Field(
        "json", description="json: 파일별 내용을 JSON 으로, zip: 압축 파일, tf: 전체를 합친 <프로젝트>.tf 파일 하나")


class ConsultReq(BaseModel):
    history: list[dict] = Field(default_factory=list,
                                description='지금까지 대화 [{"role":"assistant"|"user","content":"..."}]. 첫 턴은 빈 목록')
    file_analysis: dict | None = Field(None, description="POST /consult/files 응답의 file_analysis 그대로 (파일을 안 올렸으면 null)")
    session_id: str | None = Field(None, description="상담 세션 ID (백엔드에서 생성)")


class ChatReq(BaseModel):
    question: str
    history: list[dict] = Field(default_factory=list, description='이전 턴 [{"role":"user"|"assistant","content":"..."}]')
    categories: list[str] | None = None


class IntakeReq(BaseModel):
    history: list[dict] = Field(default_factory=list,
                                description='지금까지 대화 [{"role":"assistant"|"user","content":"..."}]. 첫 턴은 빈 목록')
    infra_context: str = Field("", description="백엔드가 자동 수집한 인프라 요약 (있으면 이건 다시 묻지 않음)")
    session_id: str | None = Field(None, description="대화 로그 묶음용 ID (백엔드에서 생성)")
    required_fields: list[str] | None = Field(
        None, description=f"필수로 물어볼 profile 항목 (생략 시 infra_status 에 맞는 기본 목록). 가능: {FIELDS}")
    infra_status: Literal["EXISTING", "NONE"] | None = Field(
        None, description=("의뢰인의 인프라 상태. EXISTING=이미 AWS 인프라가 있고 계정 연결로 자동 수집됨(가치·예산만 질문), "
                           "NONE=아직 인프라가 없는 신규 구축(구성·비용 대신 만들 서비스를 질문). 생략하면 기존 방식"))


# ───────────────────────── 공통 검사 ─────────────────────────


def _check(cats):
    bad = [c for c in cats or [] if c not in CATEGORIES]
    if bad:
        raise invalid(f"알 수 없는 category: {bad}. 가능: {CATEGORIES}")


def _check_history(history: list[dict]):
    if any(m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), str) for m in history):
        raise invalid('history 각 항목은 {"role": "user"|"assistant", "content": "..."} 형식이어야 합니다')


def _need_llm():
    if os.environ.get("LLM_BACKEND", "claude") == "claude" and not (
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log.error("ANTHROPIC_API_KEY 미설정")
        raise ApiError(500, "NOT_CONFIGURED", "AI 서버에 API 키가 설정되지 않았습니다. 관리자에게 문의하세요.")


def _call_llm(fn, *args, **kwargs):
    """LLM 호출 오류 → 통일된 에러. 내부 메시지는 로그에만."""
    _need_llm()
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        err = classify(e)
        if not isinstance(e, ApiError):
            log.warning("LLM 호출 실패 → %s: %s: %s", err.code, type(e).__name__, e)
        raise err from e


# ───────────────────────── 엔드포인트 ─────────────────────────


@app.get("/health")
def health():
    return {"ok": True, "chunks": len(rag.retriever.chunks)}


@app.post("/search")
def search(req: SearchReq):
    _check(req.categories)
    hits = rag.retriever.search(req.query, k=req.k, categories=req.categories)
    return {"results": [{**r, "text": h["text"], "score": h["score"]}
                        for r, h in zip(source_refs(hits), hits)]}


@app.post("/recommend")
def recommend(req: RecommendReq):
    _check(req.priorities)
    ctext = constraints_text(req.constraints)
    infra = req.infra + (f"\n\n[고객 제약]\n{ctext}" if ctext else "")
    return _call_llm(rag.recommend, infra, req.goal, req.priorities)


@app.post("/architectures")
def architectures(req: ArchitecturesReq):
    _check(req.priorities)
    if not 1 <= req.count <= 5:
        raise invalid("count 는 1~5")
    if req.current_monthly_usd is not None and req.current_monthly_usd <= 0:
        raise invalid("current_monthly_usd 는 0 보다 커야 합니다")
    return _call_llm(Architect(rag, n=req.count).run, req.infra, req.goal, req.priorities,
                     constraints=req.constraints, current_monthly_usd=req.current_monthly_usd)


@app.post("/intake")
def intake(req: IntakeReq):
    _check_history(req.history)
    if req.required_fields is not None:
        bad = [f for f in req.required_fields if f not in FIELDS]
        if bad or not req.required_fields:
            raise invalid(f"required_fields 에 알 수 없는 항목: {bad}. 가능: {FIELDS}" if bad
                          else "required_fields 는 비울 수 없습니다 (생략하면 기본 목록)")
    return _call_llm(lambda: Intake(rag.llm).turn(req.history, req.infra_context, req.session_id,
                                                  required=req.required_fields, infra_status=req.infra_status))


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


@app.post("/chat")
def chat(req: ChatReq):
    """NDJSON 스트리밍 (한 줄 = JSON 한 개):
        {"type":"sources","sources":[...]}
        {"type":"delta","text":"..."}          (여러 번)
        {"type":"done","refused":false}        또는 스트림 도중 오류 시 {"type":"error","code",...}
    첫 토큰 전에 실패하면 일반 에러 응답(HTTP 4xx/5xx)으로 돌려준다."""
    _check(req.categories)
    _check_history(req.history)
    sources, stream = _call_llm(rag.chat_stream, req.question, req.history, req.categories)

    first: str | None = None
    finished = refused = False
    try:  # 첫 조각까지 미리 받아서, 시작 전 실패는 HTTP 상태코드로 알린다
        first = next(stream)
    except StopIteration:
        finished = True
    except Exception as e:  # noqa: BLE001
        err = classify(e)
        if err.code != "REFUSED":
            log.warning("chat 시작 실패 → %s: %s", err.code, e)
            raise err from e
        finished = refused = True

    def body():
        yield _line({"type": "sources", "sources": sources})
        if first:
            yield _line({"type": "delta", "text": first})
        if finished:
            yield _line({"type": "done", "refused": refused})
            return
        try:
            for t in stream:
                yield _line({"type": "delta", "text": t})
            yield _line({"type": "done", "refused": False})
        except Exception as e:  # noqa: BLE001
            err = classify(e)
            if err.code == "REFUSED":
                yield _line({"type": "done", "refused": True})
            else:
                log.warning("chat 스트림 중 오류 → %s: %s", err.code, e)
                yield _line({"type": "error", **err.body()})

    return StreamingResponse(body(), media_type="application/x-ndjson; charset=utf-8")


@app.post("/terraform")
def terraform(req: TerraformReq):
    """AI 를 다시 부르지 않고 diagram 을 코드로 변환한다 (1초 미만). 자동 적용(apply)은 하지 않는다."""
    out = generate_terraform(req.diagram, req.name, req.constraints)
    if req.format == "json":
        return out
    if req.format == "tf":
        return Response(combine_terraform(out), media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{out["project"]}.tf"'})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, text in out["files"].items():
            z.writestr(f"terraform/{path}", text)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{out["project"]}-terraform.zip"'})


@app.post("/consult/files")
async def consult_files(files: list[UploadFile] = File(..., description="Terraform·CloudFormation·YAML·비용 CSV·구성도 이미지·PDF 등")):
    """고객창구 파일 업로드 (선택). 비밀값(키·비밀번호)은 분석 전에 지운다. 응답의 file_analysis 를 /consult 에 넘긴다."""
    if len(files) > MAX_FILES * 2:
        raise invalid(f"파일은 한 번에 최대 {MAX_FILES}개까지 올려 주세요")
    data = []
    for f in files:
        raw = await f.read(MAX_BYTES + 1)  # 한도 + 1 바이트만 읽어 초과 여부 판단
        data.append((f.filename or "file", raw))

    def run():
        try:
            return {"file_analysis": analyze_files(rag.llm, data)}
        except ValueError as e:
            raise invalid(str(e)) from e
    return _call_llm(run)


@app.post("/consult")
def consult(req: ConsultReq):
    """고객창구 상담 한 턴. 응답 question 에 프론트가 그릴 질문 정보(id, input 종류, options) 가 들어 있다."""
    _check_history(req.history)
    return _call_llm(lambda: Consult(rag.llm).turn(req.history, req.file_analysis, req.session_id))
