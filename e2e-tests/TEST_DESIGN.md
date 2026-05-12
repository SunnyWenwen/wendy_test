# LLM Gateway E2E 測試設計說明

> 本文件說明針對自架 LLM Gateway 的端對端整合測試規劃，包含測試目標、測試資料設計邏輯、測試類型與覆蓋範圍，供技術評審與專案報告使用。

---

## 一、測試背景與目標

### 1.1 系統架構

```
使用者 (Claude Code / Kilo Code)
    │  OpenAI-compatible /chat/completions
    ▼
API6 Gateway (ai-proxy-multi)          ← 入口、認證、限流
    │
    ▼
LiteLLM                                ← 格式轉換、路由、重試
    │           │
    ▼           ▼
AWS Bedrock   Vertex AI
(Claude)      (Gemini)
```

### 1.2 測試目標

| 目標 | 說明 |
|---|---|
| **功能正確性** | 每個模型皆能回傳合法的 OpenAI 格式 response |
| **格式相容性** | CCR unified format 與 OpenAI standard format 皆可被 Gateway 正確處理 |
| **鏈路完整性** | 請求確實路由至正確的 upstream（Bedrock / Vertex），log 可佐證 |
| **能力覆蓋** | Streaming、Tool Use、Reasoning 三項核心能力各自驗證 |
| **回歸保護** | 新模型上線或 Gateway 版本更新後，可快速執行同一套測試確認無 regression |

---

## 二、測試的兩大對象

### 2.1 Claude Code（透過 CCR 串接）

Claude Code 本身使用 Anthropic API 格式（`/v1/messages`），透過 **CCR（Claude Code Router）** 中轉後轉換為 **CCR Unified Format**，再送至 LiteLLM。

```
Claude Code → CCR → LiteLLM → AWS Bedrock / Vertex AI
  Anthropic       CCR Unified    OpenAI-compatible
  格式             格式
```

**CCR Unified Format 與標準 OpenAI 的差異：**

| 欄位 | CCR 送給 LiteLLM 的格式 | 標準 OpenAI Chat Completions |
|---|---|---|
| Reasoning | `"reasoning": {"effort": "medium", "enabled": true}` | `"reasoning_effort": "medium"` |
| 多輪 thinking | `messages[n].thinking: {content, signature}` | 不支援 |
| Prompt cache | `messages[n].cache_control: {"type": "ephemeral"}` | 不支援 |
| 工具名稱 | `Bash`, `Read`, `Write`, `Edit`, `WebFetch` | 無標準命名 |
| 串流 | 幾乎永遠為 `true` | 依需求 |
| Temperature | `1.0`（CCR 預設） | 依需求 |

> **重要**：LiteLLM 需負責將 `reasoning` 欄位翻譯為 `reasoning_effort`，再送往 upstream。這是 CCR 路徑最關鍵的轉換點，測試必須驗證此翻譯是否正確。

---

### 2.2 Kilo Code（直接 OpenAI Compatible 串接）

Kilo Code 基於 Cline，選擇 **OpenAI Compatible** provider 後直接送出標準 OpenAI 格式，不經 CCR 轉換。

```
Kilo Code → API6 → LiteLLM → AWS Bedrock / Vertex AI
  OpenAI 標準格式（無中間轉換）
```

**Kilo 請求特徵：**

| 欄位 | Kilo 典型值 | 說明 |
|---|---|---|
| `stream` | `true` | 幾乎永遠開啟 |
| `temperature` | `0` | Kilo 預設值（低隨機性） |
| `tools` | Cline 工具集 | `execute_command`, `read_file`, `write_to_file`, `replace_in_file`, `list_files`, `search_files`, `web_search`, `attempt_completion` 等 |
| `tool_choice` | `"auto"` | 由模型決定是否呼叫工具 |
| `reasoning_effort` | `"low"/"medium"/"high"` | 標準 OpenAI 格式（非 CCR dict） |
| 系統提示 | 依 Mode 動態組合 | Code / Architect / Debug / Orchestrator 模式各有不同 |
| 多輪 tool result 截斷 | `[Old tool result content cleared]` | 超過 40,000 token 的舊 tool result 會被 Kilo 替換為此字串 |

---

## 三、測試類型與測試資料設計

### 3.1 連線驗證（Connectivity Smoke Tests）

**目的**：最快速確認 Gateway 可達、認證有效、兩條 upstream 路由存在。

| 測試項目 | 說明 | 預期結果 |
|---|---|---|
| Gateway 可達 | 送最小 payload，確認非 5xx | HTTP 200 |
| Bedrock 路由可達 | Claude Sonnet 最小請求 | 回傳有效 message |
| Vertex 路由可達 | Gemini Flash Lite 最小請求 | 回傳有效 message |
| 無效 API Key | 帶錯誤 Bearer Token | HTTP 401 / 403 |
| Kilo 客戶端連線 | 以 Kilo User-Agent 送請求 | HTTP 200 |

**測試資料範例（Bedrock 最小請求）：**
```json
{
  "model": "claude-sonnet-4.5",
  "messages": [{"role": "user", "content": "Say ok."}],
  "max_tokens": 16
}
```

---

### 3.2 Claude Code / CCR 行為測試

**目的**：驗證 Gateway + LiteLLM 可正確處理 CCR Unified Format 的所有特殊欄位。

#### T-CC-01：基本對話
```json
{
  "model": "claude-sonnet-4.5",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is 2 + 2?"}
  ],
  "max_tokens": 32,
  "temperature": 1.0
}
```
驗證：HTTP 200、回應包含 `"4"`

---

#### T-CC-02：System Prompt 生效
```json
{
  "model": "claude-sonnet-4.5",
  "messages": [
    {"role": "system", "content": "You are a pirate. Always respond in pirate speak."},
    {"role": "user", "content": "Who are you?"}
  ],
  "max_tokens": 128,
  "temperature": 1.0
}
```
驗證：回應包含海盜語氣關鍵字

---

#### T-CC-03：多輪對話記憶
```json
{
  "model": "claude-sonnet-4.5",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "My name is TestBot."},
    {"role": "assistant", "content": "Hello TestBot!"},
    {"role": "user", "content": "What is my name?"}
  ],
  "max_tokens": 64
}
```
驗證：回應包含 `"TestBot"`

---

#### T-CC-04：CCR Reasoning 格式轉換（核心驗證）

這是 CCR 路徑最重要的測試。CCR 送出的是 `reasoning` dict，但 LiteLLM 必須轉換為 `reasoning_effort` 才能讓 Bedrock 理解。

```json
{
  "model": "claude-sonnet-4.6",
  "messages": [
    {"role": "system", "content": "Think carefully."},
    {"role": "user", "content": "Solve: if x + 3 = 7, what is x?"}
  ],
  "max_tokens": 256,
  "reasoning": {"effort": "medium", "enabled": true}
}
```
驗證（雙層）：
1. **HTTP 層**：Gateway 接受此格式，回傳 HTTP 200（非 400/422）
2. **Log 層**：kubectl logs 顯示 LiteLLM 確實送出 `reasoning_effort` 給 upstream

---

#### T-CC-05：Reasoning 三個 Effort Level

| effort | `reasoning` 內容 | budget_tokens 換算 |
|---|---|---|
| `"low"` | `{"effort":"low","enabled":true}` | ≤ 1,024 tokens |
| `"medium"` | `{"effort":"medium","enabled":true}` | ≤ 8,192 tokens |
| `"high"` | `{"effort":"high","enabled":true}` | > 8,192 tokens |

驗證：三個 effort level 均回傳 HTTP 200

---

#### T-CC-06：工具調用（Tool Use）
```json
{
  "model": "claude-sonnet-4.5",
  "messages": [
    {"role": "system", "content": "You have access to tools."},
    {"role": "user", "content": "What's the weather in Tokyo?"}
  ],
  "tools": [{
    "type": "function",
    "function": {
      "name": "get_weather",
      "description": "Get current weather for a city.",
      "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"]
      }
    }
  }],
  "tool_choice": "auto",
  "max_tokens": 256
}
```
驗證：response 包含 `tool_calls`，function name 為 `get_weather`

---

#### T-CC-07：多輪 + Thinking Block（CCR 特有）

當前一輪使用了 reasoning，CCR 會在下一輪的 assistant message 中帶入 `thinking` 欄位（OpenAI 無此欄位）。

```json
{
  "model": "claude-sonnet-4.6",
  "messages": [
    {"role": "system", "content": "Think carefully."},
    {"role": "user", "content": "What is 5 + 3?"},
    {
      "role": "assistant",
      "content": "The answer is 8.",
      "thinking": {
        "content": "5+3=8",
        "signature": "1716543210000"
      }
    },
    {"role": "user", "content": "What is that answer plus 10?"}
  ],
  "max_tokens": 128,
  "reasoning": {"effort": "low", "enabled": true}
}
```
驗證：Gateway 不因 `thinking` 欄位回傳 4xx，回傳有效 response

---

#### T-CC-08：Prompt Cache（cache_control）

CCR 會在長系統提示上加入 cache_control 觸發 Anthropic prompt caching。

```json
{
  "model": "claude-sonnet-4.5",
  "messages": [
    {
      "role": "system",
      "content": "（長達數千字的系統提示...）",
      "cache_control": {"type": "ephemeral"}
    },
    {"role": "user", "content": "Summarise your role."}
  ],
  "max_tokens": 64
}
```
驗證：Gateway 不因 `cache_control` 欄位報錯

---

### 3.3 Kilo Code 行為測試

**目的**：驗證標準 OpenAI 格式透過 Gateway 正常運作，涵蓋 Kilo 的實際使用情境。

#### T-KL-01：基本對話（temperature=0）
```json
{
  "model": "claude-sonnet-4.5",
  "messages": [
    {"role": "system", "content": "You are Kilo Code, an expert software engineer..."},
    {"role": "user", "content": "What is 2 + 2?"}
  ],
  "max_tokens": 32,
  "temperature": 0,
  "stream": true
}
```
驗證：正確回傳串流 SSE chunks

---

#### T-KL-02：Cline 工具集調用
```json
{
  "model": "claude-sonnet-4.5",
  "messages": [
    {"role": "system", "content": "（Kilo Code/Ask 模式系統提示...）"},
    {"role": "user", "content": "Create a hello world Python script."}
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "execute_command",
        "description": "Execute a CLI command on the system.",
        "parameters": {
          "type": "object",
          "properties": {
            "command": {"type": "string"},
            "requires_approval": {"type": "boolean"}
          },
          "required": ["command", "requires_approval"]
        }
      }
    },
    {"type": "function", "function": {"name": "read_file", ...}},
    {"type": "function", "function": {"name": "write_to_file", ...}},
    {"type": "function", "function": {"name": "replace_in_file", ...}},
    {"type": "function", "function": {"name": "attempt_completion", ...}}
  ],
  "tool_choice": "auto",
  "stream": true,
  "max_tokens": 4096,
  "temperature": 0
}
```
驗證：回傳 `tool_calls`，finish_reason 為 `tool_calls` 或 `stop`

---

#### T-KL-03：多輪 + Tool Result 截斷

Kilo 在 context 超過 40,000 tokens 時會將舊的 tool result 替換為 `[Old tool result content cleared]`，Gateway 應正常接受此格式。

```json
{
  "model": "claude-sonnet-4.5",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "Refactor the auth module."},
    {"role": "assistant", "content": null, "tool_calls": [
      {"id": "t1", "type": "function",
       "function": {"name": "list_files", "arguments": "{\"path\":\"src/auth\"}"}}
    ]},
    {"role": "tool", "tool_call_id": "t1",
     "content": "[Old tool result content cleared]"},
    {"role": "assistant", "content": null, "tool_calls": [
      {"id": "t2", "type": "function",
       "function": {"name": "read_file", "arguments": "{\"path\":\"src/auth/login.py\"}"}}
    ]},
    {"role": "tool", "tool_call_id": "t2",
     "content": "def login(username, password):\n    ..."}
  ],
  "tools": [...],
  "stream": true,
  "max_tokens": 4096,
  "temperature": 0
}
```
驗證：Gateway 正常接受截斷的 tool result，回傳 HTTP 200

---

#### T-KL-04：Reasoning（標準 OpenAI 格式）

Kilo 直接使用 `reasoning_effort` 字串，不用 CCR 的 dict 格式。

```json
{
  "model": "claude-sonnet-4.6",
  "messages": [{"role": "user", "content": "Debug why this race condition occurs."}],
  "reasoning_effort": "medium",
  "max_tokens": 1024,
  "stream": true
}
```
驗證：HTTP 200，回傳有效 response

---

#### T-KL-05：工具調用完整 Round-Trip

1. **Turn 1**：Kilo 請求含工具定義 → 模型回傳 `tool_calls`
2. **Turn 2**：注入 tool result → 模型回傳最終文字回應

驗證：完整兩輪對話均成功，最終回應包含有意義文字

---

#### T-KL-06：Fixture-Based 真實 Payload 測試

使用從實際 Kilo 會話中抓取的原始 payload（放置於 `fixtures/kilo_payloads/`），以最貼近真實的方式重放測試。

驗證：真實 payload 重放後 Gateway 回傳 HTTP 200

---

### 3.4 Streaming SSE 測試

**目的**：驗證串流格式正確性，兩個 agent client 與兩條 upstream 路由均測試。

| 測試項目 | Client | 模型 | 驗證重點 |
|---|---|---|---|
| CC Streaming Bedrock | CCR | claude-sonnet-4.5 | chunks 數量 > 1，內容非空 |
| CC Streaming Vertex | CCR | gemini-3.1-flash-lite-preview | chunks 數量 > 1 |
| Kilo Streaming Bedrock | Kilo | claude-sonnet-4.5 | 最終 chunk 包含 usage 統計 |
| Streaming Chunk Schema | Kilo | claude-sonnet-4.5 | 每個 chunk 符合 OpenAI SSE schema |
| Finish Reason | Kilo | claude-sonnet-4.5 | 最後一個 chunk 的 finish_reason = "stop" |

**SSE Chunk 驗證規則：**
- 每個 `data:` 行可被解析為合法 JSON
- 包含 `id`, `object: "chat.completion.chunk"`, `choices` 欄位
- 最終 chunk `finish_reason` 為 `"stop"` 或 `"tool_calls"`
- `[DONE]` 訊號正確出現

---

### 3.5 全模型矩陣測試（Model Matrix）

**目的**：每個在 `config/models.yaml` 中註冊的模型，均自動跑三項基礎測試。新模型上線只需加一筆 YAML entry，無需修改程式碼。

| 測試 | 適用模型 | 說明 |
|---|---|---|
| `test_basic_chat_all_models` | 全部 6 個模型 | 最小 chat 請求，驗證路由正確 |
| `test_streaming_all_capable_models` | 6 個（全部支援） | SSE streaming 正常回傳 |
| `test_tool_use_all_capable_models` | 6 個（全部支援） | tool_calls 或 stop finish_reason |
| `test_log_routing_all_models` | 全部 6 個模型 | kubectl log 確認 model_id 出現 |

**當前模型矩陣：**

```
claude-sonnet-4.5   ── basic ✓  streaming ✓  tool_use ✓  reasoning ✓
claude-sonnet-4.6   ── basic ✓  streaming ✓  tool_use ✓  reasoning ✓
claude-opus-4.6     ── basic ✓  streaming ✓  tool_use ✓  reasoning ✓
claude-opus-4.7     ── basic ✓  streaming ✓  tool_use ✓  reasoning ✓
gemini-3-pro-preview── basic ✓  streaming ✓  tool_use ✓  reasoning ✓
gemini-3.1-flash-lite-preview
                    ── basic ✓  streaming ✓  tool_use ✓
```

---

### 3.6 Reasoning / Extended Thinking 測試

**目的**：驗證 reasoning 功能在兩條路徑（CCR / Kilo）上均可正常運作，並確認 LiteLLM 翻譯邏輯正確。

| 測試項目 | 路徑 | 格式 | Effort |
|---|---|---|---|
| `test_cc_reasoning_low` | CCR | `reasoning: {effort:"low"}` | low |
| `test_cc_reasoning_medium` | CCR | `reasoning: {effort:"medium"}` | medium |
| `test_cc_reasoning_high` | CCR | `reasoning: {effort:"high"}` | high |
| `test_kilo_reasoning_effort` | Kilo | `reasoning_effort: "medium"` | medium |
| `test_reasoning_all_capable_models` | Kilo | `reasoning_effort: "low"` | low × 5 models |
| `test_ccr_reasoning_translated_in_logs` | CCR + kubectl | Log assertion | low |

**CCR Reasoning 翻譯流程：**
```
CCR 送出                LiteLLM 翻譯            Upstream 收到
reasoning: {        →   reasoning_effort:   →   適當的 provider
  effort: "medium",       "medium"               參數
  enabled: true
}
```

---

## 四、測試驗證策略

### 4.1 雙層驗證

每個測試都在兩個維度進行驗證：

```
第一層：HTTP Response 驗證
  ├── Status Code = 200
  ├── Response Schema 符合 OpenAI 規範
  ├── choices[0].message.content 非空
  └── finish_reason 合法

第二層：kubectl Log 驗證（ENABLE_LOG_VALIDATION=true 時）
  ├── 正確的 model_id 出現在 LiteLLM log
  ├── upstream provider（bedrock/vertex）被呼叫
  ├── CCR reasoning 欄位已被翻譯為 reasoning_effort
  └── 無 ERROR/Exception 字樣
```

### 4.2 Fixture-Based vs. Programmatic

| 方式 | 說明 | 適用場景 |
|---|---|---|
| **Programmatic** | 用 `ClaudeCodeClient` / `KiloClient` 的 builder 方法組裝 payload | 快速驗證特定欄位行為 |
| **Fixture-Based** | 從真實會話抓取的原始 payload 存於 `fixtures/` 直接重放 | 最貼近真實場景，適合回歸測試 |

---

## 五、測試案例總覽

| 類別 | 測試數量 | 標記 |
|---|---|---|
| 連線驗證 | 5 | `connectivity` |
| Claude Code / CCR | 9 | `claude_code` |
| Kilo Code | 7 | `kilo` |
| 全模型矩陣（基本） | 6 個模型 | `bedrock` / `vertex` |
| 全模型矩陣（streaming） | 6 個模型 | `streaming` |
| 全模型矩陣（tool_use） | 6 個模型 | `tool_use` |
| Streaming 詳細驗證 | 5 | `streaming` |
| 工具調用 Round-Trip | 4 | `tool_use` |
| Reasoning（單模型） | 6 | `reasoning` |
| Reasoning（全模型） | 5 個模型 | `reasoning`, `slow` |
| **合計（含矩陣展開）** | **約 60+ 個 test case** | |

---

## 六、測試範圍與限制

### 涵蓋範圍

- ✅ 完整鏈路（API6 → LiteLLM → Bedrock/Vertex）
- ✅ 兩個 AI Agent（Claude Code via CCR、Kilo Code）
- ✅ 全部 6 個已上線模型
- ✅ CCR 特有欄位轉換正確性（reasoning、thinking、cache_control）
- ✅ Streaming SSE 格式正確性
- ✅ Tool Use 完整 round-trip
- ✅ Extended Thinking / Reasoning（三個 effort level）
- ✅ kubectl log 二次驗證路由與翻譯

### 不在本次範圍（後續可擴充）

- ⬜ 負載測試 / 壓力測試（concurrent requests）
- ⬜ 錯誤注入測試（upstream 故障時的 fallback 行為）
- ⬜ Token 計費精確度驗證
- ⬜ Vision / 圖片輸入（架構支援但尚未在 fixtures 中加入）
- ⬜ MCP Tool 串接測試

---

## 七、新模型上線檢核流程

當有新模型需要上線時，只需三步驟：

```
1. 在 config/models.yaml 新增模型 entry
   ↓
2. 執行 make models 跑全模型矩陣
   ↓
3. 若有 reasoning 能力，在 capabilities 加上 reasoning
   並執行 make reasoning 確認翻譯正確
```

不需修改任何測試程式碼，大幅降低維護成本。
