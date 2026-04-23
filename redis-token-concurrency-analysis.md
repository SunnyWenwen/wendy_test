# Redis Token 更新機制：非阻塞設計與高並發分析

## 1. 設計核心：讀寫完全分離

這個 plugin 把「gate check（要不要讓這個 request 進來）」和「accounting（記錄實際用了多少 token）」拆成兩個完全獨立的階段：

```
┌─────────────────────────────────────────────────────────┐
│  Access Phase (同步, 在 response 前)                      │
│  目的: 判斷這個 request 能不能進                           │
│  動作: Redis GET → 讀 remaining，不寫任何東西              │
└─────────────────────────────────────────────────────────┘
              │ request 通過 → 送到 LLM → LLM 回應 → response 送出
              ↓
┌─────────────────────────────────────────────────────────┐
│  Log Phase (非同步 timer, 在 response 之後)               │
│  目的: 把實際消耗的 token 扣掉                             │
│  動作: ngx.timer.at(0, fn) → 下一個 event loop 才執行     │
│         → limit_count.rate_limit(conf, ctx, used_tokens) │
│         → Redis INCRBY/script (原子操作)                  │
└─────────────────────────────────────────────────────────┘
```

---

## 2. 為什麼 Log Phase 不會阻塞主 worker

### `ngx.timer.at(0, fn)` 的工作原理

```lua
-- ai-rate-limiting.lua log phase
local ok, err = ngx.timer.at(0, function(premature)
    if premature then return end
    -- 這裡才真正寫 Redis
    limit_count.rate_limit(timer_limit_conf, timer_ctx, plugin_name, used_tokens)
end)
```

OpenResty 的事件循環（event loop）是這樣運作的：

```
主 worker 事件循環:
  ┌─────────────────────────────────────────────────────┐
  │  Tick N                                              │
  │   ├─ 處理 request A (access phase → LLM → response) │
  │   └─ 呼叫 ngx.timer.at(0, fn_A) ← 只是「登記」      │
  │                                                      │
  │  Tick N+1                                            │
  │   ├─ 處理 request B (access phase → ...)             │
  │   └─ 執行 fn_A (timer callback, 輕量 coroutine)      │
  │        └─ 在這裡才發送 Redis INCRBY                   │
  └─────────────────────────────────────────────────────┘
```

**關鍵點：**
- `ngx.timer.at(0, fn)` 第一個參數是 delay，0 表示「下一個可用的 event loop tick 就執行」
- timer callback 是一個獨立的輕量 coroutine，不阻塞正在處理其他 request 的 worker
- Response 已經送出給 client 之後，timer 才執行 → **client 不感知 Redis 寫入延遲**

---

## 3. Access Phase：Redis GET 的設計

```lua
-- Redis policy: 直接 GET，純讀
local redis_key = "plugin-ai-rate-limiting" .. limit_conf.group .. ":" .. limit_conf.key
local val = red:get(redis_key)

-- key 不存在 → 這個 time window 還沒有任何 token 被消耗 → 通過
if val == ngx.null or val == nil then
    return nil  -- PASS
end

local remaining = tonumber(val)
if remaining <= 0 then
    return 429  -- REJECT
end
return nil  -- PASS
```

Redis key 裡存的是 **remaining（剩餘配額）**：
- Key 不存在 = 新的 time window，全額可用 → 通過
- remaining > 0 = 還有配額 → 通過
- remaining ≤ 0 = 配額耗盡 → 拒絕

**為什麼只 GET 不寫？**
因為在 access phase 還不知道這個 LLM request 實際會用多少 token（LLM 還沒跑）。
用完之後才扣，是唯一合理的設計。

---

## 4. Redis 原子性（Atomicity）

### Redis 單執行緒模型

Redis 本身是 **single-threaded** 執行命令的。不管有多少個 client 同時送命令過來，Redis 都是一個一個排隊處理：

```
Client A → ─┐
Client B → ─┤─→ Redis command queue → [cmd1][cmd2][cmd3]... → 依序執行
Client C → ─┘
```

所以單一命令（如 `GET`、`INCRBY`）天生就是原子的 —— 不會被其他命令中斷。

### Lua Script 的原子性

Log phase 的計數更新不是一個簡單的 INCRBY，而是透過 `limit_count.rate_limit()` 呼叫一段 Redis Lua script：

```lua
-- limit-count-redis.lua (APISIX 內建，log phase 呼叫路徑)
local current = redis.call('INCRBY', key, cost)
if current == cost then
    redis.call('PEXPIRE', key, expiry * 1000)  -- 第一次寫入，設 TTL
end
if current > limit then
    return {current - cost, 'rejected'}
end
return {limit - current, 'accepted'}
```

Redis 保證：**整個 Lua script 執行期間不接受任何其他命令**。
這稱為 "script atomicity"，等同於一個 transaction。

```
Timer A 的 INCRBY script ─┐
Timer B 的 INCRBY script ─┤─→ Redis Lua 執行器 → [Script_A][Script_B][Script_C]
Timer C 的 INCRBY script ─┘                        串行，互不交錯
```

**結論：** 不管有多少個 worker 的 timer 同時送 INCRBY script 過來，Redis 都會把它們排隊，一個執行完才執行下一個，最終結果保證正確累加。

---

## 5. 高並發情境：A、B、C Request 的 GET / UPDATE 順序

### 前提設定

```
limit = 1000 tokens / 60s
policy = redis
初始狀態: Redis key 不存在（新的 time window）
```

---

### 情境一：完全循序（理想情況）

```
時間軸:
t=0  A 進來 → GET redis → key 不存在 → PASS
t=1  LLM 回應 A → 用了 300 token → response 送出 → timer 登記
t=2  Timer_A 執行 → INCRBY 300 → remaining = 700

t=3  B 進來 → GET redis → remaining = 700 → PASS
t=4  LLM 回應 B → 用了 400 token → response 送出 → timer 登記
t=5  Timer_B 執行 → INCRBY 400 → remaining = 300

t=6  C 進來 → GET redis → remaining = 300 → PASS
t=7  LLM 回應 C → 用了 200 token → response 送出 → timer 登記
t=8  Timer_C 執行 → INCRBY 200 → remaining = 100
```

```
Redis remaining: 1000 → 700 → 300 → 100
每個 request 都看到最新的 remaining → 最精準
```

---

### 情境二：高並發，所有 Timer 都在下個 tick 才執行（最常見）

```
時間軸（A、B、C 幾乎同時進來）:

t=0  A GET → key 不存在 → PASS（remaining 顯示 1000）
t=0  B GET → key 不存在 → PASS（remaining 顯示 1000）← B 不知道 A 在跑
t=0  C GET → key 不存在 → PASS（remaining 顯示 1000）← C 也不知道 A、B

t=1  A LLM 回應（300 token）→ response 送出 → Timer_A 登記
t=1  B LLM 回應（400 token）→ response 送出 → Timer_B 登記
t=1  C LLM 回應（200 token）→ response 送出 → Timer_C 登記

t=2  Timer_A 執行 → Redis INCRBY 300 → remaining = 700  ─┐
t=2  Timer_B 執行 → Redis INCRBY 400 → remaining = 300   ├─ Redis 串行處理
t=2  Timer_C 執行 → Redis INCRBY 200 → remaining = 100  ─┘
```

```
Redis remaining 變化: [不存在] → 700 → 300 → 100

結論:
  ✅ A + B + C 共 900 token < 1000 limit → 三個都正確通過
  ✅ timer 雖然幾乎同時打到 Redis，但 Redis 保證原子串行更新
  ✅ 最終 remaining = 100，下一個 request 的 GET 會看到正確值
```

---

### 情境三：高並發 + 接近配額上限（overshoot 現象）

```
前提: 目前 remaining = 150（之前的 request 已消耗 850 token）

t=0  A GET → remaining = 150 → PASS
t=0  B GET → remaining = 150 → PASS  ← timer 還沒打到 Redis，還是看到 150
t=0  C GET → remaining = 150 → PASS  ← 同上

t=1  Timer_A (100 token) → INCRBY → remaining = 50
t=1  Timer_B (100 token) → INCRBY → remaining = -50  ← 已超標，但 B 的 response 早就送出了
t=1  Timer_C (100 token) → INCRBY → remaining = -150 ← 同上
```

```
Redis remaining 變化: 150 → 50 → -50 → -150

結論:
  ⚠️  A、B、C 同時通過 GET check，都看到 remaining = 150
  ⚠️  實際消耗 300 token，超過剩餘的 150
  ✅  超標會被「記錄」（remaining 變負數）
  ✅  下一個 request D GET → remaining = -150 → REJECT 429
  ✅  Redis 的 INCRBY 串行確保 remaining 正確累計為 -150，不會算錯
```

這是這個設計的 **trade-off**：允許極短暫的超量（burst overshoot），換取非阻塞的高吞吐量。

---

### 情境四：Timer 更新順序不定（網路/OS 排程差異）

```
A、B、C 的 timer 幾乎同時登記，但 OS 排程順序可能是 B → C → A

t=2  Timer_B 執行 → INCRBY 400 → remaining = 600
t=2  Timer_C 執行 → INCRBY 200 → remaining = 400
t=2  Timer_A 執行 → INCRBY 300 → remaining = 100
```

```
Timer 執行順序: B → C → A（非原始 request 順序）

結論:
  ✅ 因為 Redis INCRBY 是純加法（交換律成立），B+C+A = A+B+C = 900
  ✅ 順序不影響最終結果 remaining = 100
  ✅ 這是 Redis 原子性 + INCRBY 交換律共同保證的
```

---

## 6. GET 邏輯 vs UPDATE 邏輯的隔離

```
                  ┌──────────────────────────────────┐
                  │         Access Phase              │
Request ──────────►  check_limit_available()          │
                  │    ├─ (redis) red:get(key)        │
                  │    │   純讀，不寫，不加鎖           │
                  │    └─ remaining ≤ 0 → reject 429  │
                  └──────────────┬───────────────────┘
                                 │ PASS
                                 ▼
                  ┌──────────────────────────────────┐
                  │   LLM 處理（可能數秒）             │
                  └──────────────┬───────────────────┘
                                 │
                                 ▼
                  ┌──────────────────────────────────┐
                  │         Response 送出             │
                  └──────────────┬───────────────────┘
                                 │
                                 ▼
                  ┌──────────────────────────────────┐
                  │         Log Phase                 │
                  │  ngx.timer.at(0, fn)              │
                  │    └─ limit_count.rate_limit()    │
                  │         └─ Redis Lua Script        │
                  │              └─ INCRBY used_tokens │
                  │              └─ 設 TTL（第一次）   │
                  └──────────────────────────────────┘
```

**兩個 phase 完全解耦的好處：**

| | Access Phase (GET) | Log Phase (INCRBY) |
|---|---|---|
| 時機 | Request 進來時（同步） | Response 送出後（非同步） |
| 阻塞 worker | 是（但只有一次 GET，極快） | 否（timer coroutine） |
| Client 感知延遲 | 是 | 否 |
| Redis 操作 | 純讀 GET | 讀寫 Lua Script |
| 資料一致性 | 樂觀讀（可能 stale） | 最終一致（eventual） |
| 失敗影響 | allow_degradation 決定 | 只寫 warn log，不影響 response |

---

## 7. 各情境總結

| 情境 | GET 看到的 remaining | 實際結果 | 說明 |
|---|---|---|---|
| 完全循序 | 每次都是最新值 | 精準控制 | 理想情況 |
| 並發但總量 < limit | 全部看到高值，全部通過 | 正確 | timer 最終正確累計 |
| 並發且接近 limit | 多個 request 都看到剩餘配額 | 短暫 overshoot | 設計的 trade-off |
| Timer 亂序 | - | remaining 結果相同 | INCRBY 加法交換律 |
| 超標後下個 request | GET 看到負數 remaining | 正確 reject 429 | 恢復正常閘控 |

---

## 8. 設計取捨結論

這個設計選擇了 **高吞吐量 + 非阻塞** 而非 **精準即時限流**：

**接受的代價：**
- 高並發瞬間可能輕微超過 TPM 上限（burst overshoot）
- GET 讀到的 remaining 可能是幾毫秒前的舊值

**換來的好處：**
- Worker 不因 Redis 寫入而阻塞
- Client latency 不包含 Redis 寫入時間
- LLM gateway 場景下，單次超標幾百個 token 影響遠小於 blocking 帶來的 latency 增加
- Redis 原子性保證長期計數準確，不會因並發而算錯總量
