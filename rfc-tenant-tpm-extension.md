# RFC: `ai-rate-limiting-redis` — Per-Tenant TPM 擴充

| 欄位 | 內容 |
|---|---|
| **RFC 編號** | RFC-002 |
| **標題** | Per-Tenant TPM Rate Limiting Extension for ai-rate-limiting-redis |
| **狀態** | Draft |
| **建立日期** | 2026-03-31 |
| **相依 RFC** | RFC-001 (ai-rate-limiting-redis) |

---

## 1. 背景與需求

### 1.1 現有問題

`ai-rate-limiting-redis`（RFC-001）以 **AI Instance** 為維度做 TPM 限流，計數器 Key 是 instance name。
這對「保護單一 LLM 後端」有效，但無法解決以下場景：

> **同一個 AI Instance 被多個 Tenant 共用，但每個 Tenant 的 TPM 配額應該互相獨立計算。**

若不加 tenant 維度，就會出現：
- Tenant A 的大量請求把整個 instance 配額耗盡，導致 Tenant B 全部被拒
- 無法對 VIP Tenant 提供更高配額

### 1.2 功能需求

| # | 需求 | 說明 |
|---|---|---|
| R1 | Tenant ID 來源 | 從 HTTP request header `t-tenant-id` 取得，格式如 `t-12345678` |
| R2 | 預設配額 | 大多數 Tenant 套用統一的 default TPM 上限 |
| R3 | 個別覆寫 | 特定 Tenant 可設定不同的 TPM 上限（高或低） |
| R4 | 獨立計數器 | 每個 Tenant 的 Redis counter 互不影響 |
| R5 | 無侵入性 | 只修改 `ai-rate-limiting-redis`，不動 APISIX 原生 plugin |
| R6 | 向後相容 | 未設定 `tenant_tpm` 時行為與 v1 完全一致 |
| R7 | 無 Tenant Header | Request 沒帶 `t-tenant-id` 時跳過 tenant 檢查，不影響 instance 限流 |

---

## 2. 架構設計

### 2.1 雙維度限流模型

```
每個 Request 需同時通過兩道限流閘：

┌──────────────────────────────────────────────────────────────────┐
│  Access Phase                                                    │
│                                                                  │
│  Step 1:  Instance 維度  →  Redis Key: "<conf_id>#openai-primary:openai-primary"  │
│           limit: 100,000 TPM (整個 instance 共享)                                │
│                    ↓ pass                                                        │
│  Step 2:  Tenant 維度    →  Redis Key: "<conf_id>#tenant#t-12345678:tenant#..."  │
│           limit:  10,000 TPM (此 tenant 個人配額)                │
│                    ↓ pass                                        │
│           放行請求至上游                                          │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  Log Phase (ngx.timer)                                           │
│                                                                  │
│  同時更新：                                                       │
│    ① Instance counter  INCRBY "<conf_id>#openai-primary:..."         (actual - 1)  │
│    ② Tenant   counter  INCRBY "<conf_id>#tenant#t-12345678:..."      (actual - 1)  │
└──────────────────────────────────────────────────────────────────┘
```

### 2.2 Redis Key 命名

APISIX `limit-count` 的 `gen_limit_key` 函數在沒有 `conf.group` 時，會要求 `conf._meta.parent.resource_key`（由 APISIX plugin loader 注入）。自建 `limit_conf` 不具備此欄位，因此必須設定 `conf.group` 來觸發 bypass path。

最終 Redis key 格式：

```
"plugin-" + plugin_name + conf.group + ":" + conf.key
```

```
Instance 維度（現有）:
  key = "plugin-ai-rate-limiting-redis<conf_id>#<instance_name>:<instance_name>"
  ex:   "plugin-ai-rate-limiting-redisroute-abc#openai-primary:openai-primary"

Tenant 維度（新增）:
  key = "plugin-ai-rate-limiting-redis<conf_id>#tenant#<tenant_id>:tenant#<tenant_id>"
  ex:   "plugin-ai-rate-limiting-redisroute-abc#tenant#t-12345678:tenant#t-12345678"
```

其中 `<conf_id>` = `plugin_conf._meta.id`（APISIX route plugin config ID，跨重啟穩定）。

> 兩個 key 在 Redis 中完全獨立，TTL 也各自管理。

### 2.3 流程圖

```
Request with header:  t-tenant-id: t-12345678
                              │
                    ┌─────────▼──────────┐
                    │   access phase     │
                    └─────────┬──────────┘
                              │
               ┌──────────────▼──────────────────┐
               │  STEP 1: Instance TPM check                              │
               │  group = "<conf_id>#openai-primary"                  │
               │  INCRBY key 1 (placeholder)      │
               └──────────────┬──────────────────┘
                              │
               ┌──────────────▼──────────────────┐  instance 超限
               │  remaining >= 0 ?               ├──────────────────────►  429 / 503
               └──────────────┬──────────────────┘  restore instance +1
                              │ OK
               ┌──────────────▼──────────────────┐
               │  STEP 2: Tenant TPM check                                │  (只有 tenant_tpm 設定時才執行)
               │  group = "<conf_id>#tenant#t-12345678"               │
               │  INCRBY key 1 (placeholder)      │
               └──────────────┬──────────────────┘
                              │
               ┌──────────────▼──────────────────┐  tenant 超限
               │  remaining >= 0 ?               ├──────────────────────►  429 / 503
               └──────────────┬──────────────────┘  restore instance +1
                              │ OK               restore tenant +1
                              │
               ctx.ai_tenant_id = "t-12345678"
                              │
                    ┌─────────▼──────────┐
                    │  forward to LLM    │
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │   log phase        │
                    │ (ngx.timer.at 0)   │
                    └─────────┬──────────┘
                              │
               ┌──────────────▼──────────────────┐
               │  actual_tokens = 847             │
               │  extra = 847 - 1 = 846           │
               │                                  │
               │  INCRBY "<conf_id>#openai-primary:openai-primary"   +846  │
               │  INCRBY "<conf_id>#tenant#t-12345678:tenant#..."     +846  │
               └──────────────────────────────────┘
```

---

## 3. Schema 變更

### 3.1 新增欄位：`tenant_tpm`

```jsonc
{
  "tenant_tpm": {
    // 必填：套用到所有沒有 override 的 tenant
    "default": {
      "limit": 10000,       // 每個 tenant 每時間窗口的 token 上限
      "time_window": 60     // 時間窗口（秒）
    },
    // 選填：特定 tenant 的個別設定
    "overrides": {
      "t-12345678": {       // VIP tenant，更高配額
        "limit": 50000,
        "time_window": 60
      },
      "t-99999999": {       // 受限 tenant，更低配額
        "limit": 1000,
        "time_window": 60
      }
    }
  }
}
```

### 3.2 配額查找優先序

```
Request tenant_id = "t-12345678"

1. 查 overrides["t-12345678"]  → 找到 → 用 50,000 TPM
2. 查 overrides["t-unknown"]   → 找不到 → 用 default 10,000 TPM
3. 無 t-tenant-id header       → 跳過 tenant 檢查
```

### 3.3 完整 Schema 範例

```json
{
  "instances": [
    { "name": "openai-primary",  "limit": 100000, "time_window": 60 },
    { "name": "deepseek-backup", "limit": 50000,  "time_window": 60 }
  ],
  "tenant_tpm": {
    "default": { "limit": 10000, "time_window": 60 },
    "overrides": {
      "t-00000001": { "limit": 50000, "time_window": 60 },
      "t-00000002": { "limit": 1000,  "time_window": 60 }
    }
  },
  "limit_strategy":  "total_tokens",
  "rejected_code":   429,
  "rejected_msg":    "TPM rate limit exceeded",
  "show_limit_quota_header": true,
  "policy":          "redis",
  "redis_host":      "redis.internal",
  "redis_port":      6379,
  "redis_password":  "secret"
}
```

---

## 4. 實作細節

### 4.1 新增的程式碼區塊（最小化改動）

v1 → v2 的差異只有以下 4 個地方：

| # | 位置 | 改動 |
|---|---|---|
| 1 | Schema | 新增 `tenant_limit_entry_schema`、`tenant_tpm_schema`、`tenant_tpm` 屬性 |
| 2 | 新函式 `build_tenant_limit_conf` | 根據 tenant_id + plugin_conf 建立 limit_conf |
| 3 | `_M.access` | Step 1 後增加 Step 2 tenant 檢查，寫入 `ctx.ai_tenant_id` |
| 4 | `_M.log` | ngx.timer 內增加 tenant counter 的 INCRBY |

**`_M.check_instance_status` 不做修改**（此函式用於 ai-proxy-multi fallback 判斷，不需要 tenant 維度）

### 4.2 LRU Cache 設計

```
limit_conf_cache       { ttl=300, count=512  }   ← 現有，keyed by conf table
tenant_limit_conf_cache{ ttl=300, count=4096 }   ← 新增，keyed by "<conf_id>#<tenant_id>"
```

tenant cache 使用字串 key 是因為 tenant_id 來自 request，無法以 conf table identity 作為 key。
count=4096 可容納 4096 個不同的 (config, tenant) 組合，應足以應對大多數生產場景。

### 4.3 Tenant 不存在時的行為

| 情境 | 行為 |
|---|---|
| Request 沒有 `t-tenant-id` header | 跳過 tenant 檢查，只做 instance 限流 |
| `tenant_tpm` 沒有設定 | 跳過 tenant 檢查（完全向後相容） |
| Tenant ID 不在 overrides 中 | 使用 `default` 配額 |
| Redis 連線失敗（`allow_degradation: true`） | Fail-open，允許通過，記錄 error log |

---

## 5. Response Headers

啟用 `show_limit_quota_header: true` 時，Response 會包含兩組 Headers：

```
# Instance 維度（原有）
X-AI-RateLimit-Limit-openai-primary:      100000
X-AI-RateLimit-Remaining-openai-primary:  99153
X-AI-RateLimit-Reset-openai-primary:      42

# Tenant 維度（新增）
X-AI-RateLimit-Limit-Tenant:     10000
X-AI-RateLimit-Remaining-Tenant: 9153
X-AI-RateLimit-Reset-Tenant:     42
```

---

## 6. 測試計畫

### 6.1 測試環境準備

```bash
# 1. 啟動 Redis（local）
docker run -d --name redis-test -p 6379:6379 redis:7-alpine

# 2. 確認 Redis 連線
redis-cli ping  # PONG

# 3. 查詢當前所有 rate limit keys（找出 conf_id）
redis-cli KEYS "plugin-ai-rate-limiting-redis*"
# ex output: "plugin-ai-rate-limiting-redisroute-abc123#openai-primary:openai-primary"
# conf_id = "route-abc123" (字串中第一個 # 前的部分)

# 4. 清除測試 Key（每個測試開始前執行）
redis-cli KEYS "plugin-ai-rate-limiting-redis*" | xargs redis-cli DEL
```

> **注意：** Redis key 包含 `conf_id`（APISIX route plugin config ID）。實際值可用 `redis-cli KEYS "plugin-ai-rate-limiting-redis*"` 查詢，或從 APISIX admin API 的路由設定中確認。
> 以下測試範例用 `<conf_id>` 代表此值。

#### APISIX 路由設定（所有測試共用）

```json
{
  "uri": "/ai/chat",
  "plugins": {
    "ai-proxy-multi": {
      "instances": [
        {
          "name": "openai-primary",
          "provider": "openai",
          "weight": 100,
          "priority": 1,
          "auth": { "header": { "Authorization": "Bearer $OPENAI_KEY" } },
          "options": { "model": "gpt-4o-mini" }
        }
      ]
    },
    "ai-rate-limiting-redis": {
      "instances": [
        { "name": "openai-primary", "limit": 100000, "time_window": 60 }
      ],
      "tenant_tpm": {
        "default":   { "limit": 1000, "time_window": 60 },
        "overrides": {
          "t-00000001": { "limit": 5000, "time_window": 60 },
          "t-00000002": { "limit": 200,  "time_window": 60 }
        }
      },
      "limit_strategy":  "total_tokens",
      "rejected_code":   429,
      "rejected_msg":    "TPM rate limit exceeded",
      "show_limit_quota_header": true,
      "policy":          "redis",
      "redis_host":      "127.0.0.1",
      "redis_port":      6379
    }
  }
}
```

---

### 6.2 測試案例

#### TC-01: 無 Tenant Header — 只做 Instance 限流

**目的：** 確認沒有 `t-tenant-id` 時行為與 v1 完全一致。

```bash
curl -X POST http://apisix:9080/ai/chat \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hi"}]}'
```

**預期：**
- HTTP 200
- Response 有 `X-AI-RateLimit-Limit-openai-primary`
- Response **沒有** `X-AI-RateLimit-Limit-Tenant`
- Redis 中 `tenant#*` key 不存在

**驗證：**
```bash
redis-cli KEYS "plugin-ai-rate-limiting-redis*tenant*"  # 應為空
```

---

#### TC-02: Default Tenant 配額（未在 overrides 的 tenant）

**目的：** 確認不在 overrides 的 tenant 套用 default limit (1000 TPM)。

```bash
# tenant t-99999999 不在 overrides，套用 default 1000 TPM
curl -X POST http://apisix:9080/ai/chat \
  -H "t-tenant-id: t-99999999" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hello"}]}'
```

**預期：**
- HTTP 200
- `X-AI-RateLimit-Limit-Tenant: 1000`
- `X-AI-RateLimit-Remaining-Tenant: 999`（access phase 扣 1 placeholder）

**驗證：**
```bash
# 找出實際 key（含 conf_id）
redis-cli KEYS "plugin-ai-rate-limiting-redis*tenant#t-99999999*"
# ex: "plugin-ai-rate-limiting-redis<conf_id>#tenant#t-99999999:tenant#t-99999999"

# 確認 TTL = 60 秒、餘額為 999（placeholder -1）
redis-cli TTL  "plugin-ai-rate-limiting-redis<conf_id>#tenant#t-99999999:tenant#t-99999999"  # 接近 60
redis-cli GET  "plugin-ai-rate-limiting-redis<conf_id>#tenant#t-99999999:tenant#t-99999999"  # 999
```

---

#### TC-03: VIP Tenant 套用高配額 override

**目的：** 確認 t-00000001 套用 override 5000 TPM（不是 default 1000）。

```bash
curl -X POST http://apisix:9080/ai/chat \
  -H "t-tenant-id: t-00000001" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hello"}]}'
```

**預期：**
- HTTP 200
- `X-AI-RateLimit-Limit-Tenant: 5000`
- `X-AI-RateLimit-Remaining-Tenant: 4999`

**驗證：**
```bash
redis-cli GET "plugin-ai-rate-limiting-redis<conf_id>#tenant#t-00000001:tenant#t-00000001"  # 應為 4999
```

---

#### TC-04: 受限 Tenant 套用低配額 override

**目的：** 確認 t-00000002 套用 override 200 TPM。

```bash
curl -X POST http://apisix:9080/ai/chat \
  -H "t-tenant-id: t-00000002" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hello"}]}'
```

**預期：**
- HTTP 200
- `X-AI-RateLimit-Limit-Tenant: 200`

---

#### TC-05: Tenant 超限 → 返回 429

**目的：** 模擬 t-00000002 的 200 TPM 配額被耗盡。

```bash
# 直接用 redis-cli 把 t-00000002 的餘額設成 0（模擬已耗盡）
TENANT_KEY="plugin-ai-rate-limiting-redis<conf_id>#tenant#t-00000002:tenant#t-00000002"
redis-cli SET "$TENANT_KEY" 0 EX 60

# 發送請求，應被拒絕
curl -v -X POST http://apisix:9080/ai/chat \
  -H "t-tenant-id: t-00000002" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hello"}]}'
```

**預期：**
- HTTP 429
- Body: `TPM rate limit exceeded`
- Instance counter 未被消耗（placeholder 已還原）

**驗證：**
```bash
INST_KEY="plugin-ai-rate-limiting-redis<conf_id>#openai-primary:openai-primary"
TENANT_KEY="plugin-ai-rate-limiting-redis<conf_id>#tenant#t-00000002:tenant#t-00000002"
redis-cli GET "$INST_KEY"    # 應維持超限前的值（placeholder 已還原）
redis-cli GET "$TENANT_KEY"  # 應為 0 或 -1（INCRBY 後 restore）
```

---

#### TC-06: 兩個 Tenant 計數器互相獨立

**目的：** 確認 t-00000001 和 t-99999999 的消耗不互相影響。

```bash
# Tenant A 發送請求
curl -X POST http://apisix:9080/ai/chat \
  -H "t-tenant-id: t-00000001" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"A"}]}'

# Tenant B 發送請求
curl -X POST http://apisix:9080/ai/chat \
  -H "t-tenant-id: t-99999999" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"B"}]}'
```

**驗證：**
```bash
KEY_A="plugin-ai-rate-limiting-redis<conf_id>#tenant#t-00000001:tenant#t-00000001"
KEY_B="plugin-ai-rate-limiting-redis<conf_id>#tenant#t-99999999:tenant#t-99999999"

redis-cli GET "$KEY_A"   # 接近 4999（5000 - 1 placeholder）
redis-cli GET "$KEY_B"   # 接近 999（1000 - 1 placeholder）

# 等待 LLM 回應後（log phase timer 執行後）：
# KEY_A = 5000 - actual_tokens_A
# KEY_B = 1000 - actual_tokens_B
```

---

#### TC-07: Instance 超限不受 Tenant 影響

**目的：** 確認 instance 超限時 tenant counter 不被消耗。

```bash
# 把 instance counter 設為 0（模擬 instance 耗盡）
INST_KEY="plugin-ai-rate-limiting-redis<conf_id>#openai-primary:openai-primary"
redis-cli SET "$INST_KEY" 0 EX 60

# 任意 tenant 發請求 → 應在 Step 1 就被拒
curl -v -X POST http://apisix:9080/ai/chat \
  -H "t-tenant-id: t-00000001" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hi"}]}'
```

**預期：**
- HTTP 429
- tenant counter 未增加（Step 1 失敗後 Step 2 不執行）

**驗證：**
```bash
TENANT_KEY="plugin-ai-rate-limiting-redis<conf_id>#tenant#t-00000001:tenant#t-00000001"
redis-cli EXISTS "$TENANT_KEY"  # 應為 0（key 不存在，從未被寫入）
```

---

#### TC-08: 同一 Tenant 多次請求 — Log Phase 累計驗證

**目的：** 確認 log phase 的 extra_tokens 正確累積到 tenant counter。

```bash
# 假設每次請求實際消耗 100 tokens，發送 3 次
for i in 1 2 3; do
  curl -X POST http://apisix:9080/ai/chat \
    -H "t-tenant-id: t-99999999" \
    -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Count '$i'"}]}'
  sleep 1
done
```

**預期（假設每次 100 tokens）：**
```
TENANT_KEY = "plugin-ai-rate-limiting-redis<conf_id>#tenant#t-99999999:tenant#t-99999999"

初始:          TENANT_KEY = 1000
請求 1 access: TENANT_KEY = 999  (佔位 -1)
請求 1 log:    TENANT_KEY = 901  (再扣 99 = actual 100 - placeholder 1)
請求 2 access: TENANT_KEY = 900
請求 2 log:    TENANT_KEY = 801
請求 3 access: TENANT_KEY = 800
請求 3 log:    TENANT_KEY = 701
```

**驗證：**
```bash
# 等待所有 timer 執行完畢後
TENANT_KEY="plugin-ai-rate-limiting-redis<conf_id>#tenant#t-99999999:tenant#t-99999999"
redis-cli GET "$TENANT_KEY"
# 預期值 ≈ 1000 - (100 * 3) = 700
```

---

#### TC-09: 多 Tenant 並發壓力測試 — 計數器獨立性

**目的：** 並發場景下各 tenant 的計數器仍然獨立正確。

```bash
# 安裝 wrk 或使用 ab
# 同時對 3 個 tenant 打流量，各 100 個請求

# Terminal 1
wrk -t2 -c10 -d10s -H "t-tenant-id: t-00000001" \
    -s post.lua http://apisix:9080/ai/chat &

# Terminal 2
wrk -t2 -c10 -d10s -H "t-tenant-id: t-99999999" \
    -s post.lua http://apisix:9080/ai/chat &

# Terminal 3 （無 tenant header）
wrk -t2 -c10 -d10s \
    -s post.lua http://apisix:9080/ai/chat &

wait
```

**驗證：**
```bash
KEY_A="plugin-ai-rate-limiting-redis<conf_id>#tenant#t-00000001:tenant#t-00000001"
KEY_B="plugin-ai-rate-limiting-redis<conf_id>#tenant#t-99999999:tenant#t-99999999"
KEY_I="plugin-ai-rate-limiting-redis<conf_id>#openai-primary:openai-primary"

redis-cli GET "$KEY_A"  # 應有消耗，接近上限 5000
redis-cli GET "$KEY_B"  # 應有消耗，接近上限 1000，可能有 429
redis-cli GET "$KEY_I"  # 全部流量共享 100000 TPM
```

---

#### TC-10: Tenant override 熱更新（Config 更新後生效）

**目的：** 確認更新 overrides 配額後，舊的 LRU cache 在 TTL(300s) 後過期，新配額生效。

```bash
# Step 1: t-00000001 當前 override = 5000
# Step 2: 更新 APISIX 路由，把 t-00000001 的 override 改為 8000
# Step 3: 等待 LRU cache 過期（最多 300 秒）或重啟 APISIX worker
# Step 4: 發送請求確認新 limit 生效

curl -X POST http://apisix:9080/ai/chat \
  -H "t-tenant-id: t-00000001" \
  ...

# 新的 X-AI-RateLimit-Limit-Tenant 應為 8000
```

> **注意：** LRU cache TTL 預設 300 秒，config 更新後最多 5 分鐘舊配額才完全失效。
> 若需要即時生效，可暫時將 `tenant_limit_conf_cache` 的 TTL 調低（如 30 秒），代價是 Redis 建立 tenant conf 的頻率上升。

---

### 6.3 Redis Key 驗證速查

Redis key 格式：`plugin-ai-rate-limiting-redis<conf_id>#<dimension>:<dimension>`

```bash
# ── 列出所有 rate limit keys（找出實際 conf_id）──────────────────────────
redis-cli KEYS "plugin-ai-rate-limiting-redis*"

# ── 列出所有 tenant counter ──────────────────────────────────────────────
redis-cli KEYS "plugin-ai-rate-limiting-redis*tenant#*"

# ── 查看特定 tenant 的剩餘配額與 TTL ─────────────────────────────────────
TENANT_KEY="plugin-ai-rate-limiting-redis<conf_id>#tenant#t-12345678:tenant#t-12345678"
redis-cli GET "$TENANT_KEY"
redis-cli TTL "$TENANT_KEY"

# ── 列出所有 instance counter ────────────────────────────────────────────
redis-cli KEYS "plugin-ai-rate-limiting-redis*#openai-primary*"
redis-cli KEYS "plugin-ai-rate-limiting-redis*#deepseek-*"

# ── 監控所有 Redis 操作（debug 用）───────────────────────────────────────
redis-cli MONITOR

# ── 清除所有 tenant counter（重置）──────────────────────────────────────
redis-cli KEYS "plugin-ai-rate-limiting-redis*tenant#*" | xargs redis-cli DEL

# ── 清除所有 rate limiting counter（完整重置）───────────────────────────
redis-cli KEYS "plugin-ai-rate-limiting-redis*" | xargs redis-cli DEL
```

---

## 7. 已知限制與注意事項

| 限制 | 說明 |
|---|---|
| LRU Cache 延遲 | `tenant_limit_conf_cache` TTL=300s，config 更新後配額設定最慢 5 分鐘後生效 |
| 無 Tenant Header = 不限 Tenant | 若需要強制要求所有請求帶 tenant header，需在其他地方（如 `serverless-pre-function`）做驗證 |
| Log Phase 非同步 | Access Phase 的 placeholder 與 Log Phase 的真實消耗之間有時間差；極短窗口內可能有微小的超限洩漏 |
| Tenant ID 信任 | 插件直接信任 `t-tenant-id` header 的值，建議搭配 JWT / API Key 等認證機制確保 tenant ID 不被偽造 |
| Redis 單點 | 若 Redis 故障，`allow_degradation: true`（預設）時 Fail-open 放行所有請求 |

---

## 8. 與 RFC-001 的 diff 摘要

```lua
-- 新增：tenant_limit_entry_schema
-- 新增：tenant_tpm_schema
-- 修改：schema.properties 加入 tenant_tpm 欄位

-- 新增：get_plugin_conf_id()   (取得穩定的 plugin conf 識別 ID)
-- 新增：copy_redis_conf()      (從 transform_limit_conf 抽取，消除重複)
-- 新增：build_tenant_limit_conf()
-- 新增：get_tenant_limit_conf()
-- 新增：tenant_limit_conf_cache (LRU, count=4096)

-- 修改：transform_limit_conf() — 加入 conf.group 修正 Redis key 建構
--       (limit-count gen_limit_key 需要 conf.group 或 conf._meta.parent.resource_key，
--        自建 limit_conf 無 _meta.parent，必須用 conf.group bypass)
-- 修改：build_tenant_limit_conf() — 同上，加入 conf.group
-- 修改：_M.access() — Step 2 tenant check
-- 修改：_M.log()    — timer 內增加 tenant counter 更新

-- 不變：_M.check_instance_status()
-- 不變：fetch_limit_conf_kvs()
-- 不變：get_token_usage()
```

---

## 9. 配置建議

### 大多數 Tenant 用 default，少數 Tenant 有 override（推薦）

```json
{
  "tenant_tpm": {
    "default": { "limit": 5000, "time_window": 60 },
    "overrides": {
      "t-vip-001": { "limit": 100000, "time_window": 60 },
      "t-trial-001": { "limit": 500, "time_window": 60 }
    }
  }
}
```

### 若 overrides 數量超過 LRU count (4096)

將 `tenant_limit_conf_cache` 的 `count` 調高：
```lua
local tenant_limit_conf_cache = core.lrucache.new({ ttl = 300, count = 16384 })
```

但需注意記憶體用量（每個 entry 約 1~2KB）。

---

## 10. 參考資料

- [RFC-001: ai-rate-limiting-redis](./rfc-ai-rate-limiting-redis.md)
- [APISIX limit-count plugin source](https://github.com/apache/apisix/blob/master/apisix/plugins/limit-count/init.lua)
- [APISIX lrucache API](https://github.com/apache/apisix/blob/master/apisix/core/lrucache.lua)
- [ngx.var.http_* 變數說明](https://nginx.org/en/docs/http/ngx_http_core_module.html#var_http_)
