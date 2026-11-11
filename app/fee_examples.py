"""手续费复算接口的内置样例，直接展示在 /docs 的 Swagger UI 中。

两个样例与 examples/ 目录下的 JSON 文件保持一致：

* ``cross_month_refund``：跨月退款（11 月退款回冲 10 月原支付月份）；
* ``tier_crossing_rounding``：跨档尾差（月度累计四舍五入 vs 逐笔独立舍入）。
"""

from __future__ import annotations

# 三档阶梯：0~99,999 分 60bps；100,000~499,999 分 40bps；500,000 分起 30bps
_TIERS_THREE = [
    {"threshold_cents": 0, "rate_bps": 60},
    {"threshold_cents": 100000, "rate_bps": 40},
    {"threshold_cents": 500000, "rate_bps": 30},
]

# 两档阶梯：0~11,999 分 33bps；12,000 分起 7bps
_TIERS_TWO = [
    {"threshold_cents": 0, "rate_bps": 33},
    {"threshold_cents": 12000, "rate_bps": 7},
]

CROSS_MONTH_REFUND_REQUEST = {
    "merchant_id": "MCH-DEMO-001",
    "tiers": _TIERS_THREE,
    "payments": [
        {
            "id": "PAY-202610-001",
            "time_utc": "2026-10-03T02:00:00Z",
            "amount_cents": 150000,
            "net_fee_cents": 900,
        },
        {
            "id": "PAY-202610-002",
            "time_utc": "2026-10-20T06:30:00Z",
            "amount_cents": 80000,
            "net_fee_cents": 480,
        },
        {
            "id": "PAY-202611-001",
            "time_utc": "2026-11-02T01:15:00Z",
            "amount_cents": 60000,
            "net_fee_cents": 400,
        },
    ],
    "refunds": [
        {
            "id": "RFD-202611-001",
            "time_utc": "2026-11-05T09:30:00Z",
            "original_payment_id": "PAY-202610-001",
            "amount_cents": 50000,
        }
    ],
}

TIER_CROSSING_ROUNDING_REQUEST = {
    "merchant_id": "MCH-DEMO-002",
    "tiers": _TIERS_TWO,
    "payments": [
        {
            "id": "PAY-R-001",
            "time_utc": "2026-11-01T00:00:00Z",
            "amount_cents": 5000,
            "net_fee_cents": 17,
        },
        {
            "id": "PAY-R-002",
            "time_utc": "2026-11-02T00:00:00Z",
            "amount_cents": 5000,
            "net_fee_cents": 17,
        },
        {
            "id": "PAY-R-003",
            "time_utc": "2026-11-03T00:00:00Z",
            "amount_cents": 9000,
            "net_fee_cents": 29,
        },
        {
            "id": "PAY-R-004",
            "time_utc": "2026-11-04T00:00:00Z",
            "amount_cents": 7000,
            "net_fee_cents": 0,
        },
    ],
    "refunds": [
        {
            "id": "RFD-R-001",
            "time_utc": "2026-11-06T03:00:00Z",
            "original_payment_id": "PAY-R-004",
            "amount_cents": 7000,
        }
    ],
}

REJECTION_REQUEST = {
    "merchant_id": "MCH-DEMO-BAD",
    "tiers": _TIERS_TWO,
    "payments": [
        {
            "id": "PAY-BAD-001",
            "time_utc": "2026-11-10T08:00:00Z",
            "amount_cents": 10000,
            "net_fee_cents": 33,
        }
    ],
    "refunds": [
        {
            "id": "RFD-BAD-001",
            "time_utc": "2026-11-09T08:00:00Z",
            "original_payment_id": "PAY-BAD-001",
            "amount_cents": 1000,
        }
    ],
}

FEE_DOC_EXAMPLES = {
    "cross_month_refund": {
        "summary": "跨月退款：11 月退款回冲 10 月原支付月份",
        "description": (
            "退款 RFD-202611-001 发生在 11 月，但回冲原支付 PAY-202610-001 "
            "所在的 10 月累计金额；10 月剩余金额跨 0/100000 两档分段计费。"
        ),
        "value": CROSS_MONTH_REFUND_REQUEST,
    },
    "tier_crossing_rounding": {
        "summary": "跨档尾差：月度累计四舍五入，禁止逐笔独立舍入",
        "description": (
            "PAY-R-003 的剩余金额跨越 12000 分档位边界；累计未舍入费用"
            "41.7 分四舍五入为 42 分，与逐笔独立舍入（41 分）相差 1 分尾差。"
        ),
        "value": TIER_CROSSING_ROUNDING_REQUEST,
    },
    "rejected_refund_before_payment": {
        "summary": "整批拒绝：退款早于原支付（422）",
        "description": "退款时间早于原支付时间，返回 422 并定位问题退款。",
        "value": REJECTION_REQUEST,
    },
}
