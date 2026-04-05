# RFC: `ai-rate-limiting` — Per-Tenant TPM 擴充（覆蓋原生 plugin）

| 欄位 | 內容 |
|---|---|
| **RFC 編號** | RFC-002 |
| **標題** | Per-Tenant TPM Rate Limiting — Override of upstream ai-rate-limiting |
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

另一個問題：ai-proxy-multi 的 `fallback_strategy: ["rate_limiting"]` 硬編碼呼叫原生 `ai-rate-limiting` plugin，導致 `ai-rate-limiting-redis` 的 fallback 功能完全失效（詳見 §10）。本版本以「覆蓋原生 plugin」方式同時解決這兩個問題。

### 1.2 功能需求

| # | 需求 | 說明 |
|---|---|---|
| R1 | Tenant ID 來源 | 從 HTTP request header `t-tenant-id` 取得，格式如 `t-12345678` |
| R2 | 預設配額 | 大多數 Tenant 套用統一的 default TPM 上限 |
| R3 | 個別覆寫 | 特定 Tenant 可設定不同的 TPM 上限（高或低） |
| R4 | 獨立計數器 | 每個 (Instance, Tenant) 組合的 Redis counter 互不影響 |
| R5 | fallback 相容 | ai-proxy-multi `fallback_strategy: ["rate_limiting"]` 正常運作 |
| R6 | Instance TPM 選填 | 只設定 tenant_tpm 即可，不強制設定 instance-level 限流 |
| R7 | 向後相容 | 未設定 `tenant_tpm` 時行為與原生 plugin 完全一致 |
| R8 | 無 Tenant Header | Request 沒帶 `t-tenant-id` 時跳過 tenant 檢查 |

---

## 2. 架構設計

### 2.1 雙維度限流模型

```
每個 Request 需同時通過兩道限流閘：

┌──────────────────────────────────────────────────────────────────┐
│  Access Phase                                                    │
│                                                                  │
│  Step 1:  Instance 維度                                                          │
│           Redis Key: "<conf_id>#openai-primary:openai-primary"                   │
│           limit: 100,000 TPM (整個 instance 共享，所有 tenant 合計)               │
│                    ↓ pass                                                        │
│  Step 2:  Tenant × Instance 維度                                                 │
│           Redis Key: "<conf_id>#openai-primary#tenant#t-12345678:..."            │
│           limit:  10,000 TPM (此 tenant 在此 instance 的個人配額)                 │
│                    ↓ pass                                                        │
│           放行請求至上游                                                          │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  Log Phase (ngx.timer)                                           │
│                                                                  │
│  同時更新：                                                       │
│    ① Instance counter        INCRBY "<conf_id>#openai-primary:..."       (N-1)  │
│    ② Tenant×Instance counter INCRBY "<conf_id>#openai-primary#tenant#...:..." (N-1)  │
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
  key = "plugin-ai-rate-limiting<conf_id>#<instance_name>:<instance_name>"
  ex:   "plugin-ai-rate-limitingroute-abc#openai-primary:openai-primary"

Tenant × Instance 維度（新增）:
  key = "plugin-ai-rate-limiting<conf_id>#<instance_name>#tenant#<tenant_id>:<instance_name>#tenant#<tenant_id>"
  ex:   "plugin-ai-rate-limitingroute-abc#openai-primary#tenant#t-12345678:openai-primary#tenant#t-12345678"
  ex:   "plugin-ai-rate-limitingroute-abc#deepseek-backup#tenant#t-12345678:deepseek-backup#tenant#t-12345678"
```

其中 `<conf_id>` = `plugin_conf._meta.id`（APISIX route plugin config ID，跨重啟穩定）。

**每個 (instance, tenant) 組合各有獨立計數器**：同一個 tenant 在 openai-primary 和 deepseek-backup 上的配額互不影響。

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
               │  group = "<conf_id>#openai-primary#tenant#t-12345678" │
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
               │  INCRBY "<conf_id>#openai-primary:openai-primary"                 +846  │
               │  INCRBY "<conf_id>#openai-primary#tenant#t-12345678:..."      +846  │
               └──────────────────────────────────┘
```

---

## 3. Schema 變更

### 3.1 新增欄位：`tenant_tpm`

`default` 與 `overrides` 的值都是 **陣列**，每個元素包含 `name`（對應 AI instance 名稱）、`limit`、`time_window`，讓不同 instance 可以設定不同的 tenant TPM 配額。

```jsonc
{
  "tenant_tpm": {
    // 必填：對每個 AI instance 設定每個 tenant 的預設 TPM 上限
    "default": [
      { "name": "openai-primary",  "limit": 10000, "time_window": 60 },
      { "name": "deepseek-backup", "limit": 5000,  "time_window": 60 }
    ],
    // 選填：特定 tenant 的個別設定（覆蓋 default）
    "overrides": {
      "t-12345678": [              // VIP tenant，更高配額
        { "name": "openai-primary",  "limit": 50000, "time_window": 60 },
        { "name": "deepseek-backup", "limit": 20000, "time_window": 60 }
      ],
      "t-99999999": [              // 受限 tenant，更低配額
        { "name": "openai-primary", "limit": 1000, "time_window": 60 }
        // deepseek-backup 未設定 → 沿用 default[deepseek-backup] = 5000
      ]
    }
  }
}
```

### 3.2 配額查找優先序

對每個 **(instance_name, tenant_id)** 組合，lookup 順序：

```
instance_name = "openai-primary", tenant_id = "t-12345678"

1. overrides["t-12345678"] 陣列中找 name == "openai-primary"  → 找到 → 用 50,000 TPM
2. overrides["t-unknown"]  陣列中找 name == "openai-primary"  → overrides 無此 key
   → default 陣列中找 name == "openai-primary"                → 找到 → 用 10,000 TPM
3. instance_name 在 default/override 陣列中都找不到            → 跳過 tenant 限流
4. 無 t-tenant-id header                                      → 跳過 tenant 檢查
```

### 3.3 完整 Schema 範例

```json
{
  "instances": [
    { "name": "openai-primary",  "limit": 100000, "time_window": 60 },
    { "name": "deepseek-backup", "limit": 50000,  "time_window": 60 }
  ],
  "tenant_tpm": {
    "default": [
      { "name": "openai-primary",  "limit": 10000, "time_window": 60 },
      { "name": "deepseek-backup", "limit": 5000,  "time_window": 60 }
    ],
    "overrides": {
      "t-00000001": [
        { "name": "openai-primary",  "limit": 50000, "time_window": 60 },
        { "name": "deepseek-backup", "limit": 20000, "time_window": 60 }
      ],
      "t-00000002": [
        { "name": "openai-primary", "limit": 200, "time_window": 60 }
      ]
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

tenant cache 使用字串 key `"<conf_id>#<instance_name>#<tenant_id>"` 是因為這三個維度都來自 runtime，無法以 conf table identity 作為 key。
count=4096 可容納 4096 個不同的 (config, instance, tenant) 組合，應足以應對大多數生產場景。

### 4.3 Tenant 不存在時的行為

| 情境 | 行為 |
|---|---|
| Request 沒有 `t-tenant-id` header | 跳過 tenant 檢查，只做 instance 限流 |
| `tenant_tpm` 沒有設定 | 跳過 tenant 檢查（完全向後相容） |
| Tenant ID 不在 overrides 中 | 在 `default` 陣列中查找對應 instance entry |
| Instance name 在 default/override 陣列中都找不到 | 跳過此 (instance, tenant) 的 tenant 限流 |
| Redis 連線失敗（`allow_degradation: true`） | Fail-open，允許通過，記錄 error log |

### 4.4 Streaming API 支援

**結論：Streaming 模式無需額外修改，開箱即用。**

APISIX 3.15 在 `before_proxy` 階段偵測到 `request.stream == true` 時，會**自動注入** `stream_options: { include_usage: true }` 到送往 LLM 的請求：

```lua
-- apisix/plugins/ai-proxy/base.lua（APISIX 內建邏輯，非本 plugin）
if request_body.stream then
    request_body.stream_options = { include_usage = true }
    ctx.var.request_type = "ai_stream"
end
```

SSE chunk 解析器（`openai-base.lua`）在讀取最後一個含 `usage` 的 chunk 時，設定：

```lua
ctx.ai_token_usage = {
    prompt_tokens     = data.usage.prompt_tokens     or 0,
    completion_tokens = data.usage.completion_tokens or 0,
    total_tokens      = data.usage.total_tokens      or 0,
}
```

到 `_M.log` 執行時，`ctx.ai_token_usage` 已由 APISIX 填好，與非串流請求路徑完全一致。

#### 串流 vs 非串流行為比較

| 面向 | 非串流 | 串流 SSE |
|---|---|---|
| `ctx.ai_token_usage` 設定時機 | 完整 response body 解析後 | 最後一個含 usage 的 SSE chunk 解析後 |
| `stream_options` | N/A | APISIX 自動注入 `include_usage: true` |
| Access Phase（placeholder +1） | 正常 | 正常（串流開始前即扣） |
| Log Phase（actual - 1 調整） | 正常 | 正常（串流結束、log 觸發時執行） |
| Tenant counter 更新 | 正常 | 正常 |

#### 邊緣情況：Provider 不支援 streaming usage

若 LLM provider 不回傳 `usage` 欄位（未遵循 `stream_options`），`ctx.ai_token_usage` 為 nil，log phase 會：

1. 記錄 error log：`"failed to get token usage for llm service"`
2. 提前 return，不執行 extra_tokens 調整
3. Access phase 預扣的 `+1` placeholder **不會被校正** → counter 每次串流請求偏移 `+1`

此行為與上游 `ai-rate-limiting` plugin 一致，並非本 plugin 引入的問題。

---

## 5. APISIX Plugin 覆蓋機制

### 5.1 為什麼要覆蓋而不是新增

`ai-proxy-multi` 的 fallback 邏輯在 `pick_target()` 中硬編碼：

```lua
-- ai-proxy-multi.lua（APISIX 原生，不可動）
local ai_rate_limiting = require("apisix.plugins.ai-rate-limiting")
ai_rate_limiting.check_instance_status(nil, ctx, instance_name)
```

這行 `require` 永遠載入名為 `ai-rate-limiting` 的 Lua 模組。若自訂 plugin 命名為 `ai-rate-limiting-redis`，fallback 永遠找不到正確的 plugin。
**解法**：讓自訂 plugin 的檔名與 `plugin_name` 都叫 `ai-rate-limiting`，透過 Lua 的 `package.path` 優先機制覆蓋原生版本。

### 5.2 APISIX 的 Plugin 載入順序

APISIX 啟動時透過 `require("apisix.plugins.<name>")` 載入 plugin。Lua 的 `require` 依 `package.path` 的順序搜尋，**找到第一個符合的檔案即停止**。

```
package.path 搜尋順序（簡化）：
  1. extra_lua_path（自訂路徑，優先）
  2. APISIX 預設路徑（/usr/local/apisix/apisix/...）
```

若自訂路徑排在前面，同名檔案會覆蓋原生版本。

### 5.3 部署步驟

#### Step 1 — 準備自訂 plugin 目錄

```bash
mkdir -p /opt/apisix-custom/apisix/plugins
cp ai-rate-limiting.lua /opt/apisix-custom/apisix/plugins/ai-rate-limiting.lua
```

#### Step 2 — 修改 `config.yaml`

```yaml
# /usr/local/apisix/conf/config.yaml
apisix:
  # 自訂路徑排在 ;; （代表預設路徑）之前，確保優先被搜尋到
  extra_lua_path: "/opt/apisix-custom/?.lua;;"
```

> `extra_lua_path` 中的 `?` 是 Lua 慣例，會被替換成模組路徑。
> 例如 `require("apisix.plugins.ai-rate-limiting")` 對應到
> `/opt/apisix-custom/apisix/plugins/ai-rate-limiting.lua`。

#### Step 3 — 確認 plugin 已啟用（config.yaml 的 plugins 列表）

```yaml
plugins:
  - ai-rate-limiting    # 名稱與原生 plugin 相同，無需額外新增
  - ai-proxy-multi
  # ...其他 plugins
```

#### Step 4 — 重啟 APISIX

```bash
apisix reload   # 或 apisix restart
```

#### Step 5 — 驗證覆蓋生效

```bash
# 查看 APISIX error.log，確認載入的是自訂版本
grep "ai-rate-limiting" /usr/local/apisix/logs/error.log

# 或透過 Admin API 查看 plugin schema，確認 tenant_tpm 欄位出現
curl http://127.0.0.1:9180/apisix/admin/schema/plugins/ai-rate-limiting \
  -H "X-API-KEY: $APISIX_ADMIN_KEY" | jq '.properties.tenant_tpm'
# 若回傳非 null，表示自訂版本已生效
```

### 5.4 Docker / Kubernetes 部署

```yaml
# docker-compose.yaml 範例
services:
  apisix:
    image: apache/apisix:3.15.0
    volumes:
      - ./ai-rate-limiting.lua:/opt/apisix-custom/apisix/plugins/ai-rate-limiting.lua
      - ./config.yaml:/usr/local/apisix/conf/config.yaml
```

```yaml
# Kubernetes ConfigMap 範例
apiVersion: v1
kind: ConfigMap
metadata:
  name: apisix-custom-plugins
data:
  ai-rate-limiting.lua: |
    <plugin 內容>
---
# 掛載到 Pod
volumeMounts:
  - name: custom-plugins
    mountPath: /opt/apisix-custom/apisix/plugins
```

---

## 6. limit-count 內部機制與 INCRBY 分析

### 6.1 limit-count 的核心：固定時間窗口計數器

APISIX `limit-count` 使用 **固定時間窗口（Fixed Window）** 算法，Redis 儲存每個窗口的**已消耗量（consumed amount）**：

```
初始狀態：key 不存在（或 = 0）
每次請求：INCRBY key cost  →  若新值 > limit  →  拒絕
窗口結束：TTL 到期，key 自動消失，新窗口從 0 開始
```

### 6.2 Redis Script（偽碼）

```lua
-- limit-count-redis.lua 底層執行的 Redis 操作（簡化）
local current = redis.call("INCRBY", key, cost)     -- 累加消耗量
if current == cost then
    -- 第一次寫入，設定 TTL（time_window 秒）
    redis.call("EXPIRE", key, time_window)
end
if current > limit then
    return {nil, "rejected", ttl}                   -- 超限
end
local remaining = limit - current
return {0, remaining, ttl}                           -- 通過，回傳剩餘量
```

### 6.3 Redis 存的是「已消耗量」還是「剩餘量」？

**結論：Redis 存的是已消耗量（consumed），剩餘量由 Lua 計算得出。**

```
Redis 儲存：consumed（從 0 開始遞增）
判斷邏輯：if consumed > limit → rejected
回傳剩餘：remaining = limit - consumed
```

**為什麼存已消耗量更合理：**

| | 存已消耗量（現行）| 存剩餘量 |
|---|---|---|
| 初始值 | 0（key 不存在）| limit（需要初始化）|
| 每次操作 | `INCRBY key cost`（原子，簡單）| `DECRBY key cost`（需先確保 key 存在）|
| 原子性 | ✓ INCRBY 天然原子 | ✗ 初始化 + DECRBY 需要 Lua script 保護 |
| TTL 設定 | 第一次 INCRBY 後設 EXPIRE | 需在 key 不存在時先 SET limit，再設 EXPIRE |
| 判斷超限 | `if value > limit` | `if value < 0` |

存已消耗量的做法讓 Redis 操作更簡單，也讓 `INCRBY 0`（cost=0）成為天然的純讀操作。

### 6.4 cost=0 的特殊意義

```
INCRBY key 0
→ 不改變 consumed 值
→ Redis 回傳當前 consumed 值
→ Lua: if consumed > limit → rejected (already exhausted)
→ 等效於：「在不消耗任何配額的情況下，查看當前是否還有餘量」
```

這正是本 plugin access phase 與 `check_instance_status` 的實作原理——純讀，access phase 完全不寫入 Redis，讓計數準確反映真實 token 消耗。

### 6.5 完整計數流程

```
時間窗口 60 秒，limit = 10,000 tokens

Request 1（實際消耗 500 tokens）：
  Access phase:  INCRBY key 0   → consumed = 0   → pass（0 < 10000）
  Log phase:     INCRBY key 500 → consumed = 500

Request 2（實際消耗 800 tokens）：
  Access phase:  INCRBY key 0   → consumed = 500 → pass（500 < 10000）
  Log phase:     INCRBY key 800 → consumed = 1300

... 累積到 consumed = 9800 ...

Request N（實際消耗 300 tokens）：
  Access phase:  INCRBY key 0   → consumed = 9800 → pass（9800 < 10000）
  Log phase:     INCRBY key 300 → consumed = 10100

Request N+1：
  Access phase:  INCRBY key 0   → consumed = 10100 → REJECTED（10100 > 10000）
  （log phase 不執行，因為 ctx.ai_rate_limiting = true）

TTL 到期 → key 消失 → 新窗口從 0 開始
```

---

## 7. Response Headers

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

## 8. 測試計畫

### 6.1 測試環境準備

```bash
# 1. 啟動 Redis（local）
docker run -d --name redis-test -p 6379:6379 redis:7-alpine

# 2. 確認 Redis 連線
redis-cli ping  # PONG

# 3. 查詢當前所有 rate limit keys（找出 conf_id）
redis-cli KEYS "plugin-ai-rate-limiting*"
# ex output: "plugin-ai-rate-limitingroute-abc123#openai-primary:openai-primary"
# conf_id = "route-abc123" (字串中第一個 # 前的部分)

# 4. 清除測試 Key（每個測試開始前執行）
redis-cli KEYS "plugin-ai-rate-limiting*" | xargs redis-cli DEL
```

> **注意：** Redis key 包含 `conf_id`（APISIX route plugin config ID）。實際值可用 `redis-cli KEYS "plugin-ai-rate-limiting*"` 查詢，或從 APISIX admin API 的路由設定中確認。
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
        "default": [
          { "name": "openai-primary", "limit": 1000, "time_window": 60 }
        ],
        "overrides": {
          "t-00000001": [
            { "name": "openai-primary", "limit": 5000, "time_window": 60 }
          ],
          "t-00000002": [
            { "name": "openai-primary", "limit": 200, "time_window": 60 }
          ]
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
redis-cli KEYS "plugin-ai-rate-limiting*tenant*"  # 應為空
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
redis-cli KEYS "plugin-ai-rate-limiting*openai-primary#tenant#t-99999999*"
# ex: "plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-99999999:openai-primary#tenant#t-99999999"

# 確認 TTL = 60 秒、餘額為 999（placeholder -1）
redis-cli TTL  "plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-99999999:openai-primary#tenant#t-99999999"  # 接近 60
redis-cli GET  "plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-99999999:openai-primary#tenant#t-99999999"  # 999
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
redis-cli GET "plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-00000001:openai-primary#tenant#t-00000001"  # 應為 4999
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
TENANT_KEY="plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-00000002:openai-primary#tenant#t-00000002"
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
INST_KEY="plugin-ai-rate-limiting<conf_id>#openai-primary:openai-primary"
TENANT_KEY="plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-00000002:openai-primary#tenant#t-00000002"
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
KEY_A="plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-00000001:openai-primary#tenant#t-00000001"
KEY_B="plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-99999999:openai-primary#tenant#t-99999999"

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
INST_KEY="plugin-ai-rate-limiting<conf_id>#openai-primary:openai-primary"
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
TENANT_KEY="plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-00000001:openai-primary#tenant#t-00000001"
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
TENANT_KEY = "plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-99999999:openai-primary#tenant#t-99999999"

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
TENANT_KEY="plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-99999999:openai-primary#tenant#t-99999999"
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
KEY_A="plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-00000001:openai-primary#tenant#t-00000001"
KEY_B="plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-99999999:openai-primary#tenant#t-99999999"
KEY_I="plugin-ai-rate-limiting<conf_id>#openai-primary:openai-primary"

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

```
Key 格式：
  Instance:        plugin-ai-rate-limiting<conf_id>#<instance>:<instance>
  Tenant×Instance: plugin-ai-rate-limiting<conf_id>#<instance>#tenant#<tid>:<instance>#tenant#<tid>
```

```bash
# ── 列出所有 rate limit keys（找出實際 conf_id）──────────────────────────
redis-cli KEYS "plugin-ai-rate-limiting*"

# ── 列出所有 tenant×instance counter ────────────────────────────────────
redis-cli KEYS "plugin-ai-rate-limiting*#tenant#*"

# ── 查看特定 (instance, tenant) 的剩餘配額與 TTL ─────────────────────────
TKEY="plugin-ai-rate-limiting<conf_id>#openai-primary#tenant#t-12345678:openai-primary#tenant#t-12345678"
redis-cli GET "$TKEY"
redis-cli TTL "$TKEY"

# ── 列出特定 instance 的所有 tenant counter ──────────────────────────────
redis-cli KEYS "plugin-ai-rate-limiting*#openai-primary#tenant#*"
redis-cli KEYS "plugin-ai-rate-limiting*#deepseek-backup#tenant#*"

# ── 列出 instance 本身的 counter（排除 tenant）───────────────────────────
redis-cli KEYS "plugin-ai-rate-limiting*#openai-primary:openai-primary"

# ── 監控所有 Redis 操作（debug 用）───────────────────────────────────────
redis-cli MONITOR

# ── 清除所有 tenant counter（重置）──────────────────────────────────────
redis-cli KEYS "plugin-ai-rate-limiting*#tenant#*" | xargs redis-cli DEL

# ── 清除所有 rate limiting counter（完整重置）───────────────────────────
redis-cli KEYS "plugin-ai-rate-limiting*" | xargs redis-cli DEL
```

---

### 8.7 Fallback 測試（ai-proxy-multi 整合）

這組測試驗證 `fallback_strategy: ["rate_limiting"]` 能在 TPM 耗盡時正確切換 instance，以及 priority 降級邏輯。

#### 路由設定（Fallback 測試專用）

```json
{
  "uri": "/ai/chat",
  "plugins": {
    "ai-proxy-multi": {
      "instances": [
        {
          "name": "openai-p1-a",
          "provider": "openai",
          "weight": 100,
          "priority": 10,
          "auth": { "header": { "Authorization": "Bearer $KEY" } },
          "options": { "model": "gpt-4o-mini" }
        },
        {
          "name": "openai-p1-b",
          "provider": "openai",
          "weight": 100,
          "priority": 10,
          "auth": { "header": { "Authorization": "Bearer $KEY" } },
          "options": { "model": "gpt-4o-mini" }
        },
        {
          "name": "deepseek-p2",
          "provider": "deepseek",
          "weight": 100,
          "priority": 5,
          "auth": { "header": { "Authorization": "Bearer $KEY" } },
          "options": { "model": "deepseek-chat" }
        }
      ],
      "fallback_strategy": ["rate_limiting"],
      "balancer": { "algorithm": "roundrobin" }
    },
    "ai-rate-limiting": {
      "instances": [
        { "name": "openai-p1-a", "limit": 5000,  "time_window": 60 },
        { "name": "openai-p1-b", "limit": 5000,  "time_window": 60 },
        { "name": "deepseek-p2", "limit": 10000, "time_window": 60 }
      ],
      "limit_strategy": "total_tokens",
      "rejected_code":  429,
      "policy":         "redis",
      "redis_host":     "127.0.0.1",
      "redis_port":     6379
    }
  }
}
```

#### TC-F01: 同 Priority 內 Fallback（A 滿 → 轉 B）

**目的：** `openai-p1-a` TPM 耗盡時，請求自動轉到同 priority 的 `openai-p1-b`。

```bash
# 把 openai-p1-a 的計數器設滿（模擬耗盡）
KEY_A="plugin-ai-rate-limiting<conf_id>#openai-p1-a:openai-p1-a"
redis-cli SET "$KEY_A" 5001 EX 60

# 發送請求（ai-proxy-multi 選到 openai-p1-a → check_instance_status → false → 切換）
curl -v -X POST http://apisix:9080/ai/chat \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hi"}]}'
```

**預期：**
- HTTP 200（非 429）
- Response header 含 `X-AI-RateLimit-Limit-openai-p1-b`（表示打到了 B）
- `openai-p1-a` 計數器不再增加（check_instance_status cost=0，無寫入）

**驗證：**
```bash
KEY_B="plugin-ai-rate-limiting<conf_id>#openai-p1-b:openai-p1-b"
redis-cli GET "$KEY_A"  # 仍為 5001（cost=0，check 無副作用）
redis-cli GET "$KEY_B"  # 有消耗，log phase 寫入
```

---

#### TC-F02: 同 Priority 全滿 → 降級到低 Priority

**目的：** priority=10 的 A 和 B 都耗盡時，自動降級到 priority=5 的 `deepseek-p2`。

```bash
# 把 priority=10 的所有 instance 設滿
KEY_A="plugin-ai-rate-limiting<conf_id>#openai-p1-a:openai-p1-a"
KEY_B="plugin-ai-rate-limiting<conf_id>#openai-p1-b:openai-p1-b"
redis-cli SET "$KEY_A" 5001 EX 60
redis-cli SET "$KEY_B" 5001 EX 60

curl -v -X POST http://apisix:9080/ai/chat \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hi"}]}'
```

**預期：**
- HTTP 200
- Response 含 `X-AI-RateLimit-Limit-deepseek-p2`（打到 deepseek，priority 降級成功）

**驗證：**
```bash
KEY_P2="plugin-ai-rate-limiting<conf_id>#deepseek-p2:deepseek-p2"
redis-cli GET "$KEY_P2"  # 有消耗（log phase 寫入）
```

---

#### TC-F03: 所有 Instance 全滿 → 返回 429

**目的：** 所有 instance 都耗盡時，返回 rejected_code。

```bash
KEY_A="plugin-ai-rate-limiting<conf_id>#openai-p1-a:openai-p1-a"
KEY_B="plugin-ai-rate-limiting<conf_id>#openai-p1-b:openai-p1-b"
KEY_P2="plugin-ai-rate-limiting<conf_id>#deepseek-p2:deepseek-p2"
redis-cli SET "$KEY_A"  5001  EX 60
redis-cli SET "$KEY_B"  5001  EX 60
redis-cli SET "$KEY_P2" 10001 EX 60

curl -v -X POST http://apisix:9080/ai/chat \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hi"}]}'
```

**預期：** HTTP 429（所有 instance 皆 `check_instance_status` → false，loop 結束後仍拒絕）

---

#### TC-F04: 僅設定 tenant_tpm（無 instance-level limits）

**目的：** 驗證 instance-level TPM 為 optional，只設定 tenant_tpm 亦可正常限流。

路由設定中的 `ai-rate-limiting` 僅含 tenant_tpm，無 `instances` 欄位：

```json
{
  "ai-rate-limiting": {
    "tenant_tpm": {
      "default": [
        { "name": "openai-p1-a", "limit": 1000, "time_window": 60 }
      ]
    },
    "limit_strategy": "total_tokens",
    "policy": "redis",
    "redis_host": "127.0.0.1",
    "redis_port": 6379
  }
}
```

```bash
# 把 tenant counter 設滿
TKEY="plugin-ai-rate-limiting<conf_id>#openai-p1-a#tenant#t-00000001:openai-p1-a#tenant#t-00000001"
redis-cli SET "$TKEY" 1001 EX 60

curl -v -X POST http://apisix:9080/ai/chat \
  -H "t-tenant-id: t-00000001" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hi"}]}'
```

**預期：**
- HTTP 429（tenant TPM 超限）
- 無 `X-AI-RateLimit-Limit-openai-p1-a` header（instance-level 未設定，Step 1 跳過）
- 有 `X-AI-RateLimit-Limit-Tenant: 1000` header

---

#### TC-F05: check_instance_status cost=0 不增加計數器（副作用驗證）

**目的：** 確認 fallback 探測時不會對 Redis counter 造成副作用。

```bash
redis-cli KEYS "plugin-ai-rate-limiting*" | xargs redis-cli DEL  # 清空

# 第一次請求（ai-proxy-multi 呼叫 check_instance_status 一次，然後放行）
curl -X POST http://apisix:9080/ai/chat \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hi"}]}'

# 在 log phase timer 執行前立刻查詢（約 10ms 內）
KEY="plugin-ai-rate-limiting<conf_id>#openai-p1-a:openai-p1-a"
redis-cli GET "$KEY"
```

**預期：**
- 請求放行瞬間：`GET KEY` 回傳 `nil` 或 `0`（access phase 的 cost=0 無寫入）
- log phase 執行後（約 100ms）：`GET KEY` 回傳實際 token 數（e.g. `847`）
- **不應出現** `1`（代表舊設計的 +1 placeholder 殘留）

---

## 9. 已知限制與注意事項

| 限制 | 說明 |
|---|---|
| LRU Cache 延遲 | `tenant_limit_conf_cache` TTL=300s，config 更新後配額設定最慢 5 分鐘後生效 |
| 無 Tenant Header = 不限 Tenant | 若需要強制要求所有請求帶 tenant header，需在其他地方（如 `serverless-pre-function`）做驗證 |
| Log Phase 非同步 | Access Phase 的 placeholder 與 Log Phase 的真實消耗之間有時間差；極短窗口內可能有微小的超限洩漏 |
| Tenant ID 信任 | 插件直接信任 `t-tenant-id` header 的值，建議搭配 JWT / API Key 等認證機制確保 tenant ID 不被偽造 |
| Redis 單點 | 若 Redis 故障，`allow_degradation: true`（預設）時 Fail-open 放行所有請求 |

---

## 10. 與 RFC-001 的 diff 摘要

```lua
-- v1 → v2 新增 / 修改：
-- 新增：tenant_limit_entry_schema  (含 name, limit, time_window)
-- 新增：tenant_limit_list_schema   (陣列型別)
-- 新增：tenant_tpm_schema
-- 修改：schema.properties 加入 tenant_tpm 欄位

-- 新增：get_plugin_conf_id()              (取得穩定的 plugin conf 識別 ID)
-- 新增：copy_redis_conf()                 (從 transform_limit_conf 抽取，消除重複)
-- 新增：find_instance_limit_in_list()     (在陣列中依 name 查找 entry)
-- 新增：build_tenant_limit_conf()
-- 新增：get_tenant_limit_conf()
-- 新增：tenant_limit_conf_cache (LRU, count=4096)

-- 修改：transform_limit_conf() — 加入 conf.group 修正 Redis key 建構
--       (limit-count gen_limit_key 需要 conf.group 或 conf._meta.parent.resource_key，
--        自建 limit_conf 無 _meta.parent，必須用 conf.group bypass)
-- 修改：build_tenant_limit_conf() — 陣列 lookup；找不到 instance entry 時回傳 nil
-- 修改：_M.access() — Step 2 tenant check（nil conf 時跳過）
-- 修改：_M.log()    — timer 內增加 tenant counter 更新（nil conf 時跳過）

-- 不變：_M.check_instance_status()
-- 不變：fetch_limit_conf_kvs()
-- 不變：get_token_usage()
```

---

## 11. 配置建議

### 大多數 Tenant 用 default，少數 Tenant 有 override（推薦）

```json
{
  "tenant_tpm": {
    "default": [
      { "name": "openai-primary",  "limit": 5000,  "time_window": 60 },
      { "name": "deepseek-backup", "limit": 2000,  "time_window": 60 }
    ],
    "overrides": {
      "t-vip-001": [
        { "name": "openai-primary",  "limit": 100000, "time_window": 60 },
        { "name": "deepseek-backup", "limit": 50000,  "time_window": 60 }
      ],
      "t-trial-001": [
        { "name": "openai-primary", "limit": 500, "time_window": 60 }
      ]
    }
  }
}
```

> **注意：** override 陣列中未列出的 instance，會 fallback 到 `default` 陣列中對應的 entry。
> 若 `default` 陣列中也找不到，則對該 (instance, tenant) 組合**跳過** tenant 限流（不報錯）。

### 若 overrides 數量超過 LRU count (4096)

將 `tenant_limit_conf_cache` 的 `count` 調高：
```lua
local tenant_limit_conf_cache = core.lrucache.new({ ttl = 300, count = 16384 })
```

但需注意記憶體用量（每個 entry 約 1~2KB）。

---

## 12. `fallback_strategy: ["rate_limiting"]` 與 `ai-proxy-multi` 整合問題

### 10.1 問題現象

配置 `ai-proxy-multi` 的 `fallback_strategy: ["rate_limiting"]` 搭配 `ai-rate-limiting-redis` 後，Instance A 的 TPM 耗盡時不會自動 fallback 到 Instance B，所有請求仍然打到已超限的 instance。

### 10.2 根本原因

`ai-proxy-multi.lua`（APISIX 3.15 原生）的 `pick_target()` 函數**硬編碼**載入 `ai-rate-limiting`：

```lua
-- ai-proxy-multi.lua ~line 376 (原始碼)
local ai_rate_limiting = require("apisix.plugins.ai-rate-limiting")
...
for _ = 1, #conf.instances do
    if ai_rate_limiting.check_instance_status(nil, ctx, instance_name) then
        break
    end
    ...
end
```

`check_instance_status(nil, ctx, instance_name)` 收到 `conf = nil` 時，會自行到 `ctx.plugins` 搜尋 `name == "ai-rate-limiting"` 的 plugin config：

```lua
-- ai-rate-limiting.lua check_instance_status 的 conf 探測邏輯
if conf == nil then
    for i = 1, #ctx.plugins, 2 do
        if ctx.plugins[i]["name"] == plugin_name then  -- "ai-rate-limiting"
            conf = ctx.plugins[i + 1]
        end
    end
end
if not conf then
    return true   -- ← 找不到 → 永遠回傳 "可用"
end
```

| 步驟 | 搜尋的名稱 | 實際配置的名稱 | 結果 |
|---|---|---|---|
| require | `"ai-rate-limiting"` | — | 載入**原生** plugin 模組 |
| conf 探測 | `"ai-rate-limiting"` | `"ai-rate-limiting-redis"` | **找不到** conf |
| check 結果 | — | — | 永遠 `true`（available）|
| fallback | — | — | **永遠不觸發** |

### 10.3 修正方式

**只需修改 `ai-proxy-multi.lua` 一處**，將硬編碼改為動態查找當前 route 實際配置的 rate-limiting plugin，並直接傳入 conf（避免函數內二次搜尋）：

**diff** (`apisix/plugins/ai-proxy-multi.lua`)：
```diff
     if conf.fallback_strategy == "instance_health_and_rate_limiting" or
        fallback_strategy_has(conf.fallback_strategy, "rate_limiting") then
-        local ai_rate_limiting = require("apisix.plugins.ai-rate-limiting")
+        -- Dynamically discover which rate-limiting plugin is active for this route.
+        -- Supports "ai-rate-limiting" (built-in) and custom variants such as
+        -- "ai-rate-limiting-redis". Passing conf directly avoids a redundant
+        -- ctx.plugins scan inside check_instance_status.
+        local rl_mod, rl_conf
+        local route_plugins = ctx.plugins
+        local rl_candidates = { "ai-rate-limiting-redis", "ai-rate-limiting" }
+        for _, pname in ipairs(rl_candidates) do
+            for i = 1, #route_plugins, 2 do
+                if route_plugins[i]["name"] == pname then
+                    local ok, mod = pcall(require, "apisix.plugins." .. pname)
+                    if ok and mod.check_instance_status then
+                        rl_mod  = mod
+                        rl_conf = route_plugins[i + 1]
+                        break
+                    end
+                end
+            end
+            if rl_mod then break end
+        end
+        -- Fallback: load base plugin and pass nil conf (original behaviour)
+        if not rl_mod then
+            rl_mod = require("apisix.plugins.ai-rate-limiting")
+        end
         for _ = 1, #conf.instances do
-            if ai_rate_limiting.check_instance_status(nil, ctx, instance_name) then
+            if rl_mod.check_instance_status(rl_conf, ctx, instance_name) then
                 break
             end
```

patch 檔案位置：`ai-proxy-multi.patch`

### 10.4 修正後的完整 Fallback 流程

```
Request 進入 pick_target()
        │
        ├─ server_picker.get(ctx)  → instance_name = "openai-primary"
        │
        │  [修正後的 rl_mod / rl_conf 查找]
        │  rl_candidates = { "ai-rate-limiting-redis", "ai-rate-limiting" }
        │  → 找到 "ai-rate-limiting-redis" in ctx.plugins → rl_conf = 其設定
        │
        ├─ Loop（最多 #instances 次）:
        │   │
        │   ├─ rl_mod.check_instance_status(rl_conf, ctx, "openai-primary")
        │   │    → conf 已知（不需 nil 探測）
        │   │    → limit_conf_kvs["openai-primary"].count 耗盡 → return false
        │   │
        │   ├─ server_picker.after_balance(ctx, true)  ← 標記 openai-primary 失敗
        │   ├─ server_picker.get(ctx)  → 同 priority 下一個 instance
        │   │
        │   ├─ rl_mod.check_instance_status(rl_conf, ctx, "openai-secondary")
        │   │    → 有餘量 → return true → break
        │   │
        │
        └─ return "openai-secondary", its_conf
```

**Priority 降級**：當同一 priority group 內所有 instance 都回傳 `false`（TPM 耗盡）時，`server_picker.get()` 會自動切換到下一個 priority group（APISIX `priority_balancer` 的內建行為），繼續做 `check_instance_status` 驗證，直到找到可用 instance 或超過 `#conf.instances` 次。

### 10.5 `ai-rate-limiting-redis.lua` 是否需要修改？

**不需要**。`check_instance_status` 的邏輯本身正確：
- 修正後 `rl_conf` 直接傳入，跳過 `ctx.plugins` 搜尋
- `limit_count.rate_limit(..., 1, true)` 做 placeholder +1；rate-limited 時立即 restore -1
- 函數完全相容 `ai-proxy-multi` 的呼叫規約

### 10.6 測試驗證

```bash
# 把 openai-primary 的 TPM counter 設滿（模擬耗盡）
INST_KEY="plugin-ai-rate-limiting<conf_id>#openai-primary:openai-primary"
redis-cli SET "$INST_KEY" 0 EX 60

# 發送請求，應自動 fallback 到 openai-secondary（或下一 priority instance）
curl -v -X POST http://apisix:9080/ai/chat \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hi"}]}'

# 預期：
# - HTTP 200（非 429）
# - X-AI-RateLimit-Limit-openai-secondary: <limit>  （表示打到了 secondary）
# - openai-primary counter 未增加（check_instance_status 發現耗盡後 restore -1）

# 驗證 openai-primary 未被額外消耗
redis-cli GET "$INST_KEY"   # 仍為 0（check restore 後無淨消耗）

# 驗證 secondary 有消耗
SEC_KEY="plugin-ai-rate-limiting<conf_id>#openai-secondary:openai-secondary"
redis-cli GET "$SEC_KEY"    # 應有 placeholder -1，log phase 後為 (limit - actual_tokens)
```

---

## 13. 參考資料

- [RFC-001: ai-rate-limiting-redis](./rfc-ai-rate-limiting-redis.md)
- [APISIX limit-count plugin source](https://github.com/apache/apisix/blob/master/apisix/plugins/limit-count/init.lua)
- [APISIX lrucache API](https://github.com/apache/apisix/blob/master/apisix/core/lrucache.lua)
- [ngx.var.http_* 變數說明](https://nginx.org/en/docs/http/ngx_http_core_module.html#var_http_)
- [ai-proxy-multi fallback patch](./ai-proxy-multi.patch)
