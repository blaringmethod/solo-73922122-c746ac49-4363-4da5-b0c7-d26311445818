# 支付渠道日结对账与手续费复算 API

两个面向支付渠道对账员的服务能力，均基于 FastAPI 构建：

1. **日结对账**：核对外渠道**日结单**（扣费后应结金额）与**银行贷记流水**，
   防止合并打款被错误核销，提供精确的全局最优匹配求解、歧义检测与自动确认分类；
2. **手续费复算**：根据同一商户的人民币支付、退款导出明细，按商户月累计阶梯
   费率复算每笔支付的应收手续费，给出「应收 − 净收」的调账差额与占用档位。

## 一、日结对账接口 `POST /api/v1/reconcile`

### 业务规则

| 规则 | 说明 |
| --- | --- |
| 匹配基数 | 一笔银行入账匹配 **1～3 条**同币种日结记录 |
| 金额 | 日结金额合计与入账金额**严格相等**，单位为正整数「分」，无浮点误差 |
| 日期 | 入账日在每条结算日之后 **0～3 个自然日**内（含 0 和 3） |
| 币种 | 必须同币种（币种码大小写不敏感，统一转大写处理） |
| 唯一性 | 每条日结记录 / 每条入账在一套方案中至多使用一次 |
| 规模 | 日结记录与银行流水各不超过 **12** 条 |

**优化目标**：最大化全局已匹配入账总金额（加权集合装载问题，n≤12 用位掩码 DP 精确求解）。

### 结果分类口径

- `auto_confirm`：在**所有**最优方案中都对应同一日结组的入账 → 可自动确认核销；
- `manual_review`：在不同最优方案中对应不同日结组的入账 → 有歧义，转人工核对，
  响应中列出该入账的全部候选日结组；
- `never_matched`：在任何最优方案中始终未被匹配的日结记录 / 入账，单列处理；
- `solutions`：唯一时返回 1 套最优方案；存在歧义时返回 **2 套不同的完整最优方案**。

### 样例请求

```bash
# 唯一最优方案（自动确认 + 始终未匹配）
curl -X POST http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d @examples/request_unique.json

# 有歧义（返回两套不同最优方案，全部转人工）
curl -X POST http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d @examples/request_ambiguous.json

# 边界场景（超日期窗口 / 币种不符 / 金额无法凑齐）
curl -X POST http://localhost:8000/api/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d @examples/request_edge_cases.json
```

## 二、手续费复算接口 `POST /api/v1/fee/recalculate`

### 输入约束

| 项目 | 约束 |
| --- | --- |
| 商户 | 同一商户的人民币支付与退款，`merchant_id` 可选、原样回显 |
| 笔数 | 支付 + 退款合计最多 **200** 笔，支付/退款 ID 全局唯一 |
| 金额 | 支付、退款金额为**正整数分**；净收手续费为**非负整数分** |
| 时间 | UTC 的 ISO-8601 时间且必须带时区（建议 `Z` 结尾），内部统一转 UTC |
| 阶梯 | 最多 **5 档**月累计金额阶梯，首档下限为 0、各档下限严格递增，末档覆盖无穷 |
| 费率 | 每档 `rate_bps` 为非负整数**万分比**（如 60 表示 0.60%） |

### 计费口径

1. **退款回冲原支付月份**：无论退款发生在哪个自然月，都从原支付所在月份
   （按支付 UTC 时间归月）的月累计金额中扣除；
2. 每个计费月内，每笔支付先扣除其累计退款，剩余金额按**支付时间升序、
   支付 ID 字典序升序**依次占用月累计阶梯，跨档时按档分段计费；
3. 每月累计的**未舍入**手续费先四舍五入到分（半进位），相邻累计舍入值之差
   才是逐笔应收手续费——**不允许逐笔独立舍入**；
4. 全程整数运算（未舍入费用以 `金额 × 万分比费率` 的整数形式累加，
   `(n + 5000) // 10000` 完成四舍五入），无浮点误差。

### 响应与调账

- 逐笔返回 `fee_receivable_cents`（应收）、`adjustment_cents`
  （= 应收 − 当前净收，**正数补收、负数退还**）、`occupied_tier_indices`
  （占用档位序号）与 `segments`（跨档分段明细）；
- 按月与整批汇总支付额、退款额、净计费额、净收费、应收费与调账差额；
- 全额退款的支付剩余金额为 0，不占用任何档位，应收为 0。

### 整批拒绝（HTTP 422）

出现以下任一情况，定位问题退款并拒绝整批，响应体为
`{"error": {"code", "message", "refund_id", "original_payment_id"}}`：

| code | 触发条件 |
| --- | --- |
| `REFUND_PAYMENT_NOT_FOUND` | 退款引用的原支付 ID 不存在 |
| `REFUND_BEFORE_PAYMENT` | 退款时间早于原支付时间（等时放行） |
| `REFUND_EXCEEDS_PAYMENT` | 同一原支付的累计退款金额超过支付金额 |

编号重复、阶梯不连续、超笔数、金额非正、时间无时区等 schema 校验失败同样返回 422。

### 样例请求

Swagger UI（<http://localhost:8000/docs>）中该接口已内置三个可一键
「Try it out」的样例，仓库 `examples/` 下也有对应 JSON：

```bash
# 1) 跨月退款：11 月退款回冲 10 月原支付月份，10 月剩余金额跨 0/100000 两档
curl -X POST http://localhost:8000/api/v1/fee/recalculate \
  -H 'Content-Type: application/json' \
  -d @examples/request_fee_cross_month_refund.json

# 2) 跨档尾差：PAY-R-003 跨越 12000 分档边界；
#    月度累计未舍入 41.7 分四舍五入为 42 分，逐笔独立舍入只会得到 41 分
curl -X POST http://localhost:8000/api/v1/fee/recalculate \
  -H 'Content-Type: application/json' \
  -d @examples/request_fee_tier_crossing.json

# 3) 整批拒绝：退款早于原支付（422，返回问题退款定位信息）
curl -X POST http://localhost:8000/api/v1/fee/recalculate \
  -H 'Content-Type: application/json' \
  -d @examples/request_fee_rejected.json
```

跨档尾差样例的关键数字（两档：0 起 33bps、12000 分起 7bps）：

| 支付 | 剩余金额(分) | 累计未舍入(分) | 累计舍入(分) | 逐笔应收 | 若独立舍入 |
| --- | --- | --- | --- | --- | --- |
| PAY-R-001 | 5000 | 16.5 | 17 | **17** | 17 |
| PAY-R-002 | 5000 | 33.0 | 33 | **16** | 17（尾差 −1） |
| PAY-R-003 | 9000（跨档 2000+7000） | 44.5 | 45 | **12** | 12 |
| PAY-R-004 | 0（全额退款） | 44.5 | 45 | **0** | 0 |

### 请求体示例

```json
{
  "merchant_id": "MCH-DEMO-001",
  "tiers": [
    {"threshold_cents": 0, "rate_bps": 60},
    {"threshold_cents": 100000, "rate_bps": 40}
  ],
  "payments": [
    {
      "id": "PAY-202610-001",
      "time_utc": "2026-10-03T02:00:00Z",
      "amount_cents": 150000,
      "net_fee_cents": 900
    }
  ],
  "refunds": [
    {
      "id": "RFD-202611-001",
      "time_utc": "2026-11-05T09:30:00Z",
      "original_payment_id": "PAY-202610-001",
      "amount_cents": 50000
    }
  ]
}
```

## 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 入口、路由与交互式文档
│   ├── schemas.py       # 对账接口 Pydantic 模型与输入校验
│   ├── solver.py        # 对账求解器（前后缀位掩码 DP + SOS + DFS）
│   ├── fee_schemas.py   # 手续费复算模型、校验与业务异常
│   ├── fee_solver.py    # 手续费复算求解器（整数运算、月度累计舍入）
│   └── fee_examples.py  # /docs 内置的跨月退款 / 跨档尾差 / 拒绝样例
├── tests/
│   ├── test_solver.py       # 对账接口单元/集成测试
│   ├── test_fee_solver.py   # 手续费复算单元/集成测试
│   ├── fuzz_verify.py       # 对账：暴力穷举参考实现随机交叉验证（400 组）
│   └── fee_fuzz_verify.py   # 复算：独立参考实现随机交叉验证（500 组）
├── examples/            # 样例请求与对应响应
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 方式一：Docker Compose 启动（推荐）

```bash
docker compose up --build
```

启动后：

- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
- ReDoc 文档：<http://localhost:8000/redoc>
- OpenAPI Schema：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>

容器以非 root 用户运行，内置 `HEALTHCHECK`。停止服务：`docker compose down`。

## 方式二：本地 Python 启动

需要 Python 3.11+：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 算法说明

### 日结对账

1. 枚举每条入账的全部合法候选日结组（1～3 条、同币种、金额相等、日期合规）；
2. 按入账逐条推进的前/后缀位掩码 DP（2^n，n≤12），从机制上保证每条入账至多使用一次，
   求得全局最优匹配金额 `optimum`；
3. 利用 SOS 子集最大值表，判定每个候选组能否嵌入某个最优方案：
   - 某入账仅有 1 个最优兼容组 → 所有最优方案中映射恒定 → 自动确认；
   - 多于 1 个 → 存在歧义 → 人工核对；
4. 带上界剪枝的 DFS 枚举两套不同的完整最优方案；
5. 汇总任何最优方案中都未出现的记录为 `never_matched`。

满规模（12×12）极端高歧义输入的求解时间在 1 秒量级，常规输入为毫秒级。

### 手续费复算

1. 校验退款引用（存在性、时间先后、累计不超退），违规即整批拒绝；
2. 按支付 UTC 月份分组，月内按 `(支付时间, 支付 ID)` 排序；
3. 维护月累计金额游标，每笔支付先扣累计退款，剩余金额逐段消耗阶梯，
   记录每段档位、金额与未舍入费用；
4. 月累计未舍入费用统一四舍五入，相邻舍入值之差作为逐笔应收，
   再与净收相减得到逐笔调账差额；
5. 汇总月度与整批指标。200 笔满规模为毫秒级。

## 测试

```bash
pip install -r requirements.txt
pytest                                   # 全部单元/集成测试
python3 tests/fuzz_verify.py             # 对账：400 组随机输入 vs 暴力穷举
python3 tests/fee_fuzz_verify.py         # 复算：500 组随机输入 vs 独立参考实现
```

（独立运行脚本时如遇 `ModuleNotFoundError: app`，加 `PYTHONPATH=.` 前缀。）

- 对账交叉验证逐一比对最优值、方案合法性、唯一/歧义标记以及自动确认/人工核对分类；
- 复算交叉验证用独立的「逐分推进 + Decimal 半进位」参考实现，比对逐笔应收、
  调账差额、占用档位、月度与整批汇总，并额外校验分段金额守恒与档费率一致性。
