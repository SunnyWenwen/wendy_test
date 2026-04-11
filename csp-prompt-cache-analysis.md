# Prompt Cache 技術文件
**GCP Gemini · AWS Bedrock Claude · Azure OpenAI**
*April 2026*

---

## 一、Prompt Cache 機制總覽

各平台支援的快取方式分為兩種：
- **隱式快取（Implicit Caching）**：平台自動偵測重複前綴，無需開發者設定
- **明確快取（Explicit Caching）**：開發者手動標記要快取的區段

| 平台 | 隱式快取 | 明確快取 | 最低 Token 門檻 | 快取時效（TTL） |
|---|---|---|---|---|
| GCP Gemini 2.5 Flash | ✅ 預設開啟 | ✅ 支援 | 1,024 tokens | 預設 1 小時（可設定） |
| GCP Gemini 2.5 Pro | ✅ 預設開啟 | ✅ 支援 | 2,048 tokens | 預設 1 小時（可設定） |
| AWS Bedrock Claude 3.7 Sonnet | ❌ 不支援 | ✅ 需手動設定 | 1,024 tokens | 5 分鐘（預設） |
| AWS Bedrock Claude Sonnet 4.5 | ❌ 不支援 | ✅ 需手動設定 | 1,024 tokens | 5 分 / 1 小時 |
| AWS Bedrock Claude Haiku 4.5 | ❌ 不支援 | ✅ 需手動設定 | 4,096 tokens | 5 分 / 1 小時 |
| Azure OpenAI（GPT-4o+） | ✅ 自動啟用 | ❌ 無此機制 | 1,024 tokens | 5–10 分鐘 |

### 1.1 GCP Gemini — 隱式快取

Gemini 2.5 系列預設啟用隱式快取，無需任何程式碼修改。當新的請求與之前某次請求共享相同的開頭前綴（common prefix），系統自動命中快取並回傳 75% 折扣。

- 快取由 Google 伺服器端自動偵測，開發者無法指定「哪一段要快取」
- 僅需將靜態內容（system prompt、文件）放在請求最前面，動態內容（使用者問題）放最後
- 明確快取（Explicit Cache）：需透過 API 建立 CachedContent 物件，並在後續請求中引用，可保證節省並自訂 TTL

### 1.2 AWS Bedrock Claude — 僅支援明確快取

Bedrock 上的 Claude 目前不支援隱式快取，必須由開發者在請求中明確標記 cache checkpoint。Anthropic 直接 API 已支援自動快取，但 Bedrock 版本尚未上線。

- 需在 `system`、`messages` 或 `tools` 欄位中插入 `cachePoint`（Converse API）或 `cache_control`（InvokeModel API）
- 最多 4 個 cache checkpoint，每個 checkpoint 之前的前綴長度必須達到最低 token 門檻
- TTL 預設 5 分鐘；Claude Sonnet 4.5、Haiku 4.5、Opus 4.5 支援延長至 1 小時

### 1.3 Azure OpenAI — 隱式自動快取

Azure OpenAI（GPT-4o 以上）的 prompt caching 完全自動，無需設定 `cache_control` 欄位。系統根據 prompt 前綴的 hash 值進行路由，相同前綴命中同一台機器即可享有快取。

- 無法手動標記「哪一段要快取」，僅能透過提示結構最佳化來提升命中率
- 可選用 `prompt_cache_key` 參數影響路由，提升命中率（詳見第四節）
- 可選用 `prompt_cache_retention: "24h"` 延長快取時效至 24 小時

---

## 二、明確快取的 API 設定方式

### 2.1 GCP Gemini — Explicit Cache

Gemini 的明確快取需要先建立 CachedContent 物件，再在後續請求中引用：

**步驟 1：建立 CachedContent**

```python
from google import genai
from google.genai import types

client = genai.Client()

# 建立快取（將大型靜態文件放入）
cache = client.caches.create(
    model='gemini-2.5-flash',
    contents=[types.Content(
        parts=[types.Part(text='[長篇靜態文件內容...]')],
        role='user'
    )],
    ttl='3600s',  # 1 小時
    display_name='my-doc-cache'
)
```

**步驟 2：在請求中引用 cache**

```python
response = client.models.generate_content(
    model='gemini-2.5-flash',
    contents='請根據文件回答：合約的主要條款是什麼？',
    config=types.GenerateContentConfig(
        cached_content=cache.name  # 引用快取
    )
)
```

### 2.2 AWS Bedrock Claude — 兩種 API 格式

#### 方式一：Converse API（推薦）

使用 `cachePoint` 物件插入在想要快取的內容之後：

```python
import boto3

client = boto3.client('bedrock-runtime', region_name='us-east-1')

response = client.converse(
    modelId='anthropic.claude-3-7-sonnet-20250219-v1:0',
    system=[
        { 'text': '你是法律文件分析助理。[...長篇靜態內容...]' },
        { 'cachePoint': { 'type': 'default' } }   # ← 快取 system
    ],
    messages=[{
        'role': 'user',
        'content': [
            { 'text': '合約主要條款是什麼？' }
        ]
    }]
)
```

若需要 1 小時 TTL（支援 Sonnet 4.5 / Haiku 4.5 / Opus 4.5）：

```python
{ 'cachePoint': { 'type': 'default', 'ttl': '1h' } }
```

#### 方式二：InvokeModel API（格式與 Anthropic 直接 API 相似）

```python
body = {
    'anthropic_version': 'bedrock-2023-05-31',
    'system': '你是法律文件分析助理。',
    'messages': [{
        'role': 'user',
        'content': [
            {
                'type': 'text',
                'text': '[長篇靜態內容...]',
                'cache_control': { 'type': 'ephemeral' }  # ← 快取標記
            },
            { 'type': 'text', 'text': '合約主要條款是什麼？' }
        ]
    }],
    'max_tokens': 1024
}
```

**確認快取是否命中（從 response 的 usage 欄位）：**

```python
usage = response['usage']
print(usage['cacheReadInputTokens'])   # > 0 表示命中快取
print(usage['cacheWriteInputTokens'])  # > 0 表示首次寫入
```

### 2.3 Azure OpenAI — 無明確快取設定（全自動）

Azure OpenAI 不提供明確快取機制，快取完全自動。開發者只能透過結構最佳化和 `prompt_cache_key` 來提升命中率：

```python
response = client.chat.completions.create(
    model='gpt-4o',
    messages=[
        { 'role': 'system', 'content': '[靜態 system prompt...]' },
        { 'role': 'user',   'content': user_question }
    ],
    # 可選參數：影響路由，提升相同前綴的命中率
    extra_body={
        'prompt_cache_key': 'my-system-v1',
        'prompt_cache_retention': '24h'
    }
)
```

---

## 三、快取觸發的最低 Token 門檻

各平台對不同模型有不同的最低 token 要求，未達門檻的請求仍會執行但不會寫入快取：

| 平台 / 模型 | 最低 tokens | 最多 checkpoints | 可快取欄位 |
|---|---|---|---|
| GCP Gemini 2.5 Flash | 1,024 | — | 全部前綴（隱式） |
| GCP Gemini 2.5 Pro | 2,048 | — | 全部前綴（隱式） |
| Bedrock Claude 3.7 Sonnet | 1,024 | 4 | system, messages, tools |
| Bedrock Claude Sonnet 4 / 4.5 | 1,024 | 4 | system, messages, tools |
| Bedrock Claude Haiku 4.5 | 4,096 | 4 | system, messages, tools |
| Bedrock Claude Opus 4 / 4.1 / 4.5 | 1,024 / 1,024 / 4,096 | 4 | system, messages, tools |
| Azure OpenAI GPT-4o+ | 1,024 | — | 整個 prompt 前綴（自動） |

> ⚠️ **重要提醒**：token 計數包含整個前綴（system + messages），而非單一欄位。若總前綴未達門檻，即使加了 `cache_control` 標記，該次請求仍不會寫入快取（但不會報錯）。

---

## 四、效益評估：以 AWS Bedrock Claude 為例

### 4.1 前提假設與定價基準

以 AWS Bedrock Claude 3.7 Sonnet（us-east-1）為計算基準：

| 費用類型 | 單價（per 1M tokens） | 說明 |
|---|---|---|
| 標準輸入（Cache Miss） | $3.00 | 未命中快取，正常計費 |
| Cache Write（首次寫入） | $3.75 | 約為標準的 1.25x |
| Cache Read（命中快取） | $0.30 | 約為標準的 0.10x，省 90% |

**情境設定：**
- System prompt 大小：**3,000 tokens**（含工具定義、產品手冊等）
- 每次使用者問題：約 100 tokens（不計入快取計算）
- Load balance 輪詢：**3 個 API endpoint**（API A / B / C）
- TTL：5 分鐘（預設）

每 3,000 tokens 的 system prompt 費用：
- 標準費用（無快取）：3,000 × $3.00 / 1,000,000 = **$0.009 / 次**
- Cache Write 費用：3,000 × $3.75 / 1,000,000 = **$0.01125 / 次**
- Cache Read 費用：3,000 × $0.30 / 1,000,000 = **$0.0009 / 次**

---

### 4.2 情境一：輪詢 3 個 API（現況）

因為 cache 存在特定節點上，每個 API endpoint 各自維護獨立的 cache pool：

```
Turn 1 → API A → Cache Write（$0.01125）
Turn 2 → API B → Cache Write（$0.01125）
Turn 3 → API C → Cache Write（$0.01125）
Turn 4 → API A → Cache Read（$0.0009）
Turn 5 → API B → Cache Read（$0.0009）
Turn 6 → API C → Cache Read（$0.0009）...
```

**損益平衡計算（每個 API endpoint 各自計算）：**

| Turn 數（每個 API） | 累積費用（輪詢） | 累積費用（無快取） | 是否划算 |
|---|---|---|---|
| Turn 1（Write） | $0.01125 | $0.009 | ❌ 虧 $0.00225 |
| Turn 2（Read） | $0.01215 | $0.018 | ❌ 仍虧 |
| Turn 3（Read） | $0.01305 | $0.027 | ✅ 開始省錢 |
| Turn 5（Read） | $0.01485 | $0.045 | ✅ 省 $0.03015 |
| Turn 10（Read） | $0.02025 | $0.090 | ✅ 省 $0.06975 |
| Turn 20（Read） | $0.02925 | $0.180 | ✅ 省 $0.15075 |

> 📌 **結論**：輪詢 3 個 API 時，每個 API endpoint 各需要 **3 次以上**的請求才能回本（總共 **9 次請求**後才開始划算）。若使用者每次對話輪數少於 3 次，使用快取反而更貴。

---

### 4.3 情境二：使用 Sticky Session（最佳化）

改為 Sticky Session：同一個 user 的所有請求都路由到同一個 API endpoint：

```
User A → 永遠打 API A → Turn 1 Write, Turn 2+ Read ✅
User B → 永遠打 API B → Turn 1 Write, Turn 2+ Read ✅
User C → 永遠打 API C → Turn 1 Write, Turn 2+ Read ✅
```

實作方式：
```python
# 用 user_id hash 決定要用哪個 API
api_index = hash(user_id) % len(api_keys)
api_key = api_keys[api_index]
```

**損益平衡計算（Sticky Session）：**

| Turn 數（每個 User） | 累積費用（Sticky） | 累積費用（無快取） | 節省 |
|---|---|---|---|
| Turn 1（Write） | $0.01125 | $0.009 | ❌ 虧 $0.00225 |
| Turn 2（Read） | $0.01215 | $0.018 | ✅ 省 $0.00585 |
| Turn 3（Read） | $0.01305 | $0.027 | ✅ 省 $0.01395 |
| Turn 5（Read） | $0.01485 | $0.045 | ✅ 省 $0.03015 |
| Turn 10（Read） | $0.02025 | $0.090 | ✅ 省 $0.06975 |
| Turn 20（Read） | $0.02925 | $0.180 | ✅ 省 $0.15075 |

> 📌 **結論**：Sticky Session 下，**第 2 次請求就開始回本**。100 次對話可省約 95% 費用（無快取 $0.9 → 快取 $0.047）。

---

### 4.4 情境三：Azure OpenAI 使用 prompt_cache_key

Azure OpenAI 的 `prompt_cache_key` 讓你告訴平台「這些請求使用相同前綴，請盡量路由到同一台機器」。它不保證命中，但能顯著改善輪詢時的命中率。

**Azure OpenAI GPT-4o 定價（參考）：**
- 標準輸入：$2.50 / 1M tokens
- Cache Read：$1.25 / 1M tokens（省 50%）
- 無 Cache Write 額外費用

```python
response = client.chat.completions.create(
    model='gpt-4o',
    messages=[...],
    extra_body={
        'prompt_cache_key': f'user-{user_id}',  # 每個 user 固定的 key
        'prompt_cache_retention': '24h'          # 延長 TTL 到 24 小時
    }
)
```

**效益比較（3,000 tokens system prompt，100 次對話）：**

| 情境 | 命中率（估計） | 每次費用 | 100 次總費用 |
|---|---|---|---|
| 無快取 | 0% | $0.0075 | $0.75 |
| 輪詢（無 cache_key） | ~33% | ~$0.0050 | ~$0.50 |
| prompt_cache_key（user sticky） | ~90%+ | ~$0.0022 | ~$0.22 |
| prompt_cache_key + 24h retention | ~95%+ | ~$0.0015 | ~$0.15 |

> 📌 **結論**：Azure OpenAI 使用 `prompt_cache_key` 搭配 `24h retention`，100 次對話可省約 **80%** 費用。相比 AWS Bedrock，Azure 無需 cache write 費用，折扣直接（50%），適合高頻短對話場景。

---

## 五、最佳實踐建議

### 5.1 Prompt 結構設計原則

無論使用哪個平台，最佳化快取命中率的核心原則相同：

| 位置 | 放什麼內容 | 說明 |
|---|---|---|
| 最前面（靜態） | System prompt、工具定義、產品手冊、範本 | 所有 user 共享，cache 效益最大 |
| 中間（半靜態） | 使用者角色、偏好設定（若相對固定） | 視情況決定是否納入快取範圍 |
| 最後面（動態） | 使用者問題、當前時間、個人化資訊 | 每次都不同，不應放在前綴 |

### 5.2 Load Balance 策略建議

- **優先使用 Sticky Session by user_id**：同一 user 永遠打同一個 API endpoint，快取效益最佳
- **Azure OpenAI 使用 `prompt_cache_key=user_id`**：利用路由 hint 改善輪詢場景的命中率
- **Bedrock 若必須輪詢**：評估對話是否 ≥ 3 輪，否則關閉快取反而省錢
- **搭配延長 TTL**：Azure 用 `prompt_cache_retention="24h"`；Bedrock 新模型用 `ttl="1h"`

### 5.3 監控快取效益

建議在生產環境監控以下指標：

- **Cache Hit Rate** = `cacheReadTokens / totalInputTokens`
- **Cost Saving Rate** = `(1 - 實際費用 / 無快取費用) × 100%`
- 如果 Hit Rate < 50%，應檢查 system prompt 是否包含動態內容或 TTL 是否過短

**各平台查看快取指標的方式：**

```
Bedrock：CloudWatch → Metrics → Bedrock
         → CacheReadInputTokenCount / CacheWriteInputTokenCount

Azure OpenAI：response.usage.prompt_tokens_details.cached_tokens

GCP Gemini：response.usage_metadata.cached_content_token_count
```
