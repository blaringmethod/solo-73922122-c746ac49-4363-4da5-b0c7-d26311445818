"""手续费复算 API 的请求 / 响应数据模型与输入校验。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

# 支付 + 退款合计笔数上限
MAX_FEE_RECORDS = 200
# 费率档位上限
MAX_TIERS = 5

# 正整数分金额：严格大于 0
PositiveCents = Annotated[int, Field(strict=True, gt=0)]
# 非负整数分
NonNegativeCents = Annotated[int, Field(strict=True, ge=0)]
# 编号：非空字符串，去除首尾空白后仍需非空
RecordId = Annotated[str, Field(min_length=1, max_length=64)]
# 万分比费率：非负整数
RateBps = Annotated[int, Field(strict=True, ge=0)]


def _serialize_utc(value: datetime) -> str:
    """统一序列化为带 Z 后缀的 UTC ISO-8601 字符串。"""
    text = value.astimezone(timezone.utc).isoformat()
    return text.replace("+00:00", "Z")


class FeeTier(BaseModel):
    """月累计金额阶梯的一档。

    ``threshold_cents`` 为本档月累计金额下限（含），首档必须为 0，各档下限
    严格递增，最后一档自动覆盖至无穷大；``rate_bps`` 为万分比费率。
    """

    model_config = ConfigDict(extra="forbid")

    threshold_cents: NonNegativeCents = Field(
        ..., description="本档月累计金额下限（含，分）；首档必须为 0，逐档严格递增"
    )
    rate_bps: RateBps = Field(..., description="本档费率，非负整数万分比（如 60 表示 0.60%）")


class PaymentRecord(BaseModel):
    """人民币支付明细。"""

    model_config = ConfigDict(extra="forbid")

    id: RecordId
    time_utc: datetime = Field(..., description="支付时间（UTC，ISO-8601，必须带时区，建议以 Z 结尾）")
    amount_cents: PositiveCents = Field(..., description="支付金额，正整数分")
    net_fee_cents: NonNegativeCents = Field(
        ..., description="当前净收手续费，非负整数分"
    )

    @field_validator("id")
    @classmethod
    def _strip_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("id 不能为空字符串")
        return value

    @field_validator("time_utc")
    @classmethod
    def _normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "time_utc 必须带时区偏移（UTC 时间，例如 2026-11-01T08:00:00Z）"
            )
        return value.astimezone(timezone.utc)

    @field_serializer("time_utc")
    def _serialize_time(self, value: datetime) -> str:
        return _serialize_utc(value)


class RefundRecord(BaseModel):
    """人民币退款明细，回冲原支付所在月份的累计金额。"""

    model_config = ConfigDict(extra="forbid")

    id: RecordId
    time_utc: datetime = Field(..., description="退款时间（UTC，ISO-8601，必须带时区，建议以 Z 结尾）")
    original_payment_id: RecordId = Field(..., description="原支付唯一 ID")
    amount_cents: PositiveCents = Field(..., description="退款金额，正整数分")

    @field_validator("id", "original_payment_id")
    @classmethod
    def _strip_id(cls, value: str, info) -> str:
        value = value.strip()
        if not value:
            raise ValueError(f"{info.field_name} 不能为空字符串")
        return value

    @field_validator("time_utc")
    @classmethod
    def _normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "time_utc 必须带时区偏移（UTC 时间，例如 2026-11-05T09:30:00Z）"
            )
        return value.astimezone(timezone.utc)

    @field_serializer("time_utc")
    def _serialize_time(self, value: datetime) -> str:
        return _serialize_utc(value)


class FeeRecalcRequest(BaseModel):
    """手续费复算请求：同一商户的人民币支付与退款明细。"""

    model_config = ConfigDict(extra="forbid")

    merchant_id: Optional[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default=None, description="商户编号（可选，原样回显）"
    )
    tiers: list[FeeTier] = Field(
        ...,
        description="月累计金额阶梯，1~5 档，从 0 连续覆盖至无穷，下限严格递增",
        min_length=1,
        max_length=MAX_TIERS,
    )
    payments: list[PaymentRecord] = Field(
        ..., description="支付明细，最多 200 笔（与退款合计）", max_length=MAX_FEE_RECORDS
    )
    refunds: list[RefundRecord] = Field(
        ..., description="退款明细，最多 200 笔（与支付合计）", max_length=MAX_FEE_RECORDS
    )

    @field_validator("merchant_id")
    @classmethod
    def _strip_merchant(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            value = value.strip() or None
        return value

    @model_validator(mode="after")
    def _check_cross_fields(self) -> "FeeRecalcRequest":
        # 合计笔数上限
        if len(self.payments) + len(self.refunds) > MAX_FEE_RECORDS:
            raise ValueError(
                f"支付与退款合计不得超过 {MAX_FEE_RECORDS} 笔，"
                f"当前 {len(self.payments) + len(self.refunds)} 笔"
            )
        # 阶梯：首档下限为 0，各档下限严格递增（最后一档自动覆盖无穷）
        if self.tiers[0].threshold_cents != 0:
            raise ValueError("费率阶梯首档 threshold_cents 必须为 0")
        for prev, nxt in zip(self.tiers, self.tiers[1:]):
            if nxt.threshold_cents <= prev.threshold_cents:
                raise ValueError(
                    "费率阶梯 threshold_cents 必须严格递增且从 0 连续覆盖至无穷"
                )
        # 支付 / 退款 ID 全局唯一
        seen: set[str] = set()
        dupes: list[str] = []
        for record in (*self.payments, *self.refunds):
            if record.id in seen and record.id not in dupes:
                dupes.append(record.id)
            seen.add(record.id)
        if dupes:
            raise ValueError(f"支付与退款中存在重复编号: {sorted(dupes)}")
        return self


class FeeSegment(BaseModel):
    """一笔支付剩余金额在单个费率档内的计费分段。"""

    model_config = ConfigDict(extra="forbid")

    tier_index: int = Field(..., description="占用的档位序号，从 0 开始（对应请求 tiers 下标）")
    rate_bps: int = Field(..., description="该档万分比费率")
    amount_cents: int = Field(..., description="落入该档的计费金额（分）")
    fee_scaled_10k: int = Field(
        ...,
        description="该段未舍入手续费 × 10000 的整数表示（等于 amount_cents × rate_bps）",
    )


class PaymentFeeResult(BaseModel):
    """单笔支付的复算结果与调账差额。"""

    model_config = ConfigDict(extra="forbid")

    payment_id: str
    time_utc: str = Field(..., description="支付 UTC 时间（Z 结尾 ISO-8601）")

    @field_validator("time_utc", mode="before")
    @classmethod
    def _coerce_utc_text(cls, value):
        if isinstance(value, datetime):
            return _serialize_utc(value)
        return value
    amount_cents: int = Field(..., description="支付金额（分）")
    refund_ids: list[str] = Field(..., description="关联退款 ID（按请求顺序）")
    refund_amount_cents: int = Field(..., description="该支付累计退款金额（分）")
    net_billing_cents: int = Field(..., description="扣退后参与计费的剩余金额（分）")
    net_fee_cents: int = Field(..., description="输入的当前净收手续费（分）")
    fee_receivable_cents: int = Field(..., description="复算应收手续费（分，月度累计四舍五入差额口径）")
    adjustment_cents: int = Field(
        ..., description="调账差额 = 应收 - 净收（分）；正数补收、负数退还"
    )
    occupied_tier_indices: list[int] = Field(
        ..., description="剩余金额占用的档位序号（升序去重）；全额退款时为空列表"
    )
    segments: list[FeeSegment] = Field(..., description="跨档分段计费明细")


class MonthFeeResult(BaseModel):
    """单个自然月（按支付时间 UTC 归月）的复算汇总。"""

    model_config = ConfigDict(extra="forbid")

    month: str = Field(..., description="月份，格式 YYYY-MM（按支付 UTC 时间）")
    total_payment_cents: int = Field(..., description="当月支付金额合计（分，退款前毛额）")
    total_refund_cents: int = Field(..., description="回冲当月的退款金额合计（分，含跨月退款）")
    net_billing_cents: int = Field(..., description="当月累计计费金额 = 支付合计 - 退款合计（分）")
    net_fee_cents: int = Field(..., description="当月各支付当前净收手续费合计（分）")
    fee_receivable_cents: int = Field(..., description="当月累计未舍入费用四舍五入后的应收手续费（分）")
    adjustment_cents: int = Field(..., description="当月调账差额 = 应收 - 净收（分）")
    payments: list[PaymentFeeResult] = Field(
        ..., description="逐笔支付结果，按支付时间升序、ID 字典序升序排列"
    )


class FeeTotals(BaseModel):
    """整批汇总。"""

    model_config = ConfigDict(extra="forbid")

    payment_count: int
    refund_count: int
    total_payment_cents: int
    total_refund_cents: int
    net_billing_cents: int
    net_fee_cents: int
    fee_receivable_cents: int
    adjustment_cents: int


class FeeRecalcResponse(BaseModel):
    """手续费复算响应。"""

    model_config = ConfigDict(extra="forbid")

    merchant_id: Optional[str]
    tiers: list[FeeTier] = Field(..., description="回显请求中的费率阶梯")
    months: list[MonthFeeResult] = Field(..., description="按月复算结果，月份升序")
    totals: FeeTotals


class FeeRecalcError(Exception):
    """业务规则拒绝（退款早于支付 / 引用缺失 / 累计超退），整批拒绝。"""

    def __init__(self, code: str, message: str, refund_id: str, original_payment_id: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.refund_id = refund_id
        self.original_payment_id = original_payment_id

    def to_response(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "refund_id": self.refund_id,
                "original_payment_id": self.original_payment_id,
            }
        }
