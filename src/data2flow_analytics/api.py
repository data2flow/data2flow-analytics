"""API(8080): 내부 API `/internal/analytics/**`(ANA-api §2). core-api만 부른다(ADR-021, X-CALLER-SERVICE).

응답은 다른 서비스와 같은 `{header, response}` / `{header, page, size, totalPages, responses, totalCount}`(api-rules.md §3).
오류는 UPPER_SNAKE 코드와 Accept-Language로 현지화한 resultMessage, 검증 실패는 errors[{field, code, message}].
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .common.errors import BusinessError, ErrorCode, pick_language
from .common.jsonutil import to_jsonable

log = logging.getLogger("data2flow_analytics.api")
P = "/internal/analytics"


def _header(ok: bool, code: str = "SUCCESS", message: str = "SUCCESS") -> dict:
    return {"isSuccessful": ok, "resultCode": code, "resultMessage": message}


def ok(response: Any = None, status: int = 200, headers: dict | None = None) -> JSONResponse:
    body: dict = {"header": _header(True)}
    if response is not None:
        body["response"] = to_jsonable(response)
    return JSONResponse(body, status_code=status, headers=headers)


def ok_list(page: dict) -> JSONResponse:
    return JSONResponse({"header": _header(True), "page": page["page"], "size": page["size"], "totalPages": page["totalPages"],
                         "responses": to_jsonable(page["responses"]), "totalCount": page["totalCount"]})


def error_response(request: Request, code: ErrorCode, args: dict | None = None, errors: list | None = None,
                   extra: dict | None = None) -> JSONResponse:
    lang = pick_language(request.headers.get("accept-language"))
    body: dict = {"header": _header(False, code.name, code.message(lang, **(args or {})))}
    if errors:
        body["errors"] = [e.to_json() if hasattr(e, "to_json") else e for e in errors]
    if extra:
        body.update(to_jsonable(extra))
    return JSONResponse(body, status_code=code.status)


class Identity:
    def __init__(self, request: Request):
        org = request.headers.get("x-org-id")
        if org is None or not org.isdigit():
            raise BusinessError(ErrorCode.AUTH_TOKEN_INVALID)
        self.org = int(org)
        user = request.headers.get("x-user-id")
        self.user = int(user) if user and user.isdigit() else None
        self.request_id = request.headers.get("x-request-id")


def _user(ident: Identity, body: dict, field: str) -> int | None:
    """사용자 ID: X-USER-ID, 없으면 본문 필드(ANA-api §2.1: feedback userId, datasets createdBy·updatedBy). 문자열 또는 {userId}."""
    if ident.user is not None:
        return ident.user
    value = body.get(field)
    if isinstance(value, dict):
        value = value.get("userId")
    return int(value) if value is not None and str(value).isdigit() else None


def create_app(services: Any = None) -> FastAPI:
    """services: 서비스 묶음(analytics·datasets·models·exports·kpis). None이면 기동 때 환경변수로 만든다."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.services is None:
            from .bootstrap import build_services

            app.state.services = build_services(role="api")
        yield
        closer = getattr(app.state.services, "close", None)
        if closer:
            closer()

    app = FastAPI(title="data2flow-analytics", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.services = services

    @app.exception_handler(BusinessError)
    async def _business(request: Request, exc: BusinessError):
        return error_response(request, exc.code, exc.args_, exc.errors, exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [{"field": ".".join(str(p) for p in e.get("loc", ())[1:]) or "body", "code": str(e.get("type")), "message": str(e.get("msg"))}
                  for e in exc.errors()]
        return error_response(request, ErrorCode.INVALID_REQUEST, errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return error_response(request, ErrorCode.RESOURCE_NOT_FOUND)
        if exc.status_code == 405:
            return error_response(request, ErrorCode.INVALID_REQUEST)
        return error_response(request, ErrorCode.INTERNAL_ERROR)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        log.exception("unexpected error path=%s", request.url.path)
        return error_response(request, ErrorCode.INTERNAL_ERROR)

    @app.middleware("http")
    async def _request_id(request: Request, call_next):
        response = await call_next(request)
        rid = request.headers.get("x-request-id")
        if rid:
            response.headers["X-REQUEST-ID"] = rid
        return response

    def svc(request: Request):
        return request.app.state.services

    # ------------------------------------------------------------ 템플릿(API-ANA-30·31, 17·18)
    @app.get(f"{P}/templates")
    def list_templates(request: Request, category: str | None = None, kind: str | None = None, includeDisabled: bool = False,
                       keyword: str | None = None, view: str | None = None, semantics: str | None = None,
                       page: int | None = None, size: int | None = None):
        ident = Identity(request)
        sem = [s.strip() for s in semantics.split(",") if s.strip()] if semantics else []
        return ok_list(svc(request).analytics.list_templates(ident.org, category=category, kind=kind, include_disabled=includeDisabled,
                                                             keyword=keyword, view=view, semantics=sem, page=page, size=size))

    @app.get(P + "/templates/{template_key}")
    def get_template(request: Request, template_key: str, version: str | None = None):
        ident = Identity(request)
        return ok(svc(request).analytics.get_template(ident.org, template_key, version))

    @app.post(P + "/templates/{template_key}/check")
    def check(request: Request, template_key: str, body: dict = Body(default_factory=dict)):
        ident = Identity(request)
        return ok(svc(request).analytics.check_sufficiency(ident.org, template_key, body, ident.request_id))

    @app.get(f"{P}/template-settings")
    def template_settings(request: Request):
        ident = Identity(request)
        return ok_list(svc(request).analytics.template_settings(ident.org))

    @app.put(P + "/template-settings/{template_key}")
    def set_template(request: Request, template_key: str, body: dict = Body(default_factory=dict)):
        ident = Identity(request)
        if not isinstance(body.get("enabled"), bool):
            raise BusinessError(ErrorCode.INVALID_REQUEST)
        return ok(svc(request).analytics.set_template_enabled(ident.org, ident.user, template_key, body["enabled"]))

    # ------------------------------------------------------------ 분석 정의(API-ANA-32, 22)
    @app.post(f"{P}/analyses")
    def create_analysis(request: Request, body: dict = Body(default_factory=dict)):
        ident = Identity(request)
        created = svc(request).analytics.create_analysis(ident.org, ident.user, body)
        return ok(created, 201, {"Location": f"{P}/analyses/{created['analysisId']}"})

    @app.get(f"{P}/analyses")
    def list_analyses(request: Request, templateKey: str | None = None, owner: str | None = None, scheduleState: str | None = None,
                      realtime: bool | None = None, keyword: str | None = None, spaceIds: str | None = None,
                      page: int | None = None, size: int | None = None):
        ident = Identity(request)
        spaces = [int(s) for s in spaceIds.split(",") if s.strip()] if spaceIds is not None else None
        return ok_list(svc(request).analytics.list_analyses(ident.org, ident.user, template_key=templateKey, owner=owner,
                                                            schedule_state=scheduleState, realtime=realtime, keyword=keyword,
                                                            space_ids=spaces, page=page, size=size))

    @app.get(P + "/analyses/{analysis_id}")
    def get_analysis(request: Request, analysis_id: int):
        return ok(svc(request).analytics.get_analysis(Identity(request).org, analysis_id))

    @app.put(P + "/analyses/{analysis_id}")
    def update_analysis(request: Request, analysis_id: int, body: dict = Body(default_factory=dict)):
        ident = Identity(request)
        return ok(svc(request).analytics.update_analysis(ident.org, ident.user, analysis_id, body))

    @app.delete(P + "/analyses/{analysis_id}")
    def delete_analysis(request: Request, analysis_id: int):
        ident = Identity(request)
        svc(request).analytics.delete_analysis(ident.org, ident.user, analysis_id)
        return Response(status_code=204)

    @app.get(P + "/analyses/{analysis_id}/runs")
    def list_runs(request: Request, analysis_id: int, page: int | None = None, size: int | None = None):
        return ok_list(svc(request).analytics.list_runs(Identity(request).org, analysis_id, page, size))

    @app.get(P + "/analyses/{analysis_id}/runs/compare")
    def compare(request: Request, analysis_id: int, base: int = Query(...), target: int = Query(...)):
        return ok(svc(request).analytics.compare_runs(Identity(request).org, analysis_id, base, target))

    # ------------------------------------------------------------ 실행(API-ANA-33·34)
    @app.post(f"{P}/runs")
    def request_run(request: Request, body: dict = Body(default_factory=dict)):
        ident = Identity(request)
        status, payload = svc(request).analytics.request_run(ident.org, ident.user, body, ident.request_id)
        headers = None
        if status == 202:
            headers = {"Location": f"{P}/runs/{payload['runId']}"}
        return ok(payload, status, headers)

    @app.get(P + "/runs/{run_id}")
    def get_run(request: Request, run_id: int):
        return ok(svc(request).analytics.get_run(Identity(request).org, run_id))

    @app.post(P + "/runs/{run_id}/cancel")
    def cancel_run(request: Request, run_id: int):
        return ok(svc(request).analytics.cancel_run(Identity(request).org, run_id))

    @app.put(P + "/runs/{run_id}/ai-commentary")
    def ai_commentary(request: Request, run_id: int, body: dict = Body(default_factory=dict)):
        svc(request).analytics.set_ai_commentary(Identity(request).org, run_id, body)
        return Response(status_code=204)

    @app.post(P + "/runs/{run_id}/export")
    def export(request: Request, run_id: int, body: dict = Body(default_factory=dict)):
        ident = Identity(request)
        status, payload = svc(request).exports.export(ident.org, ident.user, run_id, body)
        return ok(payload, status)

    @app.post(P + "/analyses/{analysis_id}/runs/{run_id}/export")
    def export_nested(request: Request, analysis_id: int, run_id: int, body: dict = Body(default_factory=dict)):
        """ANA-api §2.1 경로(core가 부름). 실행이 그 분석의 것인지 확인한다"""
        ident = Identity(request)
        run = svc(request).analytics.get_run(ident.org, run_id)
        if run["run"]["analysisId"] != str(analysis_id):
            raise BusinessError(ErrorCode.ANALYSIS_RUN_NOT_FOUND)
        status, payload = svc(request).exports.export(ident.org, _user(ident, body, "requestedBy"), run_id, body)
        return ok(payload, status)

    # ------------------------------------------------------------ 실시간(API-ANA-35, 11)
    @app.post(P + "/realtime/{analysis_id}")
    def realtime(request: Request, analysis_id: int, body: dict = Body(default_factory=dict)):
        if not isinstance(body.get("enabled"), bool):
            raise BusinessError(ErrorCode.INVALID_REQUEST)
        return ok(svc(request).analytics.set_realtime(Identity(request).org, analysis_id, body["enabled"]))

    @app.get(P + "/analyses/{analysis_id}/realtime-events")
    def realtime_events(request: Request, analysis_id: int, page: int | None = None, size: int | None = None,
                        deviceIds: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None):
        devices = [int(s) for s in deviceIds.split(",") if s.strip()] if deviceIds is not None else None
        return ok_list(svc(request).analytics.realtime_events(Identity(request).org, analysis_id, from_, to, page, size, devices))

    # ------------------------------------------------------------ 모델(API-ANA-36, 23)
    @app.post(P + "/analyses/{analysis_id}/models/train")
    def train(request: Request, analysis_id: int, body: dict = Body(default_factory=dict)):
        payload, _ = svc(request).models.request_training(Identity(request).org, analysis_id, body)
        return ok(payload, 202)

    @app.post(P + "/models/{model_id}/activate")
    def activate(request: Request, model_id: int, body: dict = Body(default_factory=dict)):
        return ok(svc(request).models.activate(Identity(request).org, model_id, bool(body.get("force", False))))

    @app.get(f"{P}/models")
    def models(request: Request, page: int | None = None, size: int | None = None, analysisId: int | None = None):
        return ok_list(svc(request).models.list(Identity(request).org, page, size, analysisId))

    # ------------------------------------------------------------ 이상 피드백(API-ANA-15)
    @app.post(f"{P}/feedback")
    def feedback(request: Request, body: dict = Body(default_factory=dict)):
        ident = Identity(request)
        return ok(svc(request).analytics.create_feedback(ident.org, _user(ident, body, "userId"), body), 201)

    # ------------------------------------------------------------ 데이터셋(API-ANA-20)
    @app.get(f"{P}/datasets")
    def list_datasets(request: Request, page: int | None = None, size: int | None = None):
        return ok_list(svc(request).datasets.list(Identity(request).org, page, size))

    @app.post(f"{P}/datasets")
    def create_dataset(request: Request, body: dict = Body(default_factory=dict)):
        ident = Identity(request)
        created = svc(request).datasets.create(ident.org, _user(ident, body, "createdBy"), body)
        return ok(created, 201, {"Location": f"{P}/datasets/{created['datasetId']}"})

    @app.get(P + "/datasets/{dataset_id}")
    def get_dataset(request: Request, dataset_id: int):
        return ok(svc(request).datasets.get(Identity(request).org, dataset_id))

    @app.put(P + "/datasets/{dataset_id}")
    def update_dataset(request: Request, dataset_id: int, body: dict = Body(default_factory=dict)):
        ident = Identity(request)
        return ok(svc(request).datasets.update(ident.org, _user(ident, body, "updatedBy"), dataset_id, body))

    @app.delete(P + "/datasets/{dataset_id}")
    def delete_dataset(request: Request, dataset_id: int):
        svc(request).datasets.delete(Identity(request).org, dataset_id)
        return Response(status_code=204)

    @app.get(P + "/datasets/{dataset_id}/versions")
    def dataset_versions(request: Request, dataset_id: int, page: int | None = None, size: int | None = None):
        return ok_list(svc(request).datasets.versions(Identity(request).org, dataset_id, page, size))

    @app.post(P + "/datasets/{dataset_id}/analyses/{analysis_id}/upgrade-dataset")
    def upgrade_dataset(request: Request, dataset_id: int, analysis_id: int):
        return ok(svc(request).datasets.upgrade_analysis(Identity(request).org, dataset_id, analysis_id))

    # ------------------------------------------------------------ KPI(API-ANA-37)
    @app.post(f"{P}/kpis/compute")
    def kpis(request: Request, body: dict = Body(default_factory=dict)):
        return ok(svc(request).kpis.compute(Identity(request).org, body))

    return app


app = create_app()
