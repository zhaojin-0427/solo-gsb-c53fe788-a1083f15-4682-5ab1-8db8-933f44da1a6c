from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import init_db
from .errors import register_handlers
from .routers import bills, periods, plans, tenants, usage


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Usage Billing Settlement API",
    version="1.0.0",
    description=(
        "SaaS usage-based billing & settlement. Append-only usage ledger with "
        "idempotent ingestion, versioned tiered pricing, transactional period "
        "close with a frozen receive cutoff, immutable reproducible bills, and "
        "retro adjustments for late corrections."
    ),
    lifespan=lifespan,
)

register_handlers(app)
app.include_router(tenants.router)
app.include_router(plans.router)
app.include_router(usage.router)
app.include_router(periods.router)
app.include_router(bills.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "usage-billing-settlement",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "flow": [
            "POST /plans -> POST /plans/{id}/versions",
            "POST /tenants -> PUT /tenants/{code}/plan",
            "POST /usage (idempotent) / corrections / reversals",
            "POST /periods -> POST /periods/{id}/preview -> POST /periods/{id}/close",
            "GET /bills/{id} -> GET /bills/{id}/trace -> POST /bills/{id}/verify",
        ],
    }
