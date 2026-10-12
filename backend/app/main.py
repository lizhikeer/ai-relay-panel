"""FastAPI 应用入口。

- 统一异常处理：业务失败→200+success:false；未登录→401；上游故障→502
- 启动时探测 New API 渠道创建接口可用性，并打印一次性告警
- 托管 ../frontend 静态文件（若存在）
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, model_meta
from .newapi import close_client, get_client
from .response import BusinessError, Unauthorized, UpstreamError, envelope
from .routers import auth, channels, logs, overview, system, tokens, users, v1_models

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bff")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.ENV_FILE:
        logger.info("已载入配置文件：%s", config.ENV_FILE)
    else:
        logger.warning("未找到 .env，使用进程环境变量 / 默认值")
    logger.info("面板 BFF 启动中，New API = %s", config.settings.newapi_base_url)

    # 一次性探测：New API rc.41 渠道创建接口可用性（非破坏性，不会创建渠道）
    client = get_client()
    try:
        supported, note = await client.probe_channel_create()
        client.channel_create_supported = supported
        client.channel_create_note = note
        if supported is True:
            logger.warning(
                "[渠道创建接口] %s。重要提醒：New API rc.41 的 POST /api/channel/ 请求体必须是 "
                "{\"mode\":\"single\",\"channel\":{...}} 包装体；直接发送扁平渠道对象会返回 "
                "\"channel cannot be empty\"（即契约 §4 所述『缺陷』的真正根因）。BFF 已按包装体调用，创建可用。",
                note,
            )
        elif supported is False:
            logger.warning(
                "[渠道创建接口] %s。后续创建请求将返回 200+success:false 并给出降级引导。", note
            )
        else:
            logger.warning("[渠道创建接口] %s。", note)
    except Exception as exc:  # noqa: BLE001 - 探测失败不得影响启动
        logger.warning("[渠道创建接口] 探测异常（不影响启动）：%s", exc)

    yield

    await close_client()
    await model_meta.aclose()
    await v1_models.aclose()


app = FastAPI(title="ai-relay-panel BFF", version=config.settings.version, lifespan=lifespan)

# 同源经 Caddy 访问；此处放开便于本机 http / file:// 调试
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------- 统一异常处理 --------------------------- #
@app.exception_handler(Unauthorized)
async def _handle_unauthorized(request: Request, exc: Unauthorized):
    return JSONResponse(status_code=401, content=envelope(False, exc.message, None))


@app.exception_handler(UpstreamError)
async def _handle_upstream(request: Request, exc: UpstreamError):
    return JSONResponse(status_code=502, content=envelope(False, exc.message, None))


@app.exception_handler(BusinessError)
async def _handle_business(request: Request, exc: BusinessError):
    return JSONResponse(status_code=200, content=envelope(False, exc.message, None))


@app.exception_handler(RequestValidationError)
async def _handle_validation(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content=envelope(False, "请求参数格式错误", None))


@app.exception_handler(StarletteHTTPException)
async def _handle_http(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content=envelope(False, str(exc.detail), None))


@app.exception_handler(Exception)
async def _handle_unexpected(request: Request, exc: Exception):
    logger.exception("未处理异常：%s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content=envelope(False, "BFF 内部错误", None))


# --------------------------- 业务路由 --------------------------- #
app.include_router(auth.router)
app.include_router(overview.router)
app.include_router(channels.router)
app.include_router(tokens.router)
app.include_router(users.router)
app.include_router(logs.router)
app.include_router(system.router)

# 出口模型清单：/v1/models（由 Caddy 单独分流过来，非 /panel-api 前缀）
app.include_router(v1_models.router)


# --------------------------- 静态前端 --------------------------- #
_frontend = Path(config.settings.frontend_dir)
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
    logger.info("已挂载前端静态目录：%s", _frontend)
else:
    logger.warning("前端目录不存在（%s），仅提供 API", _frontend)
