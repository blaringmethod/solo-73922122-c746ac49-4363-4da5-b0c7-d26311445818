"""FastAPI 应用入口：支付渠道日结对账与手续费复算服务。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from .fee_examples import FEE_DOC_EXAMPLES
from .fee_schemas import FeeRecalcError, FeeRecalcRequest, FeeRecalcResponse
from .fee_solver import recalculate
from .schemas import (
    MAX_DATE_DELTA_DAYS,
    MAX_RECORDS,
    MAX_SETTLEMENTS_PER_ENTRY,
    ReconcileRequest,
    ReconcileResponse,
)
from .solver import solve

app = FastAPI(
    title="支付渠道对账与手续费复算 API",
    description=(
        "核对外渠道日结单（扣费后应结金额）与银行贷记流水，求解全局匹配入账总"
        "金额最大的方案，并按「所有最优方案中的一致性」区分自动确认、人工核对"
        "与始终未匹配记录；同时提供人民币支付/退款的手续费复算与调账差额计算。\n\n"
        "## 日结匹配规则\n"
        f"* 一笔银行入账可匹配 1~{MAX_SETTLEMENTS_PER_ENTRY} 条**同币种**日结记录；\n"
        "* 日结合计金额（正整数分）必须与入账金额**严格相等**；\n"
        f"* 入账日须在每条结算日之后 0~{MAX_DATE_DELTA_DAYS} 个自然日内（含端点）；\n"
        "* 每条记录在一套方案中只能使用一次；\n"
        f"* 日结记录与银行流水各不超过 {MAX_RECORDS} 条。\n\n"
        "## 手续费复算规则\n"
        "* 输入同一商户的人民币支付与退款，合计最多 200 笔；费率为 1~5 档、"
        "从 0 连续覆盖至无穷的月累计金额阶梯，费率单位为万分比；\n"
        "* 退款**回冲原支付所在月份**：每笔支付先扣除累计退款，剩余金额按支付"
        "时间升序、ID 字典序升序依次占用阶梯，跨档分段计费；\n"
        "* 每月累计**未舍入**费用先四舍五入到分，相邻累计舍入值之差才是逐笔应收，"
        "不允许逐笔独立舍入；\n"
        "* 退款早于支付、引用缺失或累计超退时，定位问题退款并**整批拒绝**（422）。"
    ),
    version="1.1.0",
)


@app.exception_handler(FeeRecalcError)
def fee_recalc_error_handler(_request, exc: FeeRecalcError) -> JSONResponse:
    """业务规则拒绝：统一 422 响应并携带定位信息。"""
    return JSONResponse(status_code=422, content=exc.to_response())


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/v1/reconcile",
    response_model=ReconcileResponse,
    tags=["对账"],
    summary="求解全局最优对账匹配",
    responses={
        422: {"description": "输入校验失败（编号重复、金额非正整数、超出条数等）"}
    },
)
def reconcile(payload: ReconcileRequest) -> ReconcileResponse:
    result = solve(payload.settlements, payload.bank_entries)
    return ReconcileResponse(**result)


@app.post(
    "/api/v1/fee/recalculate",
    response_model=FeeRecalcResponse,
    tags=["手续费复算"],
    summary="复算支付手续费并计算调账差额",
    responses={
        422: {
            "description": (
                "输入校验失败或业务规则拒绝：编号重复 / 阶梯不连续 / 超条数 / "
                "退款早于支付 / 引用缺失 / 累计超退"
            )
        }
    },
)
def recalculate_fee(
    payload: Annotated[FeeRecalcRequest, Body(openapi_examples=FEE_DOC_EXAMPLES)],
) -> FeeRecalcResponse:
    result = recalculate(payload.payments, payload.refunds, payload.tiers)
    return FeeRecalcResponse(
        merchant_id=payload.merchant_id,
        tiers=payload.tiers,
        months=result["months"],
        totals=result["totals"],
    )
