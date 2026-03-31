# RFC: `ai-rate-limiting-redis` Plugin

| 欄位 | 內容 |
|---|---|
| **RFC 編號** | RFC-001 |
| **標題** | ai-rate-limiting-redis — Redis-backed AI Token Rate Limiting Plugin |
| **作者** | — |
| **狀態** | Draft |
| **建立日期** | 2026-03-31 |
| **更新日期** | 2026-03-31 |
| **相關插件** | `ai-rate-limiting`, `ai-proxy`, `ai-proxy-multi` |

---

## 1. 背景與動機 (Background & Motivation)

### 1.1 現有問題

Apache APISIX 的 `ai-rate-limiting` 插件以 **Local Memory（本地記憶體）** 儲存 Token 計數器。在單節點環境下運作正常，但在**多節點叢集部署**時存在根本性缺陷：

```
實際全域消耗 = Node1 Counter + Node2 Counter + ... + NodeN Counter
```

每個 APISIX 節點各自維護獨立的計數器，互不共享，導致：

- **超限保護失效**：N 個節點時，真實消耗可高達 `limit × N` tokens
- **限流精確度低**：同一 Consumer 打到不同節點有不同的配額體驗
- **無法做到全域限流**：無法保護上游 LLM 服務的整體 API 配額

### 1.2 社群反饋

GitHub Issue [#12482](https://github.com/apache/apisix/issues/12482) 明確反映此問題：

> *"I have 2 apisix replicas, so I can not counter the tokens in local memory."*

官方 PR [#12751](https://github.com/apache/apisix/pull/12751) 正嘗試在原插件中加入 `policy` 欄位支援 Redis，但截至撰文時仍未合入 OSS 版本。

### 1.3 本 RFC 目標

建立一個獨立的 **`ai-rate-limiting-redis`** 插件，作為 `ai-rate-limiting` 的 Redis 版本替代方案，目標：

1. 100% 相容 `ai-rate-limiting` 現有的配置語義與行為
2. 將計數器儲存後端從 Local Memory 改為 **Redis 單節點**
3. 完整支援 `ai-proxy-multi` 的 Fallback 整合機制
4. 維持 Access Phase / Log Phase 雙階段設計

> **範疇限制（Out of Scope）**：本 RFC 僅涵蓋 Redis 單節點（非 Redis Cluster）。

---

## 2. 設計概覽 (Design Overview)

### 2.1 實作策略

`ai-rate-limiting-redis` = Clone `ai-rate-limiting` + 替換計數器後端

| 元件 | `ai-rate-limiting`（原版） | `ai-rate-limiting-redis`（本版） |
|---|---|---|
| 計數器儲存 | `lua-resty-limit-count`（local shared dict） | Redis `INCRBY` + `EXPIRE` Lua Script |
| 跨節點共享 | 否 | 是 |
| 設定相容性 | — | 100% 相容原版所有 Schema 屬性 |
| 新增屬性 | — | Redis 連線相關屬性（9 個） |
| `ai-proxy-multi` 整合 | 支援 | 完整支援（詳見 §5） |

### 2.2 計數器運作模式

沿用原版的 **Access Phase 佔位 + Log Phase 修正** 雙階段設計，差異在於計數器的讀寫目標由本地記憶體改為 Redis：

```
[Access Phase]  INCRBY key 1          → 快速佔位，確認是否超限
[Log Phase]     INCRBY key (N - 1)    → 補入真實 token 消耗（扣除佔位的 1）
```

---

## 3. Schema 定義 (Configuration Schema)

### 3.1 繼承自 `ai-rate-limiting` 的屬性

以下屬性與原版 `ai-rate-limiting` **完全一致**，行為不變：

| 屬性名稱 | 類型 | 預設值 | 必填 | 說明 |
|---|---|---|---|---|
| `limit` | integer (>0) | — | 條件必填 | 時間窗口內允許的最大 Token 數量 |
| `time_window` | integer (>0) | — | 條件必填 | 限流時間窗口（秒） |
| `limit_strategy` | enum | `total_tokens` | 否 | Token 計量策略：`total_tokens` / `prompt_tokens` / `completion_tokens` |
| `instances` | array | — | 否 | 針對個別 LLM 實例的獨立配額（詳見 §3.3） |
| `rules` | array | — | 否 | 進階規則，支援自訂 Key 細粒度限流 |
| `rejected_code` | integer (200~599) | `503` | 否 | 超限時返回的 HTTP 狀態碼 |
| `rejected_msg` | string | — | 否 | 超限時返回的錯誤訊息 Body |
| `show_limit_quota_header` | boolean | `true` | 否 | 是否回傳 `X-AI-RateLimit-*` Response Headers |

### 3.2 新增 Redis 連線屬性

| 屬性名稱 | 類型 | 預設值 | 必填 | 說明 |
|---|---|---|---|---|
| `redis_host` | string (minLen: 2) | — | **是** | Redis 節點位址（IP 或 hostname） |
| `redis_port` | integer (1~65535) | `6379` | 否 | Redis 連接埠 |
| `redis_username` | string (minLen: 1) | — | 否 | Redis ACL 使用者名稱（Redis 6+ ACL 模式） |
| `redis_password` | string (minLen: 0) | — | 否 | Redis 認證密碼 |
| `redis_database` | integer (≥0) | `0` | 否 | Redis 資料庫編號（0~15） |
| `redis_timeout` | integer (≥1) | `1000` | 否 | Redis 連線與操作超時（毫秒） |
| `redis_ssl` | boolean | `false` | 否 | 是否啟用 TLS/SSL 連線 |
| `redis_ssl_verify` | boolean | `false` | 否 | 是否驗證 Redis Server 的 SSL 憑證 |
| `redis_keepalive_timeout` | integer (≥1000) | `10000` | 否 | 連線池 keepalive 超時（毫秒） |
| `redis_keepalive_pool` | integer (≥1) | `100` | 否 | 連線池最大連線數 |

### 3.3 `instances` 陣列元素屬性（繼承自原版，無變化）

| 屬性名稱 | 類型 | 必填 | 說明 |
|---|---|---|---|
| `name` | string | 是 | 對應 `ai-proxy-multi` 的實例名稱 |
| `limit` | integer (>0) | 是 | 此實例的 Token 上限 |
| `time_window` | integer (>0) | 是 | 此實例的限流窗口（秒） |
| `limit_strategy` | enum | 否 | 此實例的 Token 計量策略 |

---

## 4. 內部實作設計 (Implementation Design)

### 4.1 Redis Key 命名規則

```
{plugin_name}:{route_id}:{instance_name}:{consumer_name_or_global}
```

範例：
```
ai-rate-limiting-redis:route-001:openai-instance:global
ai-rate-limiting-redis:route-001:deepseek-instance:consumer-alice
```

### 4.2 Redis Lua Script（原子操作）

為確保計數的原子性，使用 Redis Lua Script 執行：

```lua
-- KEYS[1] = rate limit key
-- ARGV[1] = limit (max tokens)
-- ARGV[2] = cost (tokens to deduct this call)
-- ARGV[3] = window (TTL in seconds)

local ttl = redis.call("TTL", KEYS[1])
if ttl < 0 then
    -- Key 不存在，初始化計數器
    redis.call("SET", KEYS[1], ARGV[1] - ARGV[2])
    redis.call("EXPIRE", KEYS[1], ARGV[3])
    return {ARGV[1] - ARGV[2], ARGV[3]}
else
    -- Key 已存在，扣除 cost
    local remaining = redis.call("INCRBY", KEYS[1], -ARGV[2])
    return {remaining, ttl}
end
```

- **返回值**：`[remaining_tokens, ttl_seconds]`
- **超限判斷**：`remaining < 0` 時拒絕請求

### 4.3 Access Phase 流程

```lua
function _M.access(conf, ctx)
    local instance_name = ctx.picked_server and ctx.picked_server.name or "global"
    local limit_conf     = get_limit_conf(conf, instance_name)

    -- 以 cost=1 作為佔位，快速判斷是否超限
    local remaining, ttl = redis_counter.incoming(key, 1)

    if remaining < 0 then
        -- 超限：將 key 扣回（還原佔位）
        redis_counter.restore(key, 1)
        return conf.rejected_code, conf.rejected_msg
    end

    -- 儲存佔位資訊至 ctx，Log Phase 使用
    ctx.ai_rate_limiting_redis = {
        key      = key,
        instance = instance_name,
        ttl      = ttl,
    }

    if conf.show_limit_quota_header then
        set_rate_limit_headers(limit_conf.limit, remaining, ttl)
    end
end
```

### 4.4 Log Phase 流程

```lua
function _M.log(conf, ctx)
    if not ctx.ai_rate_limiting_redis then return end

    local actual_tokens = get_token_usage(conf, ctx)
    if not actual_tokens or actual_tokens <= 0 then return end

    local key      = ctx.ai_rate_limiting_redis.key
    local extra    = actual_tokens - 1  -- 扣除 Access Phase 已佔位的 1

    if extra > 0 then
        -- 補入剩餘真實消耗
        redis_counter.incoming(key, extra)
    elseif extra < 0 then
        -- 實際消耗低於 1（極少見），還原多扣的差值
        redis_counter.restore(key, -extra)
    end
end
```

### 4.5 Token 計量策略取值邏輯

```lua
local function get_token_usage(conf, ctx)
    local usage = ctx.var.llm_token_usage  -- 由 ai-proxy / ai-proxy-multi 寫入
    if not usage then return nil end

    local strategy = conf.limit_strategy or "total_tokens"
    if strategy == "prompt_tokens" then
        return usage.prompt_tokens
    elseif strategy == "completion_tokens" then
        return usage.completion_tokens
    else
        return usage.total_tokens
    end
end
```

---

## 5. `ai-proxy-multi` 整合 (Integration with ai-proxy-multi)

### 5.1 整合架構

`ai-proxy-multi` 透過 `fallback_strategy: ["rate_limiting"]` 與限流插件整合：

- `ai-proxy-multi` 在選擇上游實例後，將 **實例名稱** 寫入 `ctx.picked_server.name`
- `ai-rate-limiting-redis` 於 Access Phase 讀取 `ctx.picked_server.name`，查找對應 Redis Key
- 若該實例超限，透過 **`ctx.ai_rate_limiting_rejected_instance`** 回傳拒絕資訊
- `ai-proxy-multi` 偵測此訊號後，切換至下一優先級實例

### 5.2 整合 Sequence Diagram

```
Client    ai-rate-limiting-redis    ai-proxy-multi    Instance A (Priority=1)   Instance B (Priority=2)
  |                |                     |                     |                         |
  |-- Request ----->|                     |                     |                         |
  |                |                     |                     |                         |
  |         [Access Phase]               |                     |                         |
  |         ai-proxy-multi 先選          |                     |                         |
  |         Instance A (priority=1)      |                     |                         |
  |         → ctx.picked_server.name     |                     |                         |
  |           = "instance-A"             |                     |                         |
  |                |                     |                     |                         |
  |         查詢 Redis Key:               |                     |                         |
  |         "...:instance-A:global"      |                     |                         |
  |         → remaining < 0             |                     |                         |
  |         → 超限！                     |                     |                         |
  |                |                     |                     |                         |
  |                |-- 設定 ctx.ai_rate  |                     |                         |
  |                |   limiting_rejected |                     |                         |
  |                |   _instance = "A"  ->|                     |                         |
  |                |                     |                     |                         |
  |                |              [Fallback]                    |                         |
  |                |              切換至 Instance B             |                         |
  |                |              (priority=2)                  |                         |
  |                |                     |---- Forward Req ----->|                         |
  |                |                     |                                               |
  |         [Access Phase 2nd check]    |                                               |
  |         查詢 Redis Key:               |                                               |
  |         "...:instance-B:global"      |                                               |
  |         → remaining = 200           |                                               |
  |         → 允許通過                   |                                               |
  |                |                     |------ Forward Req ---------------------------->|
  |                |                     |<---------------- LLM Response -----------------|
  |                |<-- Response --------|                                               |
  |                |                     |                                               |
  |         [Log Phase]                  |                                               |
  |         讀取 actual tokens (e.g. 47) |                                               |
  |         INCRBY "...:instance-B" +46  |                                               |
  |         (補入 47-1=46 額外消耗)       |                                               |
  |<-- Response ----|                    |                                               |
```

### 5.3 關鍵整合注意事項

#### ⚠️ 注意事項 1：實例名稱必須一致

`ai-rate-limiting-redis` 的 `instances[].name` **必須**與 `ai-proxy-multi` 的 `instances[].name` 完全相同，否則 Redis Key 查找將失敗，導致限流規則無法對應正確實例。

```json
// ai-proxy-multi 設定
"instances": [{ "name": "gpt4-prod", ... }]

// ai-rate-limiting-redis 設定
"instances": [{ "name": "gpt4-prod", "limit": 1000, "time_window": 60 }]
//                         ↑ 必須完全相同
```

#### ⚠️ 注意事項 2：Log Phase 只記錄實際服務該請求的實例

Fallback 發生時，Log Phase 只會更新**最終服務該請求的實例**的 Redis Counter，不會更新被拒絕的 Instance A 的計數（因為 Instance A 未真正處理請求，無實際 Token 消耗）。

#### ⚠️ 注意事項 3：Fallback 不計入 Log Phase 的 placeholder 還原

當 Instance A 超限被拒絕時，Access Phase 已還原對 Instance A 的佔位扣減（`restore +1`）。這確保 Instance A 的 Redis Counter 不會因為被拒絕的請求而被額外消耗。

#### ⚠️ 注意事項 4：`show_limit_quota_header` 在 Fallback 情境的行為

當 Fallback 發生時，Response Header 中的 `X-AI-RateLimit-*` 只反映**最終服務實例（Instance B）**的配額狀態，不包含 Instance A（已超限）的資訊。建議在 Fallback 情境下關閉此 Header，或在 Response Body 中提供更完整的資訊。

### 5.4 完整整合設定範例

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
          "auth": {
            "header": { "Authorization": "Bearer ${OPENAI_KEY}" }
          },
          "options": { "model": "gpt-4o" }
        },
        {
          "name": "deepseek-fallback",
          "provider": "deepseek",
          "weight": 100,
          "priority": 2,
          "auth": {
            "header": { "Authorization": "Bearer ${DEEPSEEK_KEY}" }
          },
          "options": { "model": "deepseek-chat" }
        }
      ],
      "fallback_strategy": ["rate_limiting"]
    },
    "ai-rate-limiting-redis": {
      "instances": [
        {
          "name": "openai-primary",
          "limit": 10000,
          "time_window": 60,
          "limit_strategy": "total_tokens"
        },
        {
          "name": "deepseek-fallback",
          "limit": 5000,
          "time_window": 60,
          "limit_strategy": "total_tokens"
        }
      ],
      "rejected_code": 429,
      "rejected_msg": "All AI instances are currently rate limited.",
      "show_limit_quota_header": true,
      "redis_host": "redis.internal",
      "redis_port": 6379,
      "redis_password": "${REDIS_PASSWORD}",
      "redis_database": 0,
      "redis_timeout": 1000,
      "redis_keepalive_timeout": 10000,
      "redis_keepalive_pool": 100
    }
  }
}
```

---

## 6. 與原版行為差異對照 (Behavioral Differences)

| 行為項目 | `ai-rate-limiting` | `ai-rate-limiting-redis` |
|---|---|---|
| 計數器儲存 | Nginx shared dict（本地） | Redis（外部共享） |
| 跨節點共享 | 否 | 是 |
| 計數器初始化 | 插件啟動時自動 | 第一次請求時由 Lua Script 建立 Key + TTL |
| TTL 管理 | 由 `lua-resty-limit-count` 管理 | 由 Redis `EXPIRE` 管理 |
| Redis 不可用時的行為 | 不適用 | **Fail-open**（預設）：Redis 連線失敗時記錄 Error Log，允許請求通過 |
| Access Phase 超限處理 | 直接返回 rejected_code | 同左 + 還原 Redis 佔位 |
| Log Phase 計數更新 | 寫入 shared dict | 透過 Redis INCRBY 原子更新 |
| 配置變更生效方式 | 即時 | 即時（下一個請求起生效） |

### 6.1 Redis 不可用的 Fail-open 設計

當 Redis 連線失敗或超時時，`ai-rate-limiting-redis` 採用 **Fail-open** 策略（允許請求通過），並記錄 Error Log：

```
[error] ai-rate-limiting-redis: failed to connect to Redis: timeout, allowing request to pass through
```

> **設計理由**：AI Gateway 的核心職責是轉發請求。若因 Redis 暫時不可用就阻斷所有 AI 流量，業務影響過大。Fail-open 確保服務可用性，代價是短暫的限流精確度降低。

---

## 7. 設定範例 (Configuration Examples)

### 7.1 全域限流（搭配 `ai-proxy`）

```json
{
  "uri": "/ai",
  "plugins": {
    "ai-proxy": {
      "provider": "openai",
      "auth": { "header": { "Authorization": "Bearer <token>" } },
      "options": { "model": "gpt-4o" }
    },
    "ai-rate-limiting-redis": {
      "limit": 5000,
      "time_window": 60,
      "limit_strategy": "total_tokens",
      "redis_host": "127.0.0.1",
      "redis_port": 6379
    }
  }
}
```

### 7.2 Consumer 層級限流

```json
{
  "uri": "/ai",
  "plugins": {
    "key-auth": {},
    "ai-proxy": { "...": "..." },
    "ai-rate-limiting-redis": {
      "rules": [
        {
          "key": "$consumer_name",
          "limit": 2000,
          "time_window": 3600,
          "limit_strategy": "total_tokens"
        }
      ],
      "redis_host": "redis.internal",
      "redis_password": "secret"
    }
  }
}
```

---

## 8. 測試計畫 (Test Plan)

| 測試項目 | 預期行為 |
|---|---|
| 單節點限流基本功能 | 達到 `limit` 後返回 `rejected_code` |
| 多節點共享計數 | 兩個 APISIX 節點共享同一 Redis Counter，總消耗不超限 |
| Log Phase Token 更新 | 實際 Token 消耗正確更新至 Redis |
| `ai-proxy-multi` Fallback 觸發 | Instance A 超限後流量切換至 Instance B |
| `ai-proxy-multi` Fallback Counter 正確性 | Fallback 後只更新 Instance B 的計數器 |
| Redis 連線失敗 Fail-open | Redis 不可用時請求仍可通過，Error Log 正常記錄 |
| `show_limit_quota_header` | Response Header 正確反映剩餘配額 |
| Redis SSL 連線 | `redis_ssl: true` 時正確建立加密連線 |
| Consumer 層級限流隔離 | 不同 Consumer 使用獨立計數器 |
| TTL 自動重置 | `time_window` 到期後配額自動重置 |

---

## 9. 已知限制 (Known Limitations)

| 限制 | 說明 | 緩解方案 |
|---|---|---|
| 不支援 Redis Cluster | 本版本僅支援 Redis 單節點 | 後續 RFC 可擴展 |
| Redis 為 SPOF | 單 Redis 若故障會影響計數精確度 | 啟用 Redis Sentinel / 採用 Fail-open |
| Log Phase 非同步特性 | Token 消耗在回應後才更新，極短窗口內可能有超限洩漏 | 屬設計取捨，無法完全消除 |
| 不支援滑動視窗 | 採用固定視窗（Fixed Window）計數 | 接受此限制（與原版一致） |

---

## 10. 參考資料 (References)

- [Apache APISIX `ai-rate-limiting` 官方文件](https://apisix.apache.org/docs/apisix/next/plugins/ai-rate-limiting/)
- [Apache APISIX `ai-proxy-multi` 官方文件](https://apisix.apache.org/docs/apisix/next/plugins/ai-proxy-multi/)
- [GitHub Issue #12482 - Local memory limitation for ai-rate-limiting](https://github.com/apache/apisix/issues/12482)
- [GitHub PR #12751 - feat: ai rate limiting redis support](https://github.com/apache/apisix/pull/12751)
- [APISIX `limit-count` Redis 實作參考](https://github.com/apache/apisix/blob/master/apisix/plugins/limit-count/limit-count-redis.lua)
- [APISIX Redis Schema 工具模組](https://github.com/apache/apisix/blob/master/apisix/utils/redis-schema.lua)
