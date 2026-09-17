# SaaS 用量计费结算 API

基于 **Python + FastAPI + PostgreSQL** 的用量计费（usage-based billing）结算服务。
金额全程使用 `Decimal`（数据库 `NUMERIC(28,8)`，账单按货币精度 `HALF_UP` 固化），
事件账册、价格版本、账单均不可变，任何历史账单都能用**固化的价格版本**与**固化的接收截止线**
重新计算出完全相同的结果。

## 一键启动

需要 Docker 与 Docker Compose v2：

```bash
docker compose up --build
```

启动后：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/docs | Swagger UI（可直接发请求） |
| http://localhost:8000/openapi.json | OpenAPI 描述 |
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/api/v1 | API 根前缀 |

停止 / 清空数据：

```bash
docker compose down          # 停止
docker compose down -v       # 同时删除数据库卷
```

数据库默认连接串（可在 `docker-compose.yml` 修改）：
`postgresql://billing:billing@db:5432/billing`。
应用启动时自动执行 `db/schema.sql`（含 `btree_gist` 扩展、排他约束、不可变触发器）。

## 端到端演示

服务启动后运行（仅用标准库，无需安装依赖）：

```bash
python3 scripts/demo.py
```

演示覆盖：幂等重放、同键异内容 409、追加式修正/撤销、阶梯计价、试算、
**关账与迟到事件并发时的截止线归属**、下期调整、逐事件计价轨迹、账单重算一致性。

截止线并发竞争压测（N 个事件线程与关账并发，断言每个事件恰好落在一张账单且
行序号不超过该账单 cutoff、两张账单重算均一致）：

```bash
python3 scripts/concurrency_check.py          # 默认 24 个事件
N_EVENTS=64 python3 scripts/concurrency_check.py
```

## 单元 / 集成测试

```bash
# 纯计价引擎 + 哈希（无第三方依赖）
python3 -m unittest discover -s tests -v

# 需要一个可用的 PostgreSQL（自动建表/清空）
DATABASE_URL=postgresql://billing:billing@localhost:5432/billing \
    python3 -m unittest tests.test_integration -v
```

## 领域模型与关键不变量

### 1. 用量事件（`usage_events`，只追加账册）

字段：`tenant_id, source, event_id, event_type, occurred_at, received_at, recv_seq,
quantity, dimensions, linked_event_id, root_event_id, content_hash`。

- **同一来源只能入账一次**：`UNIQUE(tenant_id, source, event_id)`。
  重复提交且**规范化内容完全一致** → 返回 `202` 且 `idempotent=true`，原账不动；
  内容不同（时间/数量/维度/关联任一不同）→ `409 Conflict`，响应体给出双方 `content_hash`。
- **修正 / 撤销只能追加，不得改写原账**：
  - `correction` / `cancellation` 必填 `linked_event_id`，追加为新行，新行有自己的
    `recv_seq`；数据库触发器禁止对 `usage_events` 的任何 `UPDATE`/`DELETE`。
  - `correction` 的 `quantity` 表示该业务事件**新的累计用量**，服务端在整条链
    （按 `recv_seq` 排序）上计算有符号差值入账；`cancellation` 自动入 `−累计`。
  - 撤销后的链不允许再追加修正；维度不可变（修正必须重复原维度）。
- **接收序号 `recv_seq`**：每租户单调递增，来自 `tenant_counters.last_recv_seq`。

### 2. 价格表（`price_versions` + `price_tiers`，版本化）

- 价格按 `[effective_from, effective_to)` 版本化；同一 `(tenant_id, source)` 的生效区间
  **用 GiST 排他约束保证不重叠**。`tenant_id = NULL` 为该 `source` 的默认价格，
  租户专属价格优先。
- 创建开口新版本（`effective_to=null`）时，旧的开口版本自动在新版本起点收尾；
  版本及其阶梯一经写入不可改（触发器只允许 `effective_to` 从 NULL 收尾一次）。
- 两种阶梯：
  - `volume`：总量落入唯一一档，`amount = quantity × unit + flat`；
  - `graduated`：跨档逐片切片，每片 `slice × unit + flat` 求和（进入该档收一次 flat）。
- 修正/撤销按有符号差值计价（撤销天然为负）；轨迹中同时保留 `raw_amount`（全精度）
  与 `amount`（按 `CURRENCY_SCALE=2`、`HALF_UP` 固化）。

### 3. 结算周期、截止线与不可变账单

关账 `POST /api/v1/periods/{id}/close` 在**单事务**内完成：

1. 对该租户的 `tenant_counters` 行加 `FOR UPDATE`（与入账事务抢同一把锁），
   读取并固化 `cutoff_recv_seq`。此刻尚未拿到序号的并发入账必须等待本事务提交，
   等待后它的序号必然 `> cutoff` —— **每条并发到达的记录都明确落在截止线一侧**。
2. 候选事件 = `recv_seq <= cutoff` 且从未进入任何账单的记录：
   - `usage` 且 `occurred_at` 在本期 → 正常用量行（`usage/current`）；
   - `usage` 且 `occurred_at` 早于本期 → **迟到记录**，进入本期调整行（`adjustment/late`）；
   - `correction/cancellation` 且 `occurred_at < 本期结束` → 本期调整（`adjustment/current`）；
     更早的为关闭后跨期修正（`adjustment/correction`）。
3. 逐条按事件 `occurred_at` 解析生效价格版本计价，写入 `bills` + `bill_lines`
   （每行记录 `price_version_id` 与完整 `pricing_trace`），周期置 `closed`。
   周期必须按时间顺序关闭；关闭后触发器拒绝一切改写，**历史账单不能重开**。
4. 若存在没有任何生效价格覆盖的事件，关账以 `422` 失败并列出事件——
   补一条覆盖区间的价格版本（可用默认价格）后重试即可。

> 迟到记录与关闭后的修正永远不会修改历史账单：它们作为**下期账单的调整行**入账，
> 且 `bill_lines.event_id` 唯一约束保证同一账册事件只被计价一次。

### 4. 试算、明细、轨迹、重算

- **试算** `GET /periods/{id}/trial`：对 open 周期只读投影（按当前最新接收序号，不固化），
  返回与正式关账一致的行明细与总额；对 closed 周期直接返回已固化账单。
- **账单明细** `GET /bills/{id}`：含每行数量、金额、归属类型与逐事件计价轨迹。
- **逐事件轨迹** `GET /bills/{id}/lines/{line_id}/trace`：档位切片、单价、flat、
  原始金额与固化金额。
- **重算核对** `GET /bills/{id}/reconcile`：用账单项上**固化的 `price_version_id`**
  与账单上**固化的 `cutoff_recv_seq`** 重新选择事件集合并逐事件计价，比对金额与行集合，
  返回 `matches`。事件账册只追加、价格/阶梯不可变，所以随时重算都一致。

## API 一览（前缀 `/api/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/tenants` | 创建租户 |
| GET | `/tenants` | 租户列表 |
| POST | `/tenants/{id}/events` | 上报事件（幂等；修正/撤销追加） |
| GET | `/tenants/{id}/events` | 查询事件（可按 `source` 过滤） |
| POST | `/price-versions` | 创建版本化价格（自动收尾旧开口版本，区间重叠报 409） |
| GET | `/price-versions` | 价格版本列表（可按 `source` / `tenant_id` 过滤） |
| GET | `/price-versions/{id}` | 单个价格版本（含阶梯） |
| POST | `/tenants/{id}/periods` | 创建结算周期（区间不可重叠） |
| GET | `/tenants/{id}/periods` | 周期列表 |
| POST | `/periods/{id}/close` | 事务内固化截止线并生成不可变账单 |
| GET | `/periods/{id}/trial` | 试算（open）或取回固化账单（closed） |
| GET | `/tenants/{id}/bills` | 租户账单列表 |
| GET | `/bills/{id}` | 账单 + 全部明细 |
| GET | `/bills/{id}/reconcile` | 按固化版本/截止线重算比对 |
| GET | `/bills/{id}/lines/{line_id}/trace` | 逐事件计价轨迹 |

## 请求示例

上报用量（`quantity` 建议用字符串，避免 JSON 浮点精度问题）：

```bash
curl -s -X POST localhost:8000/api/v1/tenants/$TID/events \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "api_calls",
    "event_id": "evt-1",
    "event_type": "usage",
    "occurred_at": "2026-01-15T12:00:00Z",
    "quantity": "1500",
    "dimensions": {"region": "us-east"}
  }'
```

追加修正（把 evt-1 的累计用量修正为 1600，服务端只把差值 100 计价入账）：

```bash
curl -s -X POST localhost:8000/api/v1/tenants/$TID/events \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "api_calls",
    "event_id": "evt-1-fix",
    "event_type": "correction",
    "linked_event_id": "'$EVT1_PK'",
    "occurred_at": "2026-02-10T09:00:00Z",
    "quantity": "1600",
    "dimensions": {"region": "us-east"}
  }'
```

创建 graduated 阶梯价格：

```bash
curl -s -X POST localhost:8000/api/v1/price-versions \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": null,
    "source": "api_calls",
    "pricing_mode": "graduated",
    "currency": "USD",
    "effective_from": "2025-01-01T00:00:00Z",
    "tiers": [
      {"tier_index": 0, "from_qty": "0",    "up_to_qty": "100",  "unit_amount": "0.10", "flat_amount": "0"},
      {"tier_index": 1, "from_qty": "100",  "up_to_qty": "1000", "unit_amount": "0.05", "flat_amount": "0"},
      {"tier_index": 2, "from_qty": "1000", "up_to_qty": null,   "unit_amount": "0.02", "flat_amount": "0"}
    ]
  }'
```

## 并发正确性说明

- 入账事务与关账事务都要先取 `tenant_counters` 的行锁（`SELECT ... FOR UPDATE`），
  因此每租户的“接收序号分配”与“截止线固化”串行执行；
  锁授予顺序就是截止线两侧的划分，不依赖提交时序的运气。
- 同一业务事件链的并发修正/撤销通过对链上所有行加 `FOR UPDATE` 串行化，
  差值计算不会丢失更新。
- 账单 / 账单项 / 事件 / 价格阶梯的不可变性同时由数据库触发器强制，
  即使绕过应用层也无法改写历史。

## 目录结构

```
app/
  main.py          FastAPI 入口（生命周期、错误处理）
  config.py        环境变量配置
  db.py            连接池 + schema 初始化
  deps.py          依赖注入与 ApiError
  models.py        Pydantic 请求/响应模型（Decimal 以字符串序列化）
  utils.py         规范化内容哈希、时间 UTC 化
  pricing.py       纯函数计价引擎（Decimal、volume/graduated、计价轨迹）
  prices.py        价格版本：校验、不重叠、生效解析
  events.py        事件入账：幂等/冲突/链路/接收序号
  periods.py       租户与结算周期
  billing.py       关账、试算、账单查询、按固化版本重算
  routers/api.py   HTTP 路由
db/schema.sql      扩展、表、排他约束、不可变触发器
scripts/demo.py    端到端演示（标准库）
tests/             计价单测 + PostgreSQL 集成测试
```
