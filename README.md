# Usage Billing Settlement API

面向 SaaS 的用量计费结算服务。Python 3.12 · FastAPI · PostgreSQL 16。

核心能力：

- **幂等采集**：同一 `(tenant, source, event_id)` 只入账一次；相同内容重放返回原记录（200），同键异内容返回 409 冲突。
- **追加式账本**：修正（correction）与撤销（reversal）只追加关联记录，原始账永不被改写（数据库触发器强制）。
- **版本化价格表**：按生效区间 `[effective_from, effective_to)` 版本化，区间不得重叠（GiST 排他约束 + 应用层校验），支持阶梯价，金额全程 `Decimal`。
- **事务化关账**：关账时在事务中固化租户级**接收截止序号**（cutoff_seq），并发到达的记录确定性地落在截止线某一侧。
- **不可变账单**：按发生时间归属本期且截止前收到的记录进入账单；迟到/关账后的修正进入下期**调整行**；历史账单不可重开。
- **可重算**：试算（preview）、账单明细、逐事件计价轨迹（trace），任何账单都能按固化的价格版本与截止线重复计算出完全相同的结果（verify）。

## 快速开始

```bash
docker compose up --build
```

启动后：

| 地址 | 说明 |
|---|---|
| `http://localhost:8000` | API 入口（返回调用流程指引） |
| `http://localhost:8000/docs` | Swagger UI 交互文档 |
| `http://localhost:8000/openapi.json` | OpenAPI 规范 |
| `http://localhost:8000/health` | 健康检查 |

一键演示完整流程（建价目 → 采集 → 试算 → 关账 → 迟到修正 → 下期调整 → 重算验证）：

```bash
./scripts/smoke.sh            # 需要 curl；有 jq 输出更美
```

运行测试：

```bash
docker compose exec app pytest tests/ -v          # 全部（计价单测 + API 集成 + 并发截止测试）
BASE_URL=http://localhost:8000 pytest tests/      # 从宿主机跑
```

## 数据模型与不变量

```
tenants ──< tenant_counters      每租户一行接收序号（行锁串行化采集与关账）
tenants ──< usage_records        追加式账本：event / correction / reversal
price_plans ──< price_versions ──< price_tiers     版本化阶梯价目
tenants ──< billing_periods ──< bills ──< bill_lines
```

数据库层强制（`app/db.py` 启动时安装）：

- `usage_records`、`bills`、`bill_lines`、`price_versions`、`price_tiers` 上 `BEFORE UPDATE OR DELETE` 触发器直接报错 —— 账本与账单物理不可变。
- `usage_records (tenant_id, source, event_id)` 唯一 —— 同源事件只入账一次。
- `usage_records (tenant_id, recv_seq)` 唯一 —— 接收顺序全局确定。
- `price_versions` 上 `EXCLUDE USING gist (plan_id WITH =, tstzrange(...) WITH &&)` —— 同计划生效区间不重叠。

## 关键设计

### 接收截止序号（并发安全关账）

每条采集记录入库时，通过 `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` 原子地领取租户级序号 `recv_seq`（行锁持有至提交）。关账事务对同一行 `SELECT ... FOR UPDATE` 读取当前值作为 `cutoff_seq`：

- 行锁使关账事务等待所有在途采集事务提交/回滚 —— 读到的截止值之后不会再冒出 `recv_seq <= cutoff` 的记录；
- 关账提交后开始的采集必然拿到 `recv_seq > cutoff`。

因此每条记录确定性地落在截止线一侧：窗口内且 `recv_seq <= cutoff_seq` 进本期账单，其余滚入下期。

### 账单归属与迟到调整

- **本期行（current）**：有效 `occurred_at ∈ [period_start, period_end)` 且 `recv_seq <= cutoff_seq` 的事件，按账期起始时刻命中的价格版本计价。
- **调整行（adjustment）**：对每个已关闭的历史账期，用**当前截止序号**下的账本状态、**该期冻结的价格版本**重新计价，与已开账金额（该期原账单 + 历次调整）逐事件求差，差额计入本期调整行。修正、撤销、修正改期（跨期移动）都由此自然覆盖。
- 账期必须按起始时间顺序关闭；已关闭账期不可重开（重开关账返回 409）。

### 阶梯价与逐事件轨迹

同一 metric 的事件按 `(occurred_at, recv_seq, id)` 固定顺序走过阶梯括号，得到每事件原始金额；metric 总额一次性量化到分，再按**最大余数法**把舍入残差确定性地分摊回事件 —— 恒有 `Σ事件行 = 阶梯总额 = 账单总额`。每行的 `trace` 保存完整括号切片（`from/to/unit_price/quantity/amount`）与残差标记，轨迹即账单、账单即轨迹。

### 可重算验证

`POST /bills/{id}/verify` 仅用冻结输入重算：追加式账本中 `recv_seq <= cutoff_seq` 的记录（永不变）、冻结的价格版本、账期窗口、`id < bill.id` 的既有账单（调整基线），逐行比对金额与数量。以上输入全部不可变，因此任何账单任何时候都能重算出相同结果。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/plans` | 创建价格计划（币种） |
| POST | `/plans/{id}/versions` | 发布价格版本（阶梯 tiers；区间重叠 409） |
| GET | `/plans/{id}/versions` | 版本列表 |
| POST | `/tenants` | 创建租户（可挂计划） |
| PUT | `/tenants/{code}/plan` | 为租户指定价格计划 |
| POST | `/usage` | 采集用量（幂等；同键异内容 409） |
| POST | `/usage/{source}/{event_id}/corrections` | 追加修正（整体替换语义，可自带幂等键） |
| POST | `/usage/{source}/{event_id}/reversals` | 追加撤销（幂等） |
| GET | `/usage/{source}/{event_id}/chain?tenant_code=` | 事件完整追加链 |
| GET | `/tenants/{code}/usage` | 账本查询 |
| POST | `/periods` | 开账期（重叠 409） |
| POST | `/periods/{id}/preview` | **试算**：假设此刻关账的结果，不落库 |
| POST | `/periods/{id}/close` | **关账**：事务内固化截止序号并生成不可变账单 |
| GET | `/tenants/{code}/periods` / `/periods/{id}` | 账期查询 |
| GET | `/tenants/{code}/bills` / `/bills/{id}` | 账单列表 / 明细（含 trace） |
| GET | `/bills/{id}/trace` | **逐事件计价轨迹** |
| POST | `/bills/{id}/verify` | **重算验证**：冻结输入重算并逐行比对 |

## 调用示例

```bash
# 阶梯价：前 100 单位 0.10，其后 0.05
curl -X POST localhost:8000/plans -d '{"name":"std","currency":"USD"}'
curl -X POST localhost:8000/plans/1/versions -d '{
  "version":1,"effective_from":"2026-01-01T00:00:00Z","effective_to":null,
  "tiers":[{"metric":"api_calls","up_to":"100","unit_price":"0.10"},
           {"metric":"api_calls","up_to":null,"unit_price":"0.05"}]}'
curl -X POST localhost:8000/tenants -d '{"code":"acme","name":"Acme","plan_id":1}'

# 采集（重放安全）
curl -X POST localhost:8000/usage -d '{
  "tenant_code":"acme","source":"meter","event_id":"evt-1",
  "occurred_at":"2026-01-10T10:00:00Z","quantity":"120",
  "metric":"api_calls","dimensions":{"region":"eu"}}'

# 账期：试算 -> 关账 -> 轨迹 -> 验证
curl -X POST localhost:8000/periods -d '{"tenant_code":"acme",
  "period_start":"2026-01-01T00:00:00Z","period_end":"2026-02-01T00:00:00Z"}'
curl -X POST localhost:8000/periods/1/preview
curl -X POST localhost:8000/periods/1/close
curl localhost:8000/bills/1/trace
curl -X POST localhost:8000/bills/1/verify
```

## 项目结构

```
app/
  main.py       FastAPI 装配与启动（建表 + 不可变触发器）
  models.py     SQLAlchemy 模型（唯一约束 / 排他约束）
  pricing.py    纯函数阶梯计价引擎（Decimal、最大余数分摊）
  ledger.py     幂等采集、追加式修正/撤销、截止时点的有效状态解析
  billing.py    关账 / 试算 / 重算验证（截止序号冻结、迟到调整）
  routers/      tenants / plans / usage / periods / bills
tests/
  test_pricing.py  计价引擎单测（无需数据库）
  test_api.py      端到端集成测试（含并发截止确定性测试）
scripts/smoke.sh   全流程 curl 演示
```
