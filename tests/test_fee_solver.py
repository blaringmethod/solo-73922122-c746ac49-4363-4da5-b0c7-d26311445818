"""手续费复算求解器与 API 的单元 / 集成测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.fee_schemas import FeeRecalcError, FeeRecalcRequest
from app.fee_solver import recalculate
from app.main import app

client = TestClient(app)


def _tier(threshold: int, rate: int) -> dict:
    return {"threshold_cents": threshold, "rate_bps": rate}


def _pay(pid: str, ts: str, amount: int, net_fee: int) -> dict:
    return {
        "id": pid,
        "time_utc": ts,
        "amount_cents": amount,
        "net_fee_cents": net_fee,
    }


def _refund(rid: str, ts: str, pid: str, amount: int) -> dict:
    return {
        "id": rid,
        "time_utc": ts,
        "original_payment_id": pid,
        "amount_cents": amount,
    }


def _recalc(payload: dict) -> dict:
    req = FeeRecalcRequest.model_validate(payload)
    return recalculate(req.payments, req.refunds, req.tiers)


# ---------------------------------------------------------------- 基础计费


def test_single_payment_single_tier():
    payload = {
        "tiers": [_tier(0, 60)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 10000, 50)],
        "refunds": [],
    }
    res = _recalc(payload)
    p = res["months"][0]["payments"][0]
    # 10000 * 60bps = 60 分
    assert p["fee_receivable_cents"] == 60
    assert p["adjustment_cents"] == 10
    assert p["occupied_tier_indices"] == [0]
    assert p["segments"] == [
        {"tier_index": 0, "rate_bps": 60, "amount_cents": 10000, "fee_scaled_10k": 600000}
    ]
    assert res["totals"]["adjustment_cents"] == 10


def test_monthly_cumulative_rounding_not_per_payment():
    # 三笔 1000 分、费率 5bps：逐笔费用 0.5 分。
    # 独立舍入会得 3 分（1+1+1）；月度累计口径应为 round(1.5)=2 分（1+0+1）。
    payload = {
        "tiers": [_tier(0, 5)],
        "payments": [
            _pay("P1", "2026-11-01T00:00:00Z", 1000, 0),
            _pay("P2", "2026-11-02T00:00:00Z", 1000, 0),
            _pay("P3", "2026-11-03T00:00:00Z", 1000, 0),
        ],
        "refunds": [],
    }
    res = _recalc(payload)
    fees = [p["fee_receivable_cents"] for p in res["months"][0]["payments"]]
    assert fees == [1, 0, 1]
    assert res["months"][0]["fee_receivable_cents"] == 2


def test_half_up_rounding_boundary():
    # 1 分 * 5000bps = 0.5 分，半进位为 1 分
    payload = {
        "tiers": [_tier(0, 5000)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 1, 0)],
        "refunds": [],
    }
    res = _recalc(payload)
    assert res["months"][0]["payments"][0]["fee_receivable_cents"] == 1


def test_zero_rate_tier_produces_zero_fee():
    payload = {
        "tiers": [_tier(0, 0), _tier(10000, 10)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 5000, 9)],
        "refunds": [],
    }
    res = _recalc(payload)
    p = res["months"][0]["payments"][0]
    assert p["fee_receivable_cents"] == 0
    assert p["adjustment_cents"] == -9
    assert p["segments"][0]["fee_scaled_10k"] == 0


# ---------------------------------------------------------------- 跨档分段


def test_payment_spans_multiple_tiers():
    payload = {
        "tiers": [_tier(0, 100), _tier(10000, 50), _tier(50000, 10)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 60000, 0)],
        "refunds": [],
    }
    res = _recalc(payload)
    p = res["months"][0]["payments"][0]
    assert p["occupied_tier_indices"] == [0, 1, 2]
    assert [(s["tier_index"], s["amount_cents"]) for s in p["segments"]] == [
        (0, 10000),
        (1, 40000),
        (2, 10000),
    ]
    # 10000*100 + 40000*50 + 10000*10 = 3,100,000 -> 310 分
    assert p["fee_receivable_cents"] == 310


def test_tier_boundary_is_exclusive_at_threshold():
    # 累计恰好用到档位边界：第二笔从 threshold 处开始，全部落入第二档
    payload = {
        "tiers": [_tier(0, 100), _tier(10000, 10)],
        "payments": [
            _pay("P1", "2026-11-01T00:00:00Z", 10000, 0),
            _pay("P2", "2026-11-02T00:00:00Z", 5000, 0),
        ],
        "refunds": [],
    }
    res = _recalc(payload)
    p1, p2 = res["months"][0]["payments"]
    assert p1["occupied_tier_indices"] == [0]
    assert p2["occupied_tier_indices"] == [1]
    assert p2["segments"] == [
        {"tier_index": 1, "rate_bps": 10, "amount_cents": 5000, "fee_scaled_10k": 50000}
    ]


def test_tier_crossing_rounding_tail_difference():
    # 与 examples/request_fee_tier_crossing.json 同口径的精简版
    payload = {
        "tiers": [_tier(0, 33), _tier(12000, 7)],
        "payments": [
            _pay("P1", "2026-11-01T00:00:00Z", 5000, 17),
            _pay("P2", "2026-11-02T00:00:00Z", 5000, 17),
            _pay("P3", "2026-11-03T00:00:00Z", 9000, 29),
        ],
        "refunds": [],
    }
    res = _recalc(payload)
    p1, p2, p3 = res["months"][0]["payments"]
    # 累计未舍入：16.5 -> 33.0 -> 44.5（分），舍入：17 -> 33 -> 45
    assert [p["fee_receivable_cents"] for p in (p1, p2, p3)] == [17, 16, 12]
    # 逐笔独立舍入 P3 本应为 round(11.5)=12，累计口径同样 12；
    # 尾差体现在 P2：独立舍入 17，累计口径 16
    assert p2["adjustment_cents"] == -1
    assert p3["occupied_tier_indices"] == [0, 1]
    assert [(s["tier_index"], s["amount_cents"]) for s in p3["segments"]] == [
        (0, 2000),
        (1, 7000),
    ]
    assert res["months"][0]["fee_receivable_cents"] == 45


# ---------------------------------------------------------------- 退款规则


def test_refund_reduces_billing_base_same_month():
    payload = {
        "tiers": [_tier(0, 60)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 10000, 60)],
        "refunds": [_refund("R1", "2026-11-02T00:00:00Z", "P1", 4000)],
    }
    res = _recalc(payload)
    p = res["months"][0]["payments"][0]
    assert p["refund_ids"] == ["R1"]
    assert p["refund_amount_cents"] == 4000
    assert p["net_billing_cents"] == 6000
    assert p["fee_receivable_cents"] == 36
    assert p["adjustment_cents"] == -24
    assert res["months"][0]["total_refund_cents"] == 4000


def test_cross_month_refund_reverses_payment_month():
    # 退款发生在 11 月，仍回冲 10 月原支付；11 月只有另一笔支付
    payload = {
        "tiers": [_tier(0, 60), _tier(100000, 40)],
        "payments": [
            _pay("P-OCT", "2026-10-10T00:00:00Z", 150000, 900),
            _pay("P-NOV", "2026-11-08T00:00:00Z", 60000, 400),
        ],
        "refunds": [_refund("R1", "2026-11-20T00:00:00Z", "P-OCT", 50000)],
    }
    res = _recalc(payload)
    months = {m["month"]: m for m in res["months"]}
    oct_p = months["2026-10"]["payments"][0]
    # 剩余 100000 全部在第一档：600 分
    assert oct_p["net_billing_cents"] == 100000
    assert oct_p["fee_receivable_cents"] == 600
    assert oct_p["refund_ids"] == ["R1"]
    assert months["2026-10"]["total_refund_cents"] == 50000
    # 11 月的支付不受该退款影响
    nov_p = months["2026-11"]["payments"][0]
    assert nov_p["refund_ids"] == []
    assert nov_p["fee_receivable_cents"] == 360
    assert months["2026-11"]["total_refund_cents"] == 0


def test_full_refund_leaves_zero_billing_and_no_tier():
    payload = {
        "tiers": [_tier(0, 60)],
        "payments": [
            _pay("P1", "2026-11-01T00:00:00Z", 10000, 60),
            _pay("P2", "2026-11-02T00:00:00Z", 10000, 60),
        ],
        "refunds": [_refund("R1", "2026-11-03T00:00:00Z", "P1", 10000)],
    }
    res = _recalc(payload)
    p1, p2 = res["months"][0]["payments"]
    assert p1["net_billing_cents"] == 0
    assert p1["fee_receivable_cents"] == 0
    assert p1["occupied_tier_indices"] == []
    assert p1["segments"] == []
    # 全额退款不占用阶梯，P2 仍从 0 开始
    assert p2["occupied_tier_indices"] == [0]
    assert p2["fee_receivable_cents"] == 60


def test_multiple_partial_refunds_accumulate():
    payload = {
        "tiers": [_tier(0, 100)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 10000, 100)],
        "refunds": [
            _refund("R1", "2026-11-02T00:00:00Z", "P1", 3000),
            _refund("R2", "2026-11-03T00:00:00Z", "P1", 3000),
        ],
    }
    res = _recalc(payload)
    p = res["months"][0]["payments"][0]
    assert p["refund_ids"] == ["R1", "R2"]
    assert p["refund_amount_cents"] == 6000
    assert p["net_billing_cents"] == 4000
    assert p["fee_receivable_cents"] == 40


def test_refund_equal_to_payment_amount_is_allowed():
    payload = {
        "tiers": [_tier(0, 60)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 10000, 0)],
        "refunds": [_refund("R1", "2026-11-01T12:00:00Z", "P1", 10000)],
    }
    res = _recalc(payload)
    assert res["months"][0]["payments"][0]["net_billing_cents"] == 0


# ---------------------------------------------------------------- 排序


def test_payments_ordered_by_time_then_id():
    payload = {
        "tiers": [_tier(0, 10)],
        "payments": [
            _pay("P-B", "2026-11-02T00:00:00Z", 1000, 0),
            _pay("P-A", "2026-11-02T00:00:00Z", 1000, 0),
            _pay("P-C", "2026-11-01T00:00:00Z", 1000, 0),
        ],
        "refunds": [],
    }
    res = _recalc(payload)
    ids = [p["payment_id"] for p in res["months"][0]["payments"]]
    assert ids == ["P-C", "P-A", "P-B"]


# ---------------------------------------------------------------- 整批拒绝


def test_reject_missing_original_payment():
    payload = {
        "tiers": [_tier(0, 60)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 10000, 60)],
        "refunds": [_refund("R1", "2026-11-02T00:00:00Z", "GHOST", 100)],
    }
    req = FeeRecalcRequest.model_validate(payload)
    with pytest.raises(FeeRecalcError) as exc:
        recalculate(req.payments, req.refunds, req.tiers)
    assert exc.value.code == "REFUND_PAYMENT_NOT_FOUND"
    assert exc.value.refund_id == "R1"
    assert exc.value.original_payment_id == "GHOST"


def test_reject_refund_before_payment():
    payload = {
        "tiers": [_tier(0, 60)],
        "payments": [_pay("P1", "2026-11-10T08:00:00Z", 10000, 60)],
        "refunds": [_refund("R1", "2026-11-09T08:00:00Z", "P1", 100)],
    }
    req = FeeRecalcRequest.model_validate(payload)
    with pytest.raises(FeeRecalcError) as exc:
        recalculate(req.payments, req.refunds, req.tiers)
    assert exc.value.code == "REFUND_BEFORE_PAYMENT"


def test_reject_refund_equal_timestamp_treated_as_not_before():
    # 等时不算「早于」，应放行
    payload = {
        "tiers": [_tier(0, 60)],
        "payments": [_pay("P1", "2026-11-01T08:00:00Z", 10000, 0)],
        "refunds": [_refund("R1", "2026-11-01T08:00:00Z", "P1", 100)],
    }
    res = _recalc(payload)
    assert res["months"][0]["payments"][0]["refund_amount_cents"] == 100


def test_reject_cumulative_refund_exceeds_payment():
    payload = {
        "tiers": [_tier(0, 60)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 10000, 60)],
        "refunds": [
            _refund("R1", "2026-11-02T00:00:00Z", "P1", 6000),
            _refund("R2", "2026-11-03T00:00:00Z", "P1", 5000),
        ],
    }
    req = FeeRecalcRequest.model_validate(payload)
    with pytest.raises(FeeRecalcError) as exc:
        recalculate(req.payments, req.refunds, req.tiers)
    assert exc.value.code == "REFUND_EXCEEDS_PAYMENT"
    assert exc.value.refund_id == "R2"


# ---------------------------------------------------------------- 输入校验


def test_reject_naive_datetime():
    payload = {
        "tiers": [_tier(0, 60)],
        "payments": [_pay("P1", "2026-11-01T00:00:00", 10000, 60)],
        "refunds": [],
    }
    with pytest.raises(Exception):
        FeeRecalcRequest.model_validate(payload)


def test_accept_offset_timezone_and_normalize_to_utc():
    payload = {
        "tiers": [_tier(0, 60)],
        "payments": [_pay("P1", "2026-11-01T08:00:00+08:00", 10000, 60)],
        "refunds": [],
    }
    req = FeeRecalcRequest.model_validate(payload)
    assert req.payments[0].time_utc.utcoffset().total_seconds() == 0
    res = recalculate(req.payments, req.refunds, req.tiers)
    # 求解器层保留 datetime（UTC），序列化为字符串由响应模型负责
    assert res["months"][0]["payments"][0]["time_utc"].isoformat() == (
        "2026-11-01T00:00:00+00:00"
    )
    # UTC 归月：+08:00 的 11 月 1 日 08:00 对应 UTC 11 月 1 日 00:00
    assert res["months"][0]["month"] == "2026-11"


def test_tiers_must_start_at_zero():
    payload = {
        "tiers": [_tier(100, 60)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 10000, 60)],
        "refunds": [],
    }
    with pytest.raises(Exception):
        FeeRecalcRequest.model_validate(payload)


def test_tiers_must_be_strictly_increasing():
    payload = {
        "tiers": [_tier(0, 60), _tier(10000, 40), _tier(10000, 30)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 10000, 60)],
        "refunds": [],
    }
    with pytest.raises(Exception):
        FeeRecalcRequest.model_validate(payload)


def test_more_than_five_tiers_rejected():
    payload = {
        "tiers": [_tier(i * 1000, 10) for i in range(6)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 10000, 60)],
        "refunds": [],
    }
    with pytest.raises(Exception):
        FeeRecalcRequest.model_validate(payload)


def test_duplicate_ids_across_payments_and_refunds_rejected():
    payload = {
        "tiers": [_tier(0, 60)],
        "payments": [_pay("DUP", "2026-11-01T00:00:00Z", 10000, 60)],
        "refunds": [_refund("DUP", "2026-11-02T00:00:00Z", "P1", 100)],
    }
    with pytest.raises(Exception):
        FeeRecalcRequest.model_validate(payload)


def test_total_records_over_200_rejected():
    payments = [
        _pay(f"P{i}", "2026-11-01T00:00:00Z", 100, 0) for i in range(150)
    ]
    refunds = [
        _refund(f"R{i}", "2026-11-02T00:00:00Z", f"P{i}", 100) for i in range(51)
    ]
    payload = {"tiers": [_tier(0, 60)], "payments": payments, "refunds": refunds}
    with pytest.raises(Exception):
        FeeRecalcRequest.model_validate(payload)


def test_non_positive_amounts_rejected():
    base = {
        "tiers": [_tier(0, 60)],
        "payments": [],
        "refunds": [],
    }
    bad_payment = dict(_pay("P1", "2026-11-01T00:00:00Z", 0, 60))
    base["payments"] = [bad_payment]
    with pytest.raises(Exception):
        FeeRecalcRequest.model_validate(base)
    bad_refund = dict(_refund("R1", "2026-11-02T00:00:00Z", "P1", -5))
    base["payments"] = [_pay("P1", "2026-11-01T00:00:00Z", 10000, 60)]
    base["refunds"] = [bad_refund]
    with pytest.raises(Exception):
        FeeRecalcRequest.model_validate(base)


def test_negative_net_fee_rejected():
    payload = {
        "tiers": [_tier(0, 60)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 10000, -1)],
        "refunds": [],
    }
    with pytest.raises(Exception):
        FeeRecalcRequest.model_validate(payload)


def test_unknown_field_rejected():
    payload = {
        "tiers": [_tier(0, 60)],
        "payments": [_pay("P1", "2026-11-01T00:00:00Z", 10000, 60)],
        "refunds": [],
        "extra": 1,
    }
    with pytest.raises(Exception):
        FeeRecalcRequest.model_validate(payload)


# ---------------------------------------------------------------- 空输入与汇总


def test_empty_payments_and_refunds():
    payload = {"tiers": [_tier(0, 60)], "payments": [], "refunds": []}
    res = _recalc(payload)
    assert res["months"] == []
    assert res["totals"] == {
        "payment_count": 0,
        "refund_count": 0,
        "total_payment_cents": 0,
        "total_refund_cents": 0,
        "net_billing_cents": 0,
        "net_fee_cents": 0,
        "fee_receivable_cents": 0,
        "adjustment_cents": 0,
    }


def test_totals_and_month_aggregation_consistency():
    payload = {
        "merchant_id": "M1",
        "tiers": [_tier(0, 60), _tier(100000, 40)],
        "payments": [
            _pay("P1", "2026-10-01T00:00:00Z", 120000, 800),
            _pay("P2", "2026-11-01T00:00:00Z", 30000, 200),
        ],
        "refunds": [_refund("R1", "2026-12-01T00:00:00Z", "P1", 20000)],
    }
    res = _recalc(payload)
    totals = res["totals"]
    assert totals["payment_count"] == 2
    assert totals["refund_count"] == 1
    assert totals["total_payment_cents"] == 150000
    assert totals["total_refund_cents"] == 20000
    assert totals["net_billing_cents"] == 130000
    # 月度汇总与总计一致
    assert totals["fee_receivable_cents"] == sum(
        m["fee_receivable_cents"] for m in res["months"]
    )
    assert totals["adjustment_cents"] == sum(
        m["adjustment_cents"] for m in res["months"]
    )
    assert [m["month"] for m in res["months"]] == ["2026-10", "2026-11"]
    # 每笔调账差额之和等于月调账
    for month in res["months"]:
        assert month["adjustment_cents"] == sum(
            p["adjustment_cents"] for p in month["payments"]
        )


# ---------------------------------------------------------------- 满规模


def test_max_200_records_runs():
    payments = [
        _pay(f"P{i:03d}", f"2026-11-01T00:{i % 60:02d}:00Z", 1000, 0)
        for i in range(100)
    ]
    refunds = [
        _refund(f"R{i:03d}", "2026-11-02T00:00:00Z", f"P{i:03d}", 100)
        for i in range(100)
    ]
    payload = {"tiers": [_tier(0, 60), _tier(50000, 30)],
               "payments": payments, "refunds": refunds}
    res = _recalc(payload)
    month = res["months"][0]
    assert len(month["payments"]) == 100
    assert month["total_refund_cents"] == 10000
    # 每笔剩余 900 分，月累计 90000 分跨两档：
    # 50000*60bps + 40000*30bps = 3000000 + 1200000（×1e-4）= 420 分
    assert month["fee_receivable_cents"] == 420
    assert all(p["net_billing_cents"] == 900 for p in month["payments"])
    # 后续支付累计越过 50000 分边界，占用第二档
    assert month["payments"][-1]["occupied_tier_indices"] == [1]


# ---------------------------------------------------------------- HTTP 集成


def test_http_recalculate_cross_month_example():
    resp = client.post("/api/v1/fee/recalculate", json=CROSS_MONTH_PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["merchant_id"] == "MCH-DEMO-001"
    months = {m["month"]: m for m in body["months"]}
    oct_p = months["2026-10"]["payments"][0]
    assert oct_p["payment_id"] == "PAY-202610-001"
    assert oct_p["refund_ids"] == ["RFD-202611-001"]
    assert oct_p["occupied_tier_indices"] == [0]
    assert body["totals"]["adjustment_cents"] == -500


def test_http_rejection_shape_and_status():
    resp = client.post("/api/v1/fee/recalculate", json=REJECTION_PAYLOAD)
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "REFUND_BEFORE_PAYMENT"
    assert err["refund_id"] == "RFD-BAD-001"
    assert err["original_payment_id"] == "PAY-BAD-001"


def test_http_validation_error_for_bad_schema():
    resp = client.post(
        "/api/v1/fee/recalculate",
        json={
            "tiers": [{"threshold_cents": 0, "rate_bps": 60}],
            "payments": [
                {"id": "P1", "time_utc": "2026-11-01T00:00:00Z",
                 "amount_cents": -1, "net_fee_cents": 0}
            ],
            "refunds": [],
        },
    )
    assert resp.status_code == 422
    assert "detail" in resp.json()


def test_http_openapi_contains_three_examples():
    spec = client.get("/openapi.json").json()
    content = spec["paths"]["/api/v1/fee/recalculate"]["post"]["requestBody"][
        "content"
    ]["application/json"]
    assert set(content["examples"]) == {
        "cross_month_refund",
        "tier_crossing_rounding",
        "rejected_refund_before_payment",
    }


def test_http_response_time_uses_z_suffix():
    resp = client.post("/api/v1/fee/recalculate", json=CROSS_MONTH_PAYLOAD)
    p = resp.json()["months"][0]["payments"][0]
    assert p["time_utc"].endswith("Z")


# 模块级样例，避免重复字面量
CROSS_MONTH_PAYLOAD = {
    "merchant_id": "MCH-DEMO-001",
    "tiers": [
        {"threshold_cents": 0, "rate_bps": 60},
        {"threshold_cents": 100000, "rate_bps": 40},
        {"threshold_cents": 500000, "rate_bps": 30},
    ],
    "payments": [
        {"id": "PAY-202610-001", "time_utc": "2026-10-03T02:00:00Z",
         "amount_cents": 150000, "net_fee_cents": 900},
        {"id": "PAY-202610-002", "time_utc": "2026-10-20T06:30:00Z",
         "amount_cents": 80000, "net_fee_cents": 480},
        {"id": "PAY-202611-001", "time_utc": "2026-11-02T01:15:00Z",
         "amount_cents": 60000, "net_fee_cents": 400},
    ],
    "refunds": [
        {"id": "RFD-202611-001", "time_utc": "2026-11-05T09:30:00Z",
         "original_payment_id": "PAY-202610-001", "amount_cents": 50000}
    ],
}

REJECTION_PAYLOAD = {
    "merchant_id": "MCH-DEMO-BAD",
    "tiers": [{"threshold_cents": 0, "rate_bps": 33},
              {"threshold_cents": 12000, "rate_bps": 7}],
    "payments": [
        {"id": "PAY-BAD-001", "time_utc": "2026-11-10T08:00:00Z",
         "amount_cents": 10000, "net_fee_cents": 33}
    ],
    "refunds": [
        {"id": "RFD-BAD-001", "time_utc": "2026-11-09T08:00:00Z",
         "original_payment_id": "PAY-BAD-001", "amount_cents": 1000}
    ],
}
