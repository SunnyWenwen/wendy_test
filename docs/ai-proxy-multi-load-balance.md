# APISIX `ai-proxy-multi` Load Balance 機制文件

> 原始碼參考：
> - [`apisix/plugins/ai-proxy-multi.lua`](https://github.com/apache/apisix/blob/release/3.15/apisix/plugins/ai-proxy-multi.lua)
> - [`apisix/plugins/ai-proxy/schema.lua`](https://github.com/apache/apisix/blob/release/3.15/apisix/plugins/ai-proxy/schema.lua)
> - [`apisix/balancer/chash.lua`](https://github.com/apache/apisix/blob/release/3.15/apisix/balancer/chash.lua)

---

## 一、Schema 定義總覽

```lua
-- apisix/plugins/ai-proxy/schema.lua
balancer = {
    type = "object",
    properties = {
        algorithm = {
            type = "string",
            enum = { "chash", "roundrobin" },
        },
        hash_on = {
            type = "string",
            default = "vars",
            enum = { "vars", "header", "cookie", "consumer", "vars_combinations" },
        },
        key = {
            description = "the key of chash for dynamic load balancing",
            type = "string",
        },
    },
    default = { algorithm = "roundrobin" }   -- ← 整個 balancer 欄位的預設值
}
```

**Instance 層級欄位：**

```lua
-- 每個 instance 必填 weight，priority 選填（預設 0）
priority = { type = "integer", default = 0 }
weight   = { type = "integer", minimum = 0 }   -- required
```

---

## 二、Load Balance 演算法選項

### 2.1 Roundrobin（預設）

**預設行為**：省略 `balancer` 欄位時自動套用。

**機制**：依照各 instance 的 `weight` 比例輪流分發請求。weight 越高，分配到的請求比例越大。

**設定範例：**

```yaml
plugins:
  ai-proxy-multi:
    balancer:
      algorithm: roundrobin   # 可省略，此為預設
    instances:
      - name: llm-a
        weight: 2             # 分配 2/3 的請求
        priority: 0
        provider: openai
        ...
      - name: llm-b
        weight: 1             # 分配 1/3 的請求
        priority: 0
        provider: openai
        ...
```

**原始碼佐證** — 動態載入 balancer：

```lua
-- apisix/plugins/ai-proxy-multi.lua
local function create_server_picker(conf, ups_tab, checkers)
    local picker = pickers[conf.balancer.algorithm]
    if not picker then
        pickers[conf.balancer.algorithm] =
            require("apisix.balancer." .. conf.balancer.algorithm)
        picker = pickers[conf.balancer.algorithm]
    end
    ...
    return picker.new(new_instances[new_instances._priority_index[1]], ups_tab)
end
```

---

### 2.2 Chash（Consistent Hashing，一致性雜湊）

**機制**：將所有 instance 以虛擬節點分佈在一個 hash ring 上，依請求的特定 key 雜湊後對應到固定的 instance。**同樣的 key 值永遠路由到同一個 instance**（除非該 instance 不可用）。

**原始碼佐證** — 虛擬節點建立：

```lua
-- apisix/balancer/chash.lua
-- 每個 instance 依 weight 取得 160 個虛擬節點
weight_normalized = weight / gcd
ring_positions = weight_normalized * CONSISTENT_POINTS   -- CONSISTENT_POINTS = 160

-- 查詢：用 key hash 找環上最近的 instance
picker:find(chash_key)
```

**`hash_on` 與 `key` 設定對照表：**

| `hash_on` | `key` 填入值 | Hash 來源 | 備註 |
|-----------|-------------|-----------|------|
| `vars`（預設） | NGINX 變數名，如 `remote_addr` | NGINX 自動解析的請求變數 | key 必填 |
| `header` | HTTP Header 名，如 `x-user-id` | Request Header 值 | key 必填 |
| `cookie` | Cookie 名，如 `session_id` | Cookie 值 | key 必填 |
| `consumer` | 不需填 | APISIX 認證後的 consumer 名稱 | key 可省略 |
| `vars_combinations` | 多變數組合，如 `$remote_addr$uri` | 組合字串 | key 必填 |

**常用 `vars` key 值：**

| key | 說明 |
|-----|------|
| `remote_addr` | 客戶端 IP（TCP 層自動解析）|
| `uri` | 請求路徑 |
| `request_uri` | 完整 URL（含 query string）|
| `http_x_forwarded_for` | Proxy 後的真實 IP |

**設定範例 — 依 User ID 固定路由（對話連貫性）：**

```yaml
plugins:
  ai-proxy-multi:
    balancer:
      algorithm: chash
      hash_on: header
      key: x-user-id        # 同一 user 永遠打同一個 LLM instance
    instances:
      - name: llm-a
        weight: 1
        priority: 0
        provider: openai
        ...
      - name: llm-b
        weight: 1
        priority: 0
        provider: openai
        ...
```

**設定範例 — 依客戶端 IP：**

```yaml
balancer:
  algorithm: chash
  hash_on: vars
  key: remote_addr
```

**原始碼佐證** — key 取值邏輯：

```lua
-- apisix/balancer/chash.lua
if hash_on == "vars" then
    chash_key = ctx.var[key]
elseif hash_on == "header" then
    chash_key = core.request.header(ctx, key)
elseif hash_on == "cookie" then
    chash_key = ctx.var["cookie_" .. key]
elseif hash_on == "consumer" then
    chash_key = ctx.consumer_name
end
```

---

## 三、Priority 機制（跨演算法適用）

`priority` 是 instance 層級的欄位，與 `algorithm` 無關，但影響 balancer 的選擇邏輯。

**原則：數字越大優先度越高，高優先度的 instance 會被優先選取。**

**原始碼佐證：**

```lua
-- apisix/plugins/ai-proxy-multi.lua
local function create_server_picker(conf, ups_tab, checkers)
    ...
    -- 有多個 priority 層級時，自動啟用 priority_balancer
    if #new_instances._priority_index > 1 then
        return priority_balancer.new(new_instances, ups_tab, picker)
    end
    -- 同一 priority：直接使用指定的 algorithm
    return picker.new(new_instances[new_instances._priority_index[1]], ups_tab)
end
```

**Priority 設定範例 — 主備切換：**

```yaml
instances:
  - name: primary-llm
    priority: 1          # 高優先，永遠先打這裡
    weight: 1
    ...
  - name: fallback-llm
    priority: 0          # 低優先，primary 失敗才使用
    weight: 1
    ...
```

---

## 四、Fallback Strategy（錯誤觸發切換）

設定在何種錯誤情況下，自動 retry 並切換到下一個 instance。

```lua
fallback_strategy = {
    anyOf = {
        { type = "string", enum = { "instance_health_and_rate_limiting", "http_429", "http_5xx" } },
        { type = "array",  items = { type = "string", enum = { "rate_limiting", "http_429", "http_5xx" } } }
    }
}
```

**原始碼佐證 — retry 邏輯：**

```lua
-- apisix/plugins/ai-proxy-multi.lua
local function retry_on_error(ctx, conf, code)
    if not ctx.server_picker then
        return code
    end
    ctx.server_picker.after_balance(ctx, true)   -- 標記當前 instance 已失敗
    if (code == 429 and fallback_strategy_has(conf.fallback_strategy, "http_429")) or
       (code >= 500 and code < 600 and
        fallback_strategy_has(conf.fallback_strategy, "http_5xx")) then
        local name, ai_instance, err = pick_ai_instance(ctx, conf)
        ...
    end
    return code
end
```

| `fallback_strategy` 值 | 觸發條件 |
|------------------------|---------|
| `http_5xx` | 上游回傳 500–599 |
| `http_429` | 上游回傳 429（rate limit）|
| `rate_limiting` | APISIX ai-rate-limiting 插件判斷超限 |
| `instance_health_and_rate_limiting` | 健康檢查失敗或限流 |

---

## 五、Priority 與 Algorithm 的關係

| 情況 | 行為 |
|------|------|
| 同 priority + roundrobin | 輪流平均分配，無法保證「永遠先打 A」|
| 同 priority + chash | 依 key hash 固定路由 |
| 不同 priority | 自動啟用 priority_balancer，高 priority 優先 |
| 不同 priority + fallback_strategy | 高 priority instance 失敗才切換到低 priority |

---

## 六、演算法選擇建議

| 場景 | 建議演算法 | 建議搭配 |
|------|-----------|---------|
| 無狀態請求、純負載分散 | `roundrobin` | `fallback_strategy: http_5xx` |
| 有狀態對話（同 user 同 instance）| `chash` + `hash_on: header` | `fallback_strategy: http_5xx` |
| 依 IP 固定路由 | `chash` + `hash_on: vars` + `key: remote_addr` | — |
| 主備切換（A 掛才用 B）| `roundrobin` + 不同 `priority` | `checks.active` + `fallback_strategy` |

---

## 七、完整設定範例

```yaml
plugins:
  ai-proxy-multi:
    balancer:
      algorithm: chash
      hash_on: header
      key: x-user-id
    fallback_strategy:
      - http_5xx
      - http_429
    instances:
      - name: primary
        priority: 1
        weight: 1
        provider: openai
        auth:
          header:
            Authorization: "Bearer sk-xxx"
        model:
          name: gpt-4o
        checks:
          active:
            type: https
            http_path: /v1/models
            healthy:
              interval: 10
              successes: 1
            unhealthy:
              interval: 5
              http_failures: 2
      - name: fallback
        priority: 0
        weight: 1
        provider: openai
        auth:
          header:
            Authorization: "Bearer sk-yyy"
        model:
          name: gpt-4o-mini
```
