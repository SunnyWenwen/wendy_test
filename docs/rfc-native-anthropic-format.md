# RFC: Native Anthropic Format Support Architecture Design

> **狀態：** 提案中  
> **日期：** 2026-05-31  
> **作者：** Platform Team  

---

## 目錄

1. [摘要](#摘要)
2. [問題陳述](#問題陳述)
3. [建議方案](#建議方案)
4. [詳細設計](#詳細設計)
5. [缺點](#缺點)
6. [替代方案比較](#替代方案比較)

---

## 摘要

目前平台提供 OpenAI 相容格式的 LLM API，架構為：

```
User (OpenAI format) → APISIX → llm-proxy (LiteLLM SDK) → AWS Bedrock Claude
```

APISIX 已強綁定 `ai-rate-limiting`（含 Redis TPM limit）、`ai-proxy-multi`（failover/roundrobin/chash）及 `file-logger`。

現在需要支援 **Native Anthropic Messages API 格式**（`/v1/messages`）。本 RFC 評估兩種實作方案，並提出建議架構。

---

## 問題陳述

### 核心限制：APISIX 3.15/3.16 的 TPM limit 缺陷

APISIX 3.15/3.16 的 `ai-proxy-multi` 僅能解析 **OpenAI format** 的 response 來填入 `ctx.ai_token_usage`。當 response 為 Anthropic format（含 `input_tokens` / `output_tokens`）時，token 提取失敗：

```lua
-- ai-rate-limiting.lua log phase
local function get_token_usage(conf, ctx)
    local usage = ctx.ai_token_usage  -- 為 nil（APISIX 無法解析 Anthropic format）
    if not usage then
        core.log.error("failed to get token usage for llm service")
        return  -- 直接 return，token 從不被扣
    end
    return usage[conf.limit_strategy]
end
```

TPM limit 分兩個階段：

| 階段 | 動作 | 問題 |
|------|------|------|
| access phase | Redis 讀取檢查（read-only） | ✅ 正常（cost=0）|
| **log phase** | **實際扣 token 數（write）** | ❌ `ctx.ai_token_usage = nil`，永遠不扣 |

**結論：只要 APISIX 收到的 response 是 Anthropic format，TPM limit 失效，rate limiting 無法正確運作。**

### APISIX 3.17 的狀態

PR #13181（`feat(ai-proxy): add native Anthropic Messages API protocol support`）已於 2026-04-09 合併進 master，但晚於 3.16.0 發布日（2026-04-07）兩天，預計落在 **3.17.0**（尚未發布）。

### 需求整理

- 支援 Native Anthropic `/v1/messages` 格式（含 streaming SSE）
- **TPM limit 不能有誤差**
- 支援 failover（roundrobin / chash）
- 自定義 header（`t-request-id`、`Authorization: Bearer xxx`）必須從 user request 往後帶給 APISIX
- 維持現有 OpenAI format 流量完全不受影響

---

## 建議方案

**在 APISIX 前面部署一個 LiteLLM Python 轉換層（CCR 角色），使用現有 llm-proxy repo 的 LiteLLM SDK 實作。**

### 架構要求

APISIX log phase 要能正確扣 token，其硬性條件為：

```
APISIX 看到的 request  → OpenAI format
APISIX 看到的 response → OpenAI format  ← 才能填 ctx.ai_token_usage
```

因此 Anthropic ↔ OpenAI 的雙向轉換，必須在 APISIX 的前後都完成，APISIX 全程只看 OpenAI format。

### 目標架構

```
┌─────────────────────────────────────────────────────────────────┐
│                        Virtual Service                          │
│  path: /v1/chat/completions  ──────────────────────────────┐   │
│  path: /v1/messages          ──────────────────────┐       │   │
└──────────────────────────────────────────────────┬─┘───────┼───┘
                                                   │         │
                                                   ▼         │
                              ┌──────────────────────────┐   │
                              │  LiteLLM-CCR             │   │
                              │  （基於 llm-proxy repo）  │   │
                              │  port: 8000              │   │
                              │  - 接收 Anthropic format  │   │
                              │  - 轉換為 OpenAI format   │   │
                              │  - 轉發 custom headers    │   │
                              │  - 回轉 Anthropic format  │   │
                              └────────────┬─────────────┘   │
                                           │ /v1/chat/completions
                                           │ + t-request-id
                                           │ + Authorization: Bearer xxx
                                           ▼                 │
                              ┌──────────────────────────┐   │
                              │         APISIX            │◄──┘
                              │  ai-rate-limiting (TPM)   │
                              │  ai-proxy-multi (failover) │
                              │  file-logger              │
                              │  全程看 OpenAI format ✅   │
                              └────────────┬─────────────┘
                                           │
                                           ▼
                              ┌──────────────────────────┐
                              │  llm-proxy (現有，不動)   │
                              │  LiteLLM SDK             │
                              │  port: 8000              │
                              └────────────┬─────────────┘
                                           │
                                           ▼
                                    AWS Bedrock Claude
```

### Request / Response 流程

```
User
 │  POST /v1/messages
 │  Headers:
 │    t-request-id: req-abc123
 │    Authorization: Bearer <user-token>
 │  Body: { "model": "claude-3-5-sonnet", "messages": [...] }  ← Anthropic format
 ▼
Virtual Service
 │  path routing: /v1/messages → LiteLLM-CCR:8000
 ▼
LiteLLM-CCR（llm-proxy repo，新 instance）
 │  1. 接收 Anthropic format request
 │  2. 提取 custom headers（t-request-id、Authorization）
 │  3. 透過 AnthropicAdapter 轉換為 OpenAI format
 │  4. 呼叫 APISIX:9080/v1/chat/completions
 │     Headers:
 │       t-request-id: req-abc123（帶入）
 │       Authorization: Bearer <user-token>（帶入）
 ▼
APISIX
 │  access phase:
 │    ai-rate-limiting: 讀 Redis，檢查 TPM limit（✅ OpenAI format，正常）
 │    ai-proxy-multi: 選 instance（failover/roundrobin）
 │  forward to llm-proxy
 ▼
llm-proxy (LiteLLM SDK) → AWS Bedrock
 │  回傳 OpenAI format response
 ▼
APISIX
 │  log phase:
 │    ctx.ai_token_usage 由 ai-proxy-multi 從 OpenAI response 填入 ✅
 │    ai-rate-limiting: 扣實際 token 數 ✅
 ▼
LiteLLM-CCR
 │  5. 收到 OpenAI format response
 │  6. 轉換回 Anthropic format
 │     （含 streaming: OpenAI SSE → Anthropic SSE 逐 chunk 轉換）
 ▼
User
   收到 Native Anthropic format response ✅
```

---

## 詳細設計

### LiteLLM-CCR 實作

基於現有 **llm-proxy repo** 的 LiteLLM SDK，部署第二個 instance，`api_base` 指向 APISIX 而非 Bedrock。兩個 instance 使用相同 port 8000，部署在不同 container。

#### config.yaml（LiteLLM proxy 模式）

```yaml
model_list:
  - model_name: claude-3-5-sonnet-20241022
    litellm_params:
      model: openai/claude-3-5-sonnet-20241022  # openai/ 前綴 = 打任何 OAI-compatible endpoint
      api_base: http://apisix-internal:9080
      api_key: "${APISIX_CONSUMER_KEY}"

  - model_name: claude-opus-4-5
    litellm_params:
      model: openai/claude-opus-4-5
      api_base: http://apisix-internal:9080
      api_key: "${APISIX_CONSUMER_KEY}"

litellm_settings:
  drop_params: true  # top_k 等 OpenAI 不支援的參數自動忽略
  forward_pass_through_headers:
    - t-request-id      # request 追蹤 ID
    - authorization     # Bearer token，帶給 APISIX 做驗證
```

#### 或 SDK 模式（Python FastAPI，基於 llm-proxy repo）

```python
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import litellm
import os

app = FastAPI()

APISIX_BASE_URL = "http://apisix-internal:9080"
APISIX_KEY = os.environ["APISIX_CONSUMER_KEY"]

# 需要往後帶給 APISIX 的 custom headers
FORWARD_HEADERS = ["t-request-id", "authorization"]

@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    body = await request.json()
    is_streaming = body.get("stream", False)

    # 提取並轉發 custom headers
    extra_headers = {
        h: request.headers[h]
        for h in FORWARD_HEADERS
        if h in request.headers
    }
    # 若 Authorization 未帶入則補上 APISIX consumer key
    if "authorization" not in extra_headers:
        extra_headers["authorization"] = f"Bearer {APISIX_KEY}"

    # LiteLLM AnthropicAdapter 轉換，打 APISIX（OpenAI format）
    response = await litellm.acompletion(
        model="openai/" + body["model"],
        messages=body["messages"],
        api_base=APISIX_BASE_URL,
        extra_headers=extra_headers,
        stream=is_streaming,
        **{k: v for k, v in body.items()
           if k not in ("model", "messages", "stream")}
    )

    if is_streaming:
        return StreamingResponse(
            convert_openai_stream_to_anthropic(response),
            media_type="text/event-stream"
        )

    return convert_openai_response_to_anthropic(response)
```

### Virtual Service 路由設定

```yaml
# 只新增一條 path rule，現有 OpenAI 流量完全不動
spec:
  http:
  - match:
    - uri:
        prefix: /v1/messages
    route:
    - destination:
        host: litellm-ccr
        port:
          number: 8000
  - match:
    - uri:
        prefix: /v1/chat/completions    # 現有，不動
    route:
    - destination:
        host: apisix
        port:
          number: 9080
```

### LiteLLM 版本確認

已確認 LiteLLM **1.83.14** 完整支援：

| 功能 | 狀態 | 引入版本 |
|------|------|----------|
| `/v1/messages` endpoint | ✅ | ~v1.40（PR #4635，2024-07） |
| Streaming SSE 雙向轉換 | ✅ | ~v1.40 |
| openai-compatible backend | ✅ | PR #12016（2025-06） |
| thinking / tool_use 轉換 | ✅ | 持續維護 |
| extra_headers 轉發 | ✅ | 內建支援 |

⚠️ **已知 issue #23841**：`input_text` content block 在特定路徑下可能被靜默丟棄，使用前建議測試此 edge case。

### 升版退場計畫

當 APISIX 3.17.0 正式發布後：

1. 在 APISIX 新增一條 `/v1/messages` route，`provider: anthropic`
2. Virtual Service 將 `/v1/messages` 改指向 APISIX（不再經過 LiteLLM-CCR）
3. 下架 LiteLLM-CCR instance
4. LiteLLM-CCR 的程式碼可保留但不部署，作為備援

---

## 缺點

### LiteLLM-CCR 方案的缺點

| 缺點 | 說明 | 緩解措施 |
|------|------|----------|
| 多一個網路 hop | 每個 Anthropic format 請求多經過一層 | LiteLLM-CCR 部署在同一 cluster，延遲影響 < 5ms |
| 需維護第二個 instance | 多一個服務要監控、部署 | 使用與 llm-proxy 相同的 Docker image，僅 config 不同 |
| 兩次格式轉換 | Anthropic→OpenAI（CCR）+ OpenAI→Bedrock（現有）| 轉換皆為 in-memory，CPU 成本極低 |
| streaming 轉換複雜 | OpenAI SSE ↔ Anthropic SSE 格式差異大 | LiteLLM 1.83.14 已內建處理，非自行實作 |
| 臨時方案 | 等 APISIX 3.17 後需退場 | 退場流程簡單，Virtual Service 改一條 rule |

---

## 替代方案比較

### 方案 A：Claude Code Router（musistudio/claude-code-router）

#### 功能評估

| 評估面向 | 結論 |
|---------|------|
| **設計目的** | ❌ 專為個人 Claude Code CLI 省錢設計，非生產 API gateway |
| Anthropic↔OpenAI 雙向轉換 | ✅ 完整支援（AnthropicTransformer pipeline） |
| Streaming SSE 轉換 | ✅ 完整支援 |
| thinking / tool_use / top_k | ✅ 有對應 transformer |
| **Custom header forwarding** | ❌ 無此功能，需 fork 修改原始碼 |
| AWS Bedrock 原生支援 | ❌ 僅透過 openai-compatible 間接支援 |
| 多租戶並發穩定性 | ❓ 未設計此場景，未經驗證 |
| 技術棧 | Node.js / TypeScript / Fastify（與現有 Python stack 異質） |
| 維護風險 | 高（個人 OSS 專案，無企業支撐，patch 需持續 rebase） |
| **結論** | ❌ **不建議** |

#### CCR 能否獨立打包成一個模組？

技術上可行，但代價不低，以下逐點說明：

**1. 需要 fork 並大幅裁剪原始碼**

CCR 的格式轉換邏輯（`AnthropicTransformer`、`ToolUseTransformer`、`ReasoningTransformer` 等）分散在多個 TypeScript 檔案，且與 Claude Code CLI 的 routing 邏輯緊耦合（background / thinking / longContext router rules、Web UI `/ui/`、PM2 process manager、preset marketplace 等）。要獨立打包需要手動提取 transformer 模組，去掉所有 CLI 專用邏輯，工程量不小。

**2. 仍需新增 custom header forwarding**

CCR 的 Fastify server 沒有從 incoming request 動態轉發 header 給 backend 的功能。即使打包後，仍需手動在 request handler 中加入：

```typescript
// 需要手動 patch
const incomingHeaders = request.headers;
outgoingHeaders['t-request-id'] = incomingHeaders['t-request-id'];
outgoingHeaders['authorization'] = incomingHeaders['authorization'];
```

**3. 技術棧異質**

CCR 是 Node.js / TypeScript，與現有 Python stack 不同，意味著需要額外維護 Node.js 的部署流程、監控體系、Dockerfile，以及團隊需要具備 TypeScript 維護能力。

**4. 上游同步問題**

當 CCR upstream 修復 streaming bug、新增新的 Anthropic 模型格式支援時（例如 claude-opus-4 的新參數），你的 fork 需要手動 merge。隨著時間拉長，fork 與 upstream 的差距越來越大，維護成本持續累積。相比之下，LiteLLM 透過 `pip upgrade` 即可取得所有修復。

**5. 多租戶並發未驗證**

CCR 設計為單一 Claude Code CLI 用戶使用，沒有多租戶高並發的壓力測試數據或對應的設計（如 connection pool、rate limit、graceful shutdown 等）。

**結論：即使將 CCR 打包成獨立模組，仍比 LiteLLM 方案需要更多工程投入與長期維護成本，且無法解決技術棧異質的問題。**

---

### 方案 B：LiteLLM SDK / Proxy（**建議採用**）

| 評估面向 | 結論 |
|---------|------|
| **設計目的** | ✅ 生產 LLM gateway，企業級 |
| Anthropic↔OpenAI 雙向轉換 | ✅ 內建 AnthropicAdapter |
| Streaming SSE 轉換 | ✅ 內建 AnthropicStreamWrapper |
| thinking / tool_use / top_k | ✅ `drop_params: true` 自動處理 |
| **Custom header forwarding** | ✅ `forward_pass_through_headers` config 或 `extra_headers` SDK 參數 |
| AWS Bedrock 原生支援 | ✅（現有 llm-proxy 已使用）|
| 多租戶並發穩定性 | ✅ 設計目標之一 |
| 技術棧 | ✅ Python（與現有 llm-proxy 相同）|
| 維護風險 | 低（BerriAI 商業版支撐，v1.83.14 已穩定）|
| 已在 stack 中 | ✅ llm-proxy repo 現有服務，無新依賴 |
| **結論** | ✅ **建議採用** |

---

### 方案 C：等待 APISIX 3.17.0

| 評估面向 | 結論 |
|---------|------|
| 實作成本 | ✅ 零，只需升版並新增 route config |
| 時程 | ❌ 發布日期未知（PR 已 merge 進 master，但無確定 ETA）|
| 風險 | ✅ 最低（官方支援）|
| **結論** | 可作為長期方案，但無法滿足當前需求 |

---

### 方案比較總表

| | CCR（含打包）| **LiteLLM SDK** | APISIX 3.17 |
|--|-------------|-----------------|-------------|
| TPM limit 準確 | ✅ | ✅ | ✅ |
| Custom headers | ❌ 需 fork + patch | ✅ | ✅ |
| Streaming | ✅ | ✅ | ✅ |
| 現有技術棧 | ❌ Node.js | ✅ Python | ✅ |
| 生產驗證 | ❌ | ✅ | ✅ |
| 可立即部署 | ❌（需 fork + 裁剪）| ✅ | ❌（待發布）|
| 維護負擔 | 高 | 低 | 無 |
| **建議** | ❌ | **✅ 採用** | 升版後替換 |

---

## 決策

**採用方案 B：LiteLLM SDK（llm-proxy repo）作為 Anthropic Format Converter（CCR 角色）。**

基於現有 llm-proxy repo 部署一個新的 LiteLLM instance（`litellm-ccr`，port 8000），`api_base` 指向 APISIX，負責 Anthropic ↔ OpenAI 雙向格式轉換，並轉發 `t-request-id`、`Authorization` 等 custom headers。Virtual Service 新增 `/v1/messages` path rule 指向此 instance，現有 OpenAI 流量完全不受影響。

當 APISIX 3.17.0 發布後，執行退場計畫，將轉換責任移交 APISIX 原生支援。
