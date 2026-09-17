-- SaaS 用量计费结算系统 - 数据库结构
-- 金额一律 NUMERIC(28,8)，计算过程使用 Decimal。

CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- 租户
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 每租户“接收序号”计数器：入账时在同一事务内 +1，固化截止线时对该行加锁读取。
CREATE TABLE IF NOT EXISTS tenant_counters (
    tenant_id        UUID PRIMARY KEY REFERENCES tenants(id),
    last_recv_seq    BIGINT NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- 结算周期（按租户连续、不重叠）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS billing_periods (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    period_start TIMESTAMPTZ NOT NULL,
    period_end   TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','closed')),
    -- 关闭时固化：本租户最后一笔被本账接收的记录序号
    cutoff_recv_seq BIGINT,
    closed_at    TIMESTAMPTZ,
    bill_id      UUID,
    CONSTRAINT chk_period_range CHECK (period_end > period_start),
    CONSTRAINT uq_period UNIQUE (tenant_id, period_start)
);

-- 同一租户的周期区间不得重叠
CREATE UNIQUE INDEX IF NOT EXISTS uq_periods_no_overlap
    ON billing_periods USING gist (
        tenant_id,
        tstzrange(period_start, period_end, '[)')
    );

-- 关闭后截止线与账单不可再改；未关闭时这些列必须为空
ALTER TABLE billing_periods DROP CONSTRAINT IF EXISTS chk_closed_state;
ALTER TABLE billing_periods ADD CONSTRAINT chk_closed_state CHECK (
    (status = 'open'  AND cutoff_recv_seq IS NULL AND closed_at IS NULL AND bill_id IS NULL)
    OR
    (status = 'closed' AND cutoff_recv_seq IS NOT NULL AND closed_at IS NOT NULL AND bill_id IS NOT NULL)
);

-- ---------------------------------------------------------------------------
-- 用量事件（只追加账册：禁止 UPDATE / DELETE）
--   event_type: usage（原始用量）/ correction（修正）/ cancellation（撤销）
--   correction / cancellation 必须通过 linked_event_id 关联到同租户同 source 的账内记录
--   同一 (tenant_id, source, event_id) 只能入账一次；同键异内容由应用层报 409
--   recv_seq 为该租户的单调接收序号（截止线判定依据）
--   root_event_id 指向同一条业务事件链的首条 usage 记录
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_events (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id),
    source           TEXT NOT NULL,
    event_id         TEXT NOT NULL,
    event_type       TEXT NOT NULL CHECK (event_type IN ('usage','correction','cancellation')),
    occurred_at      TIMESTAMPTZ NOT NULL,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    recv_seq         BIGINT NOT NULL,
    quantity         NUMERIC(28,8) NOT NULL,
    dimensions       JSONB NOT NULL DEFAULT '{}'::jsonb,
    linked_event_id  UUID REFERENCES usage_events(id),
    root_event_id    UUID NOT NULL,
    content_hash     TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_source_event UNIQUE (tenant_id, source, event_id)
);

CREATE INDEX IF NOT EXISTS idx_events_tenant_occurred ON usage_events (tenant_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_tenant_recv     ON usage_events (tenant_id, recv_seq);
CREATE INDEX IF NOT EXISTS idx_events_root            ON usage_events (root_event_id);
CREATE INDEX IF NOT EXISTS idx_events_linked          ON usage_events (linked_event_id);

-- ---------------------------------------------------------------------------
-- 价格表（版本化）
--   tenant_id 为空表示该 source 的默认价格；非空表示租户专属价格（优先于默认）。
--   [effective_from, effective_to) 生效区间；同 (tenant, source) 区间不得重叠。
--   版本一经创建，其层级不可修改；新版本只能让旧版本在新版本开始时“收尾关闭”。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_versions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID REFERENCES tenants(id),  -- NULL = 默认价格
    source         TEXT NOT NULL,
    currency       TEXT NOT NULL DEFAULT 'USD',
    pricing_mode   TEXT NOT NULL DEFAULT 'volume'
                     CHECK (pricing_mode IN ('volume','graduated')),
    effective_from TIMESTAMPTZ NOT NULL,
    effective_to   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_price_range CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE INDEX IF NOT EXISTS idx_price_versions_lookup
    ON price_versions (tenant_id, source, effective_from);

-- 默认价格之间区间不重叠（tenant_id IS NULL）
CREATE UNIQUE INDEX IF NOT EXISTS uq_default_price_no_overlap
    ON price_versions USING gist (
        source,
        tstzrange(effective_from, COALESCE(effective_to, 'infinity'::timestamptz), '[)')
    )
    WHERE tenant_id IS NULL;

-- 租户专属价格之间区间不重叠
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_price_no_overlap
    ON price_versions USING gist (
        tenant_id,
        source,
        tstzrange(effective_from, COALESCE(effective_to, 'infinity'::timestamptz), '[)')
    )
    WHERE tenant_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 价格阶梯
--   volume 模式：总量落入唯一一档，quantity * unit_amount + flat_amount
--   graduated 模式：逐档切片，每片 quantity * unit_amount + flat_amount 求和
--   up_to_qty 为 NULL 表示开区间（最后一档）；层级不允许重叠/空洞。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_tiers (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    price_version_id UUID NOT NULL REFERENCES price_versions(id) ON DELETE CASCADE,
    tier_index       INT  NOT NULL,
    from_qty         NUMERIC(28,8) NOT NULL,
    up_to_qty        NUMERIC(28,8),
    unit_amount      NUMERIC(28,8) NOT NULL,
    flat_amount      NUMERIC(28,8) NOT NULL DEFAULT 0,
    CONSTRAINT uq_tier_order UNIQUE (price_version_id, tier_index),
    CONSTRAINT chk_tier_bounds CHECK (
        from_qty >= 0
        AND (up_to_qty IS NULL OR up_to_qty > from_qty)
    )
);

-- ---------------------------------------------------------------------------
-- 账单（一旦生成即不可变，历史账单永不重开）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bills (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id),
    period_id        UUID NOT NULL UNIQUE REFERENCES billing_periods(id),
    currency         TEXT NOT NULL,
    -- 固化的截止线：仅 recv_seq <= cutoff_recv_seq 的事件可入本账
    cutoff_recv_seq  BIGINT NOT NULL,
    total_amount     NUMERIC(28,8) NOT NULL,
    generated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bills_tenant ON bills (tenant_id, generated_at);

-- billing_periods.bill_id 反向引用账单；与 bills.period_id 构成互引用，
-- 使用可延迟外键以支持同一事务内先建账单再固化周期。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_period_bill'
    ) THEN
        ALTER TABLE billing_periods
            ADD CONSTRAINT fk_period_bill FOREIGN KEY (bill_id)
            REFERENCES bills(id) DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

-- 账单项：正常用量 / 调整（迟到记录或关闭后的修正撤销）
CREATE TABLE IF NOT EXISTS bill_lines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bill_id             UUID NOT NULL REFERENCES bills(id),
    event_id            UUID NOT NULL REFERENCES usage_events(id),
    line_kind           TEXT NOT NULL CHECK (line_kind IN ('usage','adjustment')),
    period_attribution  TEXT NOT NULL CHECK (period_attribution IN ('current','late','correction')),
    price_version_id    UUID NOT NULL REFERENCES price_versions(id),
    quantity            NUMERIC(28,8) NOT NULL,
    amount              NUMERIC(28,8) NOT NULL,
    pricing_trace       JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 同一条账册事件只能进入一张账单（杜绝重复计价；修正/撤销各自是新事件）
    CONSTRAINT uq_billed_event UNIQUE (event_id)
);

CREATE INDEX IF NOT EXISTS idx_bill_lines_bill ON bill_lines (bill_id);
CREATE INDEX IF NOT EXISTS idx_periods_bill ON billing_periods (bill_id);

-- ---------------------------------------------------------------------------
-- 只追加保护：事件、账单、账单项、已关闭周期一律禁止改写
-- 价格版本允许把 effective_to 从 NULL 收尾为具体时间（应用层只做这一种 UPDATE）。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only / immutable; append a new record instead',
        TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_events_immutable ON usage_events;
CREATE TRIGGER trg_events_immutable
    BEFORE UPDATE OR DELETE ON usage_events
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

DROP TRIGGER IF EXISTS trg_bills_immutable ON bills;
CREATE TRIGGER trg_bills_immutable
    BEFORE UPDATE OR DELETE ON bills
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

DROP TRIGGER IF EXISTS trg_bill_lines_immutable ON bill_lines;
CREATE TRIGGER trg_bill_lines_immutable
    BEFORE UPDATE OR DELETE ON bill_lines
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- 结算周期：唯一允许的变更是 open -> closed 的一次性固化；
-- 关闭后任何列不得再改（历史账单不能重开）。DELETE 一律禁止。
CREATE OR REPLACE FUNCTION billing_period_close_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'billing_periods is append-only; historical periods cannot be removed'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- 已关闭周期的任何字段变更都拒绝（历史账单不能重开）
    IF OLD.status = 'closed' AND ROW(NEW.*) IS DISTINCT FROM ROW(OLD.*) THEN
        RAISE EXCEPTION 'billing period is closed; the bill is immutable and cannot be reopened'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- 未关闭时标识与区间也不允许改（status/截止线/账单只能由关账事务一次性固化）
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.period_start IS DISTINCT FROM OLD.period_start
       OR NEW.period_end IS DISTINCT FROM OLD.period_end THEN
        RAISE EXCEPTION 'period identity and range are immutable'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_period_close_only ON billing_periods;
CREATE TRIGGER trg_period_close_only
    BEFORE UPDATE OR DELETE ON billing_periods
    FOR EACH ROW EXECUTE FUNCTION billing_period_close_only();

DROP TRIGGER IF EXISTS trg_price_tiers_immutable ON price_tiers;
CREATE TRIGGER trg_price_tiers_immutable
    BEFORE UPDATE OR DELETE ON price_tiers
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- 价格版本：唯一允许的变更是把打开的 effective_to (NULL) 收尾为一个时间点；
-- 关闭后不得再动，其它列一律不可改。DELETE 一律禁止。
CREATE OR REPLACE FUNCTION price_version_clamp_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'price_versions is append-only; create a new version instead'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.source IS DISTINCT FROM OLD.source
       OR NEW.currency IS DISTINCT FROM OLD.currency
       OR NEW.pricing_mode IS DISTINCT FROM OLD.pricing_mode
       OR NEW.effective_from IS DISTINCT FROM OLD.effective_from
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'price version fields are immutable; only effective_to may be set once'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF OLD.effective_to IS NOT NULL AND NEW.effective_to IS DISTINCT FROM OLD.effective_to THEN
        RAISE EXCEPTION 'effective_to is already closed and cannot be changed'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF NEW.effective_to IS NOT NULL AND NEW.effective_to <= NEW.effective_from THEN
        RAISE EXCEPTION 'effective_to must be after effective_from'
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_price_version_clamp ON price_versions;
CREATE TRIGGER trg_price_version_clamp
    BEFORE UPDATE OR DELETE ON price_versions
    FOR EACH ROW EXECUTE FUNCTION price_version_clamp_only();
