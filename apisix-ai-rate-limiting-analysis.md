# Apache APISIX `ai-rate-limiting` 套件分析文件

> 版本參考：Apache APISIX 3.12 / next
> 分析日期：2026-03-31

---

## 1. 概述 (Overview)

`ai-rate-limiting` 是 Apache APISIX 針對 LLM（大型語言模型）流量設計的 **Token 用量速率限制**插件。

與傳統的 `limit-count`（以請求次數限流）不同，`ai-rate-limiting` 以 **Token 消耗量**作為計量單位，能更精確地控制 AI 服務的 API 用量與費用。

### 主要特性

- 基於 Token 數量的速率限制（而非單純請求次數）
- 支援三種 Token 計量策略：`prompt_tokens`、`completion_tokens`、`total_tokens`
- 支援對多個 LLM 實例設定獨立的限流配額
- 支援 Consumer 層級（per-user）的獨立限流
- 可與 `ai-proxy` / `ai-proxy-multi` 搭配，實現超配額後的自動 Fallback
- 支援在 Response Header 回傳剩餘配額資訊

---

## 2. 運作原理 (How It Works)

該插件分為兩個執行階段：

### 2.1 Access 階段（請求進入時）

1. 取得當前請求對應的 LLM 實例名稱及限流配置。
2. 呼叫內部的 `limit_count.rate_limit()` 函式，以 **1 個 Token** 作為佔位消耗進行前置檢查。
3. 若目前配額已超限，立即返回錯誤（HTTP 503 或自訂狀態碼）。
4. 若未超限，放行請求，繼續往 upstream 轉發。

> **注意：** Access 階段以 1 個 token 作為佔位，主要是為了判斷「是否仍有剩餘配額」，真正的消耗量在 Log 階段才更新。

### 2.2 Log 階段（回應完成後）

1. 從 Context 中讀取實際的 Token 用量（由 `ai-proxy` 記錄至 `ctx`）。
2. 根據 `limit_strategy` 取出對應的 Token 數字（`prompt_tokens` / `completion_tokens` / `total_tokens`）。
3. 將實際 Token 消耗量更新至計數器（扣除 Access 階段的佔位 1 token，補入真實值）。

---

## 3. Sequence Diagram

### 3.1 基本請求流程（未超限）

```
Client          APISIX (ai-rate-limiting)       ai-proxy            LLM Service
  |                        |                        |                     |
  |----  HTTP Request ----->|                        |                     |
  |                        |                        |                     |
  |               [Access Phase]                    |                     |
  |               Check local counter               |                     |
  |               consume 1 token (placeholder)     |                     |
  |               Counter OK → pass through         |                     |
  |                        |                        |                     |
  |                        |----  Forward Req ------>|                     |
  |                        |                        |---- API Call ------->|
  |                        |                        |<--- LLM Response ----|
  |                        |                        |  (includes token     |
  |                        |<--- Response -----------|   usage metadata)   |
  |                        |                        |                     |
  |               [Log Phase]                       |                     |
  |               Read actual token count           |                     |
  |               from ctx (total/prompt/completion)|                     |
  |               Update counter with real tokens   |                     |
  |                        |                        |                     |
  |<--- HTTP Response ------|                        |                     |
  |  (with X-AI-RateLimit- |                        |                     |
  |   Limit/Remaining/Reset|                        |                     |
  |   headers if enabled)  |                        |                     |
```

### 3.2 超限請求流程（Rate Limit Exceeded）

```
Client          APISIX (ai-rate-limiting)       ai-proxy            LLM Service
  |                        |                        |                     |
  |----  HTTP Request ----->|                        |                     |
  |                        |                        |                     |
  |               [Access Phase]                    |                     |
  |               Check local counter               |                     |
  |               Counter EXCEEDED                  |                     |
  |                        |                        |                     |
  |<-- 503 (or custom) ----|                        |                     |
  |   (rejected_msg body)  |                        |                     |
  |  X-AI-RateLimit-Reset  |                        |                     |
  |  header included       |                        |                     |
```

### 3.3 搭配 ai-proxy-multi 的 Fallback 流程

```
Client     ai-rate-limiting      ai-proxy-multi     Instance A (GPT-4)   Instance B (Deepseek)
  |               |                    |                   |                      |
  |-- Request ---->|                    |                   |                      |
  |               |                    |                   |                      |
  |          [Access Phase]            |                   |                      |
  |          Check Instance A quota    |                   |                      |
  |          → EXCEEDED                |                   |                      |
  |               |                    |                   |                      |
  |               |-- Fallback signal ->|                   |                      |
  |               |  (rate_limiting    |                   |                      |
  |               |   fallback_strategy|                   |                      |
  |               |   triggered)       |                   |                      |
  |               |                    |---- Route to B --->|                      |
  |               |                    |                                          |
  |               |                    |<------------- Response -------------------|
  |               |<-- Response --------|                                          |
  |<-- Response ---|                    |                                          |
```

### 3.4 Local Memory 限制示意（多節點問題）

```
                  ┌─────────────────────────────────────────┐
                  │            Load Balancer                │
                  └───────────┬─────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
  ┌─────────────────────┐         ┌─────────────────────┐
  │   APISIX Node 1     │         │   APISIX Node 2     │
  │                     │         │                     │
  │  ai-rate-limiting   │         │  ai-rate-limiting   │
  │  Local Counter: 80  │         │  Local Counter: 75  │
  │  (Limit: 100)       │         │  (Limit: 100)       │
  │                     │         │                     │
  └─────────────────────┘         └─────────────────────┘

  ⚠️  問題：兩個節點各自維護獨立的 Counter
      實際總消耗 = 80 + 75 = 155 tokens
      但每個節點以為「還未超限」→ 真實消耗超過設定的 100 token 限制
```

---

## 4. 設定屬性說明 (Configuration Properties)

### 4.1 頂層屬性

| 屬性名稱 | 類型 | 預設值 | 必填 | 說明 |
|---|---|---|---|---|
| `limit` | integer (>0) | - | 條件必填 | 時間窗口內允許的最大 Token 數量。若設定了 `time_window`，則此欄位為必填。若使用 `instances` 欄位則可省略此頂層設定。 |
| `time_window` | integer (>0) | - | 條件必填 | 限流的時間窗口，單位為**秒**。若設定了 `limit`，則此欄位為必填。 |
| `limit_strategy` | string (enum) | `total_tokens` | 否 | Token 計量策略，可選值：`total_tokens`、`prompt_tokens`、`completion_tokens`。 |
| `instances` | array | - | 否 | 針對個別 LLM 實例的獨立限流配置，詳見 §4.2。 |
| `rules` | array | - | 否 | 進階規則，支援以自訂 Key 或變數進行細粒度控制，詳見 §4.3。 |
| `rejected_code` | integer (200~599) | `503` | 否 | 超出限制時返回的 HTTP 狀態碼。 |
| `rejected_msg` | string | - | 否 | 超出限制時返回的自訂錯誤訊息 Body。 |
| `show_limit_quota_header` | boolean | `true` | 否 | 是否在 Response Header 中回傳限流配額資訊（`X-AI-RateLimit-*` 系列 Header）。 |

### 4.2 `instances` 陣列元素屬性

| 屬性名稱 | 類型 | 預設值 | 必填 | 說明 |
|---|---|---|---|---|
| `name` | string | - | 是 | 對應 `ai-proxy-multi` 中所設定的 LLM 實例名稱（Instance Name）。 |
| `limit` | integer (>0) | - | 是 | 此實例的 Token 上限。 |
| `time_window` | integer (>0) | - | 是 | 此實例的限流時間窗口（秒）。 |
| `limit_strategy` | string (enum) | `total_tokens` | 否 | 此實例的 Token 計量策略，同頂層屬性說明。 |

### 4.3 `rules` 陣列元素屬性

| 屬性名稱 | 類型 | 說明 |
|---|---|---|
| `key` | string | 用於區分計數的識別鍵（例如 Consumer 名稱、IP 等 APISIX 變數）。 |
| `limit` | integer | 此規則的 Token 上限。 |
| `time_window` | integer | 此規則的時間窗口（秒）。 |
| `limit_strategy` | string (enum) | 此規則的 Token 計量策略。 |

### 4.4 limit_strategy 詳細說明

| 策略值 | 計量對象 | 說明 |
|---|---|---|
| `total_tokens` | 總 Token 數 | `prompt_tokens + completion_tokens`，最全面的統計方式，涵蓋輸入與輸出，**預設值**。 |
| `prompt_tokens` | 輸入 Token 數 | 僅計算 LLM Request 中的輸入（Prompt）部分，適合控制輸入成本。 |
| `completion_tokens` | 輸出 Token 數 | 僅計算 LLM Response 中生成的輸出部分，適合控制生成量。 |

### 4.5 Response Headers（當 `show_limit_quota_header: true`）

| Header 名稱 | 說明 |
|---|---|
| `X-AI-RateLimit-Limit-{name}` | 該實例的 Token 上限配額 |
| `X-AI-RateLimit-Remaining-{name}` | 該實例的剩餘可用 Token 數 |
| `X-AI-RateLimit-Reset-{name}` | 計數器重置的 Unix Timestamp（秒） |

---

## 5. Local Memory 限制分析 (Current Limitation)

### 5.1 目前的限制

**Apache APISIX 開源版** 的 `ai-rate-limiting` 插件，計數器**僅支援 Local Memory（本地記憶體）儲存**。

這意味著每一個 APISIX 節點各自維護獨立的 Token 計數器，節點之間**完全不共享**計數狀態。

### 5.2 問題根源

APISIX 通常以叢集（Cluster）模式部署，前端通過 Load Balancer 分散流量至多個節點。在此架構下：

```
實際全域消耗 = Node1 Counter + Node2 Counter + ... + NodeN Counter
```

但每個節點只看得到自己的計數器，不知道其他節點的消耗狀況，導致：

- **超限保護失效**：真實 Token 消耗可能遠超設定的 `limit` 值。
- **配額精確度低**：N 個節點時，最大洩漏量可達 `limit × N` tokens。
- **限流不公平**：同一 Consumer 打到不同節點會有不同的配額體驗。

### 5.3 社群反饋與 Issue

來自 Apache APISIX GitHub Issue [#12482](https://github.com/apache/apisix/issues/12482)：

> *"I have 2 apisix replicas, so I can not counter the tokens in local memory."*

社群回應：
> *"Currently not supported, welcome to submit PR to extend this functionality."*

### 5.4 解決方案進展

PR [#12751](https://github.com/apache/apisix/pull/12751)（`feat: ai rate limiting redis support`）正在開發中，計畫新增 `policy` 欄位，支援：

| `policy` 值 | 儲存位置 | 適用場景 |
|---|---|---|
| `local`（預設） | 本地記憶體 | 單節點 / 測試環境 |
| `redis` | Redis 單節點 | 多節點共享計數 |
| `redis-cluster` | Redis Cluster | 高可用 + 多節點共享計數 |

> ⚠️ **重要提醒**：`policy` 欄位目前（截至 2026-03-31）在 **Apache APISIX 開源版**尚未正式發布。API7 Enterprise 版本 3.9.2+ 已支援此功能。

### 5.5 與其他限流插件的比較

| 插件名稱 | 計量單位 | 支援 Redis | 備註 |
|---|---|---|---|
| `limit-req` | 請求速率 (req/s) | 否 | 基於漏桶算法 |
| `limit-count` | 請求次數 | ✅ 支援 | 支援 local / redis / redis-cluster |
| `limit-conn` | 並發連線數 | 否 | 基於並發數控制 |
| `ai-rate-limiting` | Token 數量 | ⚠️ 開發中 | 目前 OSS 版本僅 local |

---

## 6. 使用範例 (Examples)

### 6.1 基本範例：搭配 `ai-proxy` 限制 Token 用量

```json
{
  "uri": "/ai",
  "plugins": {
    "ai-proxy": {
      "provider": "openai",
      "auth": {
        "header": {
          "Authorization": "Bearer <your-token>"
        }
      },
      "options": {
        "model": "gpt-4"
      }
    },
    "ai-rate-limiting": {
      "limit": 300,
      "time_window": 30,
      "limit_strategy": "prompt_tokens",
      "rejected_code": 429,
      "rejected_msg": "Token quota exceeded, please try again later.",
      "show_limit_quota_header": true
    }
  }
}
```

**說明：** 每 30 秒內，所有請求的 `prompt_tokens` 累積上限為 300。超過後返回 HTTP 429。

### 6.2 進階範例：搭配 `ai-proxy-multi` 對多實例設定不同配額

```json
{
  "uri": "/ai",
  "plugins": {
    "ai-proxy-multi": {
      "instances": [
        {
          "name": "openai-instance",
          "provider": "openai",
          "weight": 100,
          "priority": 1,
          "auth": { "header": { "Authorization": "Bearer <token>" } },
          "options": { "model": "gpt-4" }
        },
        {
          "name": "deepseek-instance",
          "provider": "deepseek",
          "weight": 100,
          "priority": 2,
          "auth": { "header": { "Authorization": "Bearer <token>" } },
          "options": { "model": "deepseek-chat" }
        }
      ],
      "fallback_strategy": ["rate_limiting"]
    },
    "ai-rate-limiting": {
      "instances": [
        {
          "name": "openai-instance",
          "limit": 1000,
          "time_window": 60,
          "limit_strategy": "total_tokens"
        },
        {
          "name": "deepseek-instance",
          "limit": 500,
          "time_window": 60,
          "limit_strategy": "total_tokens"
        }
      ],
      "show_limit_quota_header": true
    }
  }
}
```

**說明：** `openai-instance` 每分鐘最多 1000 total tokens；`deepseek-instance` 每分鐘最多 500 total tokens。當 `openai-instance` 超限後，透過 `fallback_strategy: ["rate_limiting"]`，流量自動切換至 `deepseek-instance`。

### 6.3 Consumer 層級限流範例

```json
{
  "uri": "/ai",
  "plugins": {
    "ai-proxy": { "...": "..." },
    "key-auth": {},
    "ai-rate-limiting": {
      "rules": [
        {
          "key": "$consumer_name",
          "limit": 500,
          "time_window": 3600,
          "limit_strategy": "total_tokens"
        }
      ]
    }
  }
}
```

**說明：** 每個 Consumer 每小時各自享有 500 total tokens 的獨立配額。

---

## 7. 架構限制與建議 (Limitations & Recommendations)

| 場景 | 問題 | 建議 |
|---|---|---|
| 單節點部署 | 無問題 | 直接使用 `local` policy |
| 多節點部署（OSS） | Local Counter 各自獨立，實際消耗可能超限 | 等待 PR #12751 合入，或使用 API7 Enterprise |
| 高精確度限流需求 | Local Memory 精確度不足 | 使用支援 Redis 的 API7 Enterprise 版本 |
| 高可用限流需求 | 單 Redis 有 SPOF 風險 | 使用 `redis-cluster` policy（API7 Enterprise） |
| 測試 / 開發環境 | 精確度要求低 | 可直接使用 Local Memory |

---

## 8. 參考資料 (References)

- [Apache APISIX 官方文件 - ai-rate-limiting (3.12)](https://apisix.apache.org/docs/apisix/3.12/plugins/ai-rate-limiting/)
- [Apache APISIX 官方文件 - ai-rate-limiting (next)](https://apisix.apache.org/docs/apisix/next/plugins/ai-rate-limiting/)
- [API7 Hub - AI Rate Limiting](https://docs.api7.ai/hub/ai-rate-limiting/)
- [GitHub Issue #12482 - How to config ai-rate-limiting plugin's counter to redis](https://github.com/apache/apisix/issues/12482)
- [GitHub PR #12751 - feat: ai rate limiting redis support](https://github.com/apache/apisix/pull/12751)
- [Apache APISIX 開源版 GitHub Repository](https://github.com/apache/apisix)
