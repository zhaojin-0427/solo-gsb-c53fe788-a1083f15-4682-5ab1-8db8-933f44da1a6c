from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import close_pool, init_pool
from .deps import ApiError
from .routers.api import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    yield
    close_pool()


app = FastAPI(
    title="SaaS 用量计费结算 API",
    version="1.0.0",
    description=(
        "采集用量事件（幂等 / 只追加修正撤销）、版本化阶梯价格表，"
        "并按固化截止线生成不可变账单；支持试算、账单明细、"
        "逐事件计价轨迹与按固化版本重算。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    detail: dict | str = {"message": exc.detail}
    if exc.extra:
        detail.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.get("/health", tags=["meta"])
def health():
    from .db import get_pool

    with get_pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "database": "ok"}


@app.get("/", tags=["meta"])
def index():
    return {
        "service": "usage-billing",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/health",
    }


app.include_router(router, prefix="/api/v1")
