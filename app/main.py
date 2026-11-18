from pathlib import Path

from itsdangerous import BadSignature, URLSafeSerializer
from litestar import Litestar, Request, Response, get, post
from litestar.exceptions import HTTPException
from litestar.response import Redirect
from litestar.static_files import create_static_files_router
from litestar.status_codes import HTTP_302_FOUND

from app.config import Config
from app.db import init_db
from app.services import (
    close_batch,
    dispose_empty_object,
    get_batch,
    list_batches,
    list_dedup_runs,
    list_empty_objects,
    list_objects,
    list_reports,
    list_vouchers,
    list_withdrawals,
    recheck_object,
    register_object,
    run_dedup,
    run_visibility,
    waterline_meta,
)
from app.services.errors import ServiceError
from app.services.objects import (
    dispose_empty_object,
    list_dedup_runs,
    list_empty_objects,
    list_objects,
    list_vouchers,
    list_withdrawals,
    recheck_object,
    register_object,
    run_dedup,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-manifest-session")


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("manifest_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _ok(payload: dict) -> Response:
    return Response(
        content={"ok": True, **payload},
        status_code=200,
        media_type="application/json",
    )


def _fail(exc: ServiceError) -> Response:
    # 出错回白话：ServiceError.message 即面向页面的中文判定。
    return Response(
        content={"ok": False, "error": exc.message},
        status_code=exc.status_code,
        media_type="application/json",
    )


def _on_startup() -> None:
    init_db()


@get("/")
async def root(request: Request) -> Redirect:
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return Redirect("/app/", status_code=HTTP_302_FOUND)
    return Redirect("/login", status_code=HTTP_302_FOUND)


@get("/login")
async def login_page() -> Response:
    return Response(
        content=(STATIC / "login.html").read_bytes(),
        media_type="text/html; charset=utf-8",
    )


@get("/app/")
async def spa_page(request: Request) -> Response | Redirect:
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return Redirect("/login", status_code=HTTP_302_FOUND)
    return Response(
        content=(STATIC / "index.html").read_bytes(),
        media_type="text/html; charset=utf-8",
    )


@post("/api/login")
async def api_login(request: Request) -> Response:
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return Response(
            content={"ok": False, "error": "账号或密码错误"},
            status_code=400,
            media_type="application/json",
        )
    resp = Response(
        content={"ok": True, "user": user},
        status_code=200,
        media_type="application/json",
    )
    resp.set_cookie(
        "manifest_session",
        serializer.dumps({"user": user}),
        httponly=True,
        samesite="lax",
    )
    return resp


@post("/api/logout")
async def api_logout() -> Response:
    resp = Response(content={"ok": True}, status_code=200, media_type="application/json")
    resp.delete_cookie("manifest_session")
    return resp


@get("/api/meta")
async def api_meta(request: Request) -> dict:
    require_user(request)
    return {
        "lab_name": Config.LAB_NAME,
        "product_line": "cc-manifest-lab",
        "chain": ["落地批次", "对象登记", "清单闭合", "可见性对账"],
        "waterline": waterline_meta(),
    }


@get("/api/batches")
async def api_batches(request: Request, status: str | None = None) -> dict:
    require_user(request)
    return {"items": list_batches(status=status)}


@get("/api/batches/{batch_id:int}")
async def api_batch_detail(request: Request, batch_id: int) -> dict:
    require_user(request)
    batch = get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return {"batch": batch, "objects": list_objects(batch_id)}


@post("/api/batches/{batch_id:int}/objects")
async def api_register(request: Request, batch_id: int) -> Response:
    require_user(request)
    body = await request.json()
    try:
        obj = register_object(
            batch_id,
            str(body.get("object_key", "")),
            str(body.get("content_hash", "")),
            int(body.get("byte_size", 0) or 0),
        )
    except ServiceError as exc:
        return _fail(exc)
    return _ok({"object": obj})


@post("/api/batches/{batch_id:int}/close")
async def api_close(request: Request, batch_id: int) -> Response:
    require_user(request)
    try:
        run = close_batch(batch_id)
    except ServiceError as exc:
        return _fail(exc)
    return _ok({"run": run})


@get("/api/visibility-reports")
async def api_reports(request: Request) -> dict:
    require_user(request)
    return {"items": list_reports()}


@post("/api/visibility-reports")
async def api_run_report(request: Request) -> Response:
    require_user(request)
    body = await request.json()
    report = run_visibility(str(body.get("as_of_date", "2026-11-16")))
    return _ok({"report": report})


# --------------------------------------------------------------------------
# 空串治理：空串列表 / 处置（撤回轨、占位复检轨互斥）/ 强制复检 / 凭证流水
# --------------------------------------------------------------------------

@get("/api/empty-objects")
async def api_empty_objects(request: Request) -> dict:
    require_user(request)
    return {
        "items": list_empty_objects(),
        "vouchers": list_vouchers(),
        "withdrawals": list_withdrawals(),
        "waterline": waterline_meta(),
    }


@post("/api/empty-objects/{object_id:int}/dispose")
async def api_dispose_empty(request: Request, object_id: int) -> Response:
    require_user(request)
    body = await request.json()
    try:
        result = dispose_empty_object(object_id, str(body.get("track", "")))
    except ServiceError as exc:
        return _fail(exc)
    return _ok(result)


@post("/api/empty-objects/{object_id:int}/recheck")
async def api_recheck(request: Request, object_id: int) -> Response:
    require_user(request)
    body = await request.json()
    try:
        result = recheck_object(object_id, str(body.get("content_hash", "")))
    except ServiceError as exc:
        return _fail(exc)
    return _ok(result)


@get("/api/recheck-vouchers")
async def api_vouchers(request: Request) -> dict:
    require_user(request)
    return {"items": list_vouchers()}


@get("/api/withdrawal-flows")
async def api_withdrawals(request: Request) -> dict:
    require_user(request)
    return {"items": list_withdrawals()}


# --------------------------------------------------------------------------
# 去重编排台：复扫留痕，空串/占位一律拦截计数，不再吃空串
# --------------------------------------------------------------------------

@get("/api/dedup-runs")
async def api_dedup_runs(request: Request) -> dict:
    require_user(request)
    return {"items": list_dedup_runs()}


@post("/api/dedup-runs")
async def api_run_dedup(request: Request) -> Response:
    require_user(request)
    body = await request.json()
    try:
        run = run_dedup(str(body.get("scope", "all")))
    except ServiceError as exc:
        return _fail(exc)
    return _ok({"run": run})


app = Litestar(
    route_handlers=[
        root,
        login_page,
        spa_page,
        api_login,
        api_logout,
        api_meta,
        api_batches,
        api_batch_detail,
        api_register,
        api_close,
        api_reports,
        api_run_report,
        api_empty_objects,
        api_dispose_empty,
        api_recheck,
        api_vouchers,
        api_withdrawals,
        api_dedup_runs,
        api_run_dedup,
        create_static_files_router(path="/static", directories=[str(STATIC)]),
    ],
    on_startup=[_on_startup],
)
