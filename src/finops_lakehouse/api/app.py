"""FastAPI application factory executing deterministic FinOpsLakehouse queries & calculators."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
)

from finops_lakehouse.sdk import FinOpsLakehouse


def _reject_float_and_bool(value: Any) -> Decimal | int | str:
    """Strictly forbid IEEE 754 float and bool inputs at HTTP JSON boundaries (ADR-001)."""
    if isinstance(value, (float, bool)):
        raise ValueError(
            "IEEE 754 float and bool are strictly forbidden for monetary/rate precision; "
            "pass string decimal or integer."
        )
    if not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"Unsupported monetary/rate value type: {type(value).__name__}.")
    return value


StrictDecimalInput = Annotated[str | int | Decimal, BeforeValidator(_reject_float_and_bool)]


class B2BRoiRequest(BaseModel):
    """Request schema for B2B deal economics calculation."""

    model_config = ConfigDict(extra="forbid")

    initial_investment: StrictDecimalInput
    monthly_cashflows: list[StrictDecimalInput] = Field(min_length=1)
    contract_revenue: StrictDecimalInput
    direct_delivery_cost: StrictDecimalInput
    annual_discount_rate: StrictDecimalInput = "0.150000"
    monthly_inaction_cost: StrictDecimalInput = "0.0000"
    delay_months: StrictInt = Field(default=3, ge=0)
    target_gross_margin: StrictDecimalInput = "0.650000"


class CareerParityRequest(BaseModel):
    """Request schema for CLT x PJ Simples x Contractor USD/EUR parity + Monte Carlo."""

    model_config = ConfigDict(extra="forbid")

    clt_gross_monthly: StrictDecimalInput
    pj_gross_monthly: StrictDecimalInput
    contractor_fx_monthly: StrictDecimalInput
    fx_currency: StrictStr = "USD"
    vr_va_monthly: StrictDecimalInput = "1200.0000"
    health_monthly: StrictDecimalInput = "800.0000"
    iss_rate: StrictDecimalInput = "0.020100"
    pro_labore_ratio: StrictDecimalInput = "0.280000"
    seed: StrictInt | None = Field(default=None, ge=0)
    n_scenarios: StrictInt = Field(default=10_000, ge=1, le=50_000)


class AiFinOpsRequest(BaseModel):
    """Request schema for AI/Cloud FinOps optimization simulation (no filesystem path input)."""

    model_config = ConfigDict(extra="forbid")

    cache_hit_ratio: StrictDecimalInput = "0.400000"
    cache_discount_rate: StrictDecimalInput = "0.750000"
    batch_eligible_ratio: StrictDecimalInput = "0.300000"
    batch_discount_rate: StrictDecimalInput = "0.500000"
    enable_deepseek_offpeak: StrictBool = True


def create_app(lakehouse: FinOpsLakehouse | None = None) -> FastAPI:
    """Build the FastAPI application delegating 100% of logic to FinOpsLakehouse SDK."""
    service = lakehouse or FinOpsLakehouse()
    app = FastAPI(
        title="FinOps, CAPEX/OPEX & Revenue Recovery Lakehouse API",
        version="0.1.0",
        description="Deterministic Decimal API backed by FinOpsLakehouse SDK and DuckDB Gold.",
    )
    app.state.lakehouse = service

    @app.exception_handler(ValueError)
    async def _value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(TypeError)
    async def _type_error_handler(_request: Request, exc: TypeError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(InvalidOperation)
    async def _decimal_op_handler(_request: Request, exc: InvalidOperation) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(FileNotFoundError)
    async def _not_found_handler(_request: Request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return service.get_status()

    @app.get("/api/v1/budget-variance")
    def budget_variance(
        competencia: str | None = Query(default=None),
        cod_centro_custo: str | None = Query(default=None),
        tipo_despesa: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=1000),
    ) -> dict[str, Any]:
        return service.budget_variance(
            competencia=competencia,
            cod_centro_custo=cod_centro_custo,
            tipo_despesa=tipo_despesa,
            limit=limit,
        )

    @app.get("/api/v1/aging-priority")
    def aging_priority(
        faixa_aging: str | None = Query(default=None),
        prioridade: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=1000),
    ) -> dict[str, Any]:
        return service.aging_priority(
            faixa_aging=faixa_aging,
            prioridade=prioridade,
            limit=limit,
        )

    @app.post("/api/v1/b2b-roi")
    def b2b_roi(payload: B2BRoiRequest) -> dict[str, Any]:
        return service.b2b_roi(
            initial_investment=payload.initial_investment,
            monthly_cashflows=payload.monthly_cashflows,
            contract_revenue=payload.contract_revenue,
            direct_delivery_cost=payload.direct_delivery_cost,
            annual_discount_rate=payload.annual_discount_rate,
            monthly_inaction_cost=payload.monthly_inaction_cost,
            delay_months=payload.delay_months,
            target_gross_margin=payload.target_gross_margin,
        )

    @app.post("/api/v1/career-parity")
    def career_parity(payload: CareerParityRequest) -> dict[str, Any]:
        return service.career_parity(
            clt_gross_monthly=payload.clt_gross_monthly,
            pj_gross_monthly=payload.pj_gross_monthly,
            contractor_fx_monthly=payload.contractor_fx_monthly,
            fx_currency=payload.fx_currency,
            vr_va_monthly=payload.vr_va_monthly,
            health_monthly=payload.health_monthly,
            iss_rate=payload.iss_rate,
            pro_labore_ratio=payload.pro_labore_ratio,
            seed=payload.seed,
            n_scenarios=payload.n_scenarios,
        )

    @app.get("/api/v1/ai-finops")
    def ai_finops_get(
        cache_hit_ratio: str = Query(default="0.400000"),
        cache_discount_rate: str = Query(default="0.750000"),
        batch_eligible_ratio: str = Query(default="0.300000"),
        batch_discount_rate: str = Query(default="0.500000"),
        enable_deepseek_offpeak: bool = Query(default=True),
    ) -> dict[str, Any]:
        return service.ai_finops(
            cache_hit_ratio=cache_hit_ratio,
            cache_discount_rate=cache_discount_rate,
            batch_eligible_ratio=batch_eligible_ratio,
            batch_discount_rate=batch_discount_rate,
            enable_deepseek_offpeak=enable_deepseek_offpeak,
        )

    @app.post("/api/v1/ai-finops")
    def ai_finops_post(payload: AiFinOpsRequest) -> dict[str, Any]:
        return service.ai_finops(
            cache_hit_ratio=payload.cache_hit_ratio,
            cache_discount_rate=payload.cache_discount_rate,
            batch_eligible_ratio=payload.batch_eligible_ratio,
            batch_discount_rate=payload.batch_discount_rate,
            enable_deepseek_offpeak=payload.enable_deepseek_offpeak,
        )

    return app


app = create_app()
