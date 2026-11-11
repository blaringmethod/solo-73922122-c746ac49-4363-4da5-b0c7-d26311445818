"""手续费复算核心求解器（纯函数，便于单测）。

计费口径
========
1. **退款回冲原支付月份**：无论退款发生在哪个月，都从原支付所在自然月
   （按支付 UTC 时间归月）的累计金额中扣除；
2. 每个计费月内，每笔支付先扣除累计退款，剩余金额按「支付时间升序、
   支付 ID 字典序升序」依次占用月累计金额阶梯，跨档时按档分段计费；
3. 每月累计的**未舍入**手续费先四舍五入到分，相邻累计舍入值之差才是
   逐笔应收手续费——不允许逐笔独立舍入；
4. 全部金额与费率均为整数，未舍入费用以 ``金额 × 费率``（万分比）的
   整数形式累加，最后按 ``(n + 5000) // 10000`` 做半进位四舍五入，
   全程无浮点误差。

整批拒绝
========
* 退款引用的原支付不存在；
* 退款时间早于（或等于）原支付时间；
* 同一原支付的累计退款金额超过支付金额。
出现任一情况即抛出 :class:`FeeRecalcError`，拒绝整批。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from .fee_schemas import FeeRecalcError


def _round_half_up_scaled(fee_scaled_10k: int) -> int:
    """未舍入手续费（×10000 的整数）四舍五入到分，半进位（0.5 进位）。"""
    return (fee_scaled_10k + 5000) // 10000


def _month_key(dt) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def recalculate(payments: Sequence, refunds: Sequence, tiers: Sequence) -> dict:
    """执行手续费复算，返回结构化结果字典。

    参数为 Pydantic 模型列表（``PaymentRecord`` / ``RefundRecord`` /
    ``FeeTier``），也兼容带同名属性的普通对象。
    """
    # ---------- 1. 建立支付索引并校验退款引用 ----------
    payment_by_id: dict[str, object] = {p.id: p for p in payments}
    # 每笔支付关联的退款（保留请求顺序）与累计退款额
    refunds_of: dict[str, list[object]] = defaultdict(list)
    refunded_sum: dict[str, int] = defaultdict(int)

    for refund in refunds:
        original = payment_by_id.get(refund.original_payment_id)
        if original is None:
            raise FeeRecalcError(
                code="REFUND_PAYMENT_NOT_FOUND",
                message=(
                    f"退款 {refund.id} 引用的原支付 {refund.original_payment_id} 不存在，"
                    "整批拒绝"
                ),
                refund_id=refund.id,
                original_payment_id=refund.original_payment_id,
            )
        if refund.time_utc < original.time_utc:
            raise FeeRecalcError(
                code="REFUND_BEFORE_PAYMENT",
                message=(
                    f"退款 {refund.id} 的时间 {refund.time_utc.isoformat()} "
                    f"早于原支付 {original.id} 的时间 {original.time_utc.isoformat()}，"
                    "整批拒绝"
                ),
                refund_id=refund.id,
                original_payment_id=original.id,
            )
        refunded_sum[original.id] += refund.amount_cents
        if refunded_sum[original.id] > original.amount_cents:
            raise FeeRecalcError(
                code="REFUND_EXCEEDS_PAYMENT",
                message=(
                    f"原支付 {original.id} 累计退款 {refunded_sum[original.id]} 分，"
                    f"超过支付金额 {original.amount_cents} 分（触发退款 {refund.id}），"
                    "整批拒绝"
                ),
                refund_id=refund.id,
                original_payment_id=original.id,
            )
        refunds_of[original.id].append(refund)

    # ---------- 2. 按支付 UTC 月份分组，月内按 (时间, ID) 排序 ----------
    payments_by_month: dict[str, list[object]] = defaultdict(list)
    for payment in payments:
        payments_by_month[_month_key(payment.time_utc)].append(payment)

    rate_by_index = [t.rate_bps for t in tiers]
    thresholds = [t.threshold_cents for t in tiers]

    months_out: list[dict] = []
    grand = {
        "payment_count": len(payments),
        "refund_count": len(refunds),
        "total_payment_cents": 0,
        "total_refund_cents": 0,
        "net_billing_cents": 0,
        "net_fee_cents": 0,
        "fee_receivable_cents": 0,
        "adjustment_cents": 0,
    }

    for month in sorted(payments_by_month):
        ordered = sorted(
            payments_by_month[month], key=lambda p: (p.time_utc, p.id)
        )

        # 月累计游标：已被前面支付占用的计费金额（退款已先扣除）
        cumulative_used = 0
        cumulative_fee_scaled = 0  # 月累计未舍入费用（×10000）
        prev_rounded = 0  # 截至上一笔的累计舍入费用（分）

        month_payment_total = 0
        month_refund_total = 0
        month_net_fee = 0
        month_fee_receivable = 0

        payment_results: list[dict] = []
        for payment in ordered:
            related_refunds = refunds_of.get(payment.id, [])
            refund_amount = refunded_sum.get(payment.id, 0)
            remaining = payment.amount_cents - refund_amount

            # ---------- 3. 剩余金额依次占用阶梯，跨档分段 ----------
            segments: list[dict] = []
            occupied: list[int] = []
            cursor = cumulative_used
            left = remaining
            while left > 0:
                # 定位 cursor 所在档位
                tier_index = 0
                for idx in range(len(thresholds) - 1):
                    if cursor >= thresholds[idx + 1]:
                        tier_index = idx + 1
                    else:
                        break
                else:
                    tier_index = len(thresholds) - 1
                lower = thresholds[tier_index]
                upper = (
                    thresholds[tier_index + 1]
                    if tier_index + 1 < len(thresholds)
                    else None
                )
                # 本档还能容纳多少金额
                if upper is None:
                    take = left  # 最后一档覆盖无穷
                else:
                    take = min(left, upper - max(cursor, lower))
                rate = rate_by_index[tier_index]
                segments.append(
                    {
                        "tier_index": tier_index,
                        "rate_bps": rate,
                        "amount_cents": take,
                        "fee_scaled_10k": take * rate,
                    }
                )
                if not occupied or occupied[-1] != tier_index:
                    occupied.append(tier_index)
                cursor += take
                left -= take

            segment_fee_scaled = sum(s["fee_scaled_10k"] for s in segments)
            cumulative_used += remaining
            cumulative_fee_scaled += segment_fee_scaled

            # ---------- 4. 月度累计四舍五入，相邻舍入值之差为逐笔应收 ----------
            rounded_total = _round_half_up_scaled(cumulative_fee_scaled)
            fee_receivable = rounded_total - prev_rounded
            prev_rounded = rounded_total

            adjustment = fee_receivable - payment.net_fee_cents

            payment_results.append(
                {
                    "payment_id": payment.id,
                    "time_utc": payment.time_utc,
                    "amount_cents": payment.amount_cents,
                    "refund_ids": [r.id for r in related_refunds],
                    "refund_amount_cents": refund_amount,
                    "net_billing_cents": remaining,
                    "net_fee_cents": payment.net_fee_cents,
                    "fee_receivable_cents": fee_receivable,
                    "adjustment_cents": adjustment,
                    "occupied_tier_indices": occupied,
                    "segments": segments,
                }
            )

            month_payment_total += payment.amount_cents
            month_refund_total += refund_amount
            month_net_fee += payment.net_fee_cents
            month_fee_receivable += fee_receivable

        month_adjustment = month_fee_receivable - month_net_fee
        months_out.append(
            {
                "month": month,
                "total_payment_cents": month_payment_total,
                "total_refund_cents": month_refund_total,
                "net_billing_cents": month_payment_total - month_refund_total,
                "net_fee_cents": month_net_fee,
                "fee_receivable_cents": month_fee_receivable,
                "adjustment_cents": month_adjustment,
                "payments": payment_results,
            }
        )

        grand["total_payment_cents"] += month_payment_total
        grand["total_refund_cents"] += month_refund_total
        grand["net_billing_cents"] += month_payment_total - month_refund_total
        grand["net_fee_cents"] += month_net_fee
        grand["fee_receivable_cents"] += month_fee_receivable
        grand["adjustment_cents"] += month_adjustment

    return {"months": months_out, "totals": grand}
