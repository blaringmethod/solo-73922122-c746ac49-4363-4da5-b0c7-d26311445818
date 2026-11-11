"""手续费复算的独立参考实现 + 随机交叉验证（不进 pytest 主套件，单独运行）。

参考实现用最直白的方式重写计费口径（金额逐分推进阶梯游标 +
Decimal 半进位舍入），与生产求解器（整数运算 + (n+5000)//10000）
相互独立；随机生成合法输入（含跨月退款、跨档、全额退款、同时间戳），
逐字段比对两者输出。

运行方式：python3 tests/fee_fuzz_verify.py
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from app.fee_schemas import FeeRecalcRequest
from app.fee_solver import recalculate

_TIERS_POOL = [
    [{"threshold_cents": 0, "rate_bps": 60}],
    [
        {"threshold_cents": 0, "rate_bps": 100},
        {"threshold_cents": 12000, "rate_bps": 7},
    ],
    [
        {"threshold_cents": 0, "rate_bps": 60},
        {"threshold_cents": 100000, "rate_bps": 40},
        {"threshold_cents": 500000, "rate_bps": 30},
    ],
    [
        {"threshold_cents": 0, "rate_bps": 33},
        {"threshold_cents": 5000, "rate_bps": 19},
        {"threshold_cents": 20000, "rate_bps": 5},
        {"threshold_cents": 80000, "rate_bps": 1},
    ],
]


def reference_recalc(payments, refunds, tiers) -> dict:
    """与生产实现独立的参考实现：逐分推进 + Decimal 半进位。"""
    pay_by_id = {p.id: p for p in payments}
    refunded: dict[str, int] = {p.id: 0 for p in payments}
    refunds_of: dict[str, list[str]] = {p.id: [] for p in payments}
    for r in refunds:
        refunded[r.original_payment_id] += r.amount_cents
        refunds_of[r.original_payment_id].append(r.id)

    thresholds = [t.threshold_cents for t in tiers]
    rates = [t.rate_bps for t in tiers]

    def tier_at(cursor: int) -> int:
        idx = 0
        for i in range(len(thresholds) - 1):
            if cursor >= thresholds[i + 1]:
                idx = i + 1
            else:
                break
        return idx

    by_month: dict[str, list] = {}
    for p in payments:
        by_month.setdefault(f"{p.time_utc.year:04d}-{p.time_utc.month:02d}", []).append(p)

    months_out = []
    totals = {
        "payment_count": len(payments),
        "refund_count": len(refunds),
        "total_payment_cents": 0,
        "total_refund_cents": 0,
        "net_billing_cents": 0,
        "net_fee_cents": 0,
        "fee_receivable_cents": 0,
        "adjustment_cents": 0,
    }
    for month in sorted(by_month):
        ordered = sorted(by_month[month], key=lambda p: (p.time_utc, p.id))
        cursor = 0
        cumulative = Decimal("0")
        prev_rounded = 0
        month_pay_total = month_refund_total = month_net_fee = month_fee = 0
        pay_results = []
        for p in ordered:
            remaining = p.amount_cents - refunded[p.id]
            occupied: list[int] = []
            seg_scaled = 0
            left = remaining
            cur = cursor
            while left > 0:
                ti = tier_at(cur)
                upper = thresholds[ti + 1] if ti + 1 < len(thresholds) else None
                if upper is None:
                    take = left
                else:
                    take = min(left, upper - max(cur, thresholds[ti]))
                seg_scaled += take * rates[ti]
                if not occupied or occupied[-1] != ti:
                    occupied.append(ti)
                cur += take
                left -= take
            cursor += remaining
            cumulative += Decimal(seg_scaled) / Decimal(10000)
            rounded = int(cumulative.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            fee = rounded - prev_rounded
            prev_rounded = rounded
            pay_results.append(
                {
                    "payment_id": p.id,
                    "refund_ids": refunds_of[p.id],
                    "refund_amount_cents": refunded[p.id],
                    "net_billing_cents": remaining,
                    "fee_receivable_cents": fee,
                    "adjustment_cents": fee - p.net_fee_cents,
                    "occupied_tier_indices": occupied,
                }
            )
            month_pay_total += p.amount_cents
            month_refund_total += refunded[p.id]
            month_net_fee += p.net_fee_cents
            month_fee += fee
        months_out.append(
            {
                "month": month,
                "total_payment_cents": month_pay_total,
                "total_refund_cents": month_refund_total,
                "net_billing_cents": month_pay_total - month_refund_total,
                "net_fee_cents": month_net_fee,
                "fee_receivable_cents": month_fee,
                "adjustment_cents": month_fee - month_net_fee,
                "payments": pay_results,
            }
        )
        for key in (
            "total_payment_cents",
            "total_refund_cents",
            "net_billing_cents",
            "net_fee_cents",
            "fee_receivable_cents",
            "adjustment_cents",
        ):
            totals[key] += months_out[-1][key]
    return {"months": months_out, "totals": totals}


def random_payload(rng: random.Random) -> dict:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    n_pay = rng.randint(0, 12)
    payments = []
    for i in range(n_pay):
        # 时间戳允许重复，以覆盖 (时间, ID) 字典序 tie-break
        ts = base + timedelta(days=rng.randint(0, 89), hours=rng.randint(0, 23),
                              minutes=rng.choice([0, 0, 30]))
        payments.append(
            {
                "id": f"P{i:03d}",
                "time_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "amount_cents": rng.randint(1, 20000),
                "net_fee_cents": rng.randint(0, 500),
            }
        )
    refunds = []
    rid = 0
    for p in payments:
        # 每笔支付随机 0~2 次退款，累计不超过支付金额；部分跨月
        p_time = datetime.strptime(p["time_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        budget = p["amount_cents"]
        for _ in range(rng.randint(0, 2)):
            if budget <= 0:
                break
            amount = rng.randint(1, budget)
            budget -= amount
            r_time = p_time + timedelta(days=rng.randint(0, 40))
            refunds.append(
                {
                    "id": f"R{rid:03d}",
                    "time_utc": r_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "original_payment_id": p["id"],
                    "amount_cents": amount,
                }
            )
            rid += 1
    return {
        "tiers": rng.choice(_TIERS_POOL),
        "payments": payments,
        "refunds": refunds,
    }


def main() -> int:
    rng = random.Random(20261111)
    rounds = 500
    for round_idx in range(rounds):
        payload = random_payload(rng)
        req = FeeRecalcRequest.model_validate(payload)
        actual = recalculate(req.payments, req.refunds, req.tiers)
        expected = reference_recalc(req.payments, req.refunds, req.tiers)

        assert actual["totals"] == expected["totals"], (
            f"round {round_idx}: totals mismatch\n{payload}"
        )
        assert len(actual["months"]) == len(expected["months"])
        for am, em in zip(actual["months"], expected["months"]):
            for key in (
                "month",
                "total_payment_cents",
                "total_refund_cents",
                "net_billing_cents",
                "net_fee_cents",
                "fee_receivable_cents",
                "adjustment_cents",
            ):
                assert am[key] == em[key], f"round {round_idx}: month field {key} mismatch"
            assert len(am["payments"]) == len(em["payments"])
            for ap, ep in zip(am["payments"], em["payments"]):
                for key in (
                    "payment_id",
                    "refund_ids",
                    "refund_amount_cents",
                    "net_billing_cents",
                    "fee_receivable_cents",
                    "adjustment_cents",
                    "occupied_tier_indices",
                ):
                    assert ap[key] == ep[key], (
                        f"round {round_idx}: payment {ep['payment_id']} field {key} "
                        f"{ap[key]} != {ep[key]}\n{payload}"
                    )
                # 分段金额之和必须等于剩余计费金额，段费率与档位一致
                assert sum(s["amount_cents"] for s in ap["segments"]) == ap[
                    "net_billing_cents"
                ]
                for s in ap["segments"]:
                    assert s["fee_scaled_10k"] == (
                        s["amount_cents"] * s["rate_bps"]
                    )
                    assert s["rate_bps"] == payload["tiers"][s["tier_index"]]["rate_bps"]
                # 逐笔应收之和必须等于月度累计舍入费用
            assert sum(
                p["fee_receivable_cents"] for p in am["payments"]
            ) == am["fee_receivable_cents"]

    print(f"OK: {rounds} random rounds cross-validated against independent reference")
    return 0


if __name__ == "__main__":
    sys.exit(main())
