# LLM Gateway E2E 測試套件

自架 LLM Gateway 整合測試，驗證 Claude Code 與 Kilo Code 透過 Gateway 串接 AWS Bedrock / Vertex AI 的完整鏈路。

```
Claude Code / Kilo Code
    → API6 (ai-proxy-multi)
    → LiteLLM
    → AWS Bedrock (Claude Sonnet/Opus) / Vertex AI (Gemini)
```

詳細測試規劃請參閱 [TEST_DESIGN.md](./TEST_DESIGN.md)。

---

## 快速開始

```bash
cd e2e-tests
python3 -m pip install -e ".[dev]"
cp .env.example .env   # 填入 GATEWAY_BASE_URL、GATEWAY_API_KEY
make smoke             # 先跑連線驗證
make test              # 完整測試（略過 slow 標記）
```

---

## 目錄結構

```
e2e-tests/
├── agents/
│   ├── base.py              # 共用 async HTTP client
│   ├── claude_code.py       # CCR 格式模擬器（reasoning dict、thinking、cache_control）
│   └── kilo.py              # Kilo OpenAI-compatible 模擬器 + fixture loader
├── config/
│   ├── models.yaml          # 模型 registry（新增模型只需改此檔）
│   └── settings.py          # 環境變數設定，含 default model 常數
├── fixtures/
│   ├── kilo_payloads/       # Kilo 真實抓包 payload（.json）
│   └── cc_payloads/         # CCR 真實抓包 payload（.json）
├── validators/
│   ├── response.py          # OpenAI response schema assertions
│   └── log_parser.py        # kubectl log 擷取與 assertion
├── utils/
│   └── kubectl.py           # Kubernetes Python client 包裝
└── tests/
    ├── conftest.py           # 共用 fixtures、自動 parametrize
    ├── test_connectivity.py  # 連線與認證 smoke tests
    ├── test_claude_code.py   # CCR 特有行為測試
    ├── test_kilo.py          # Kilo 行為測試
    ├── test_models.py        # 全模型矩陣（自動 parametrize）
    ├── test_streaming.py     # SSE streaming 測試
    ├── test_tool_use.py      # 工具調用 round-trip 測試
    └── test_reasoning.py     # Extended thinking / reasoning 測試
```

---

## 測試模型

| 模型 ID（Gateway alias） | Upstream | 能力 |
|---|---|---|
| `claude-sonnet-4.5` | AWS Bedrock | streaming, tool_use, reasoning, vision |
| `claude-sonnet-4.6` | AWS Bedrock | streaming, tool_use, reasoning, vision |
| `claude-opus-4.6` | AWS Bedrock | streaming, tool_use, reasoning, vision |
| `claude-opus-4.7` | AWS Bedrock | streaming, tool_use, reasoning, vision |
| `gemini-2.5-pro` | Vertex AI | streaming, tool_use, reasoning, vision |
| `gemini-3.1-flash-lite-preview` | Vertex AI | streaming, tool_use, vision |
| `gemini-3.1-pro-preview` | Vertex AI | streaming, tool_use, reasoning, vision |

---

## 執行指令

```bash
make smoke        # 連線 smoke test（最快，先跑這個）
make cc           # Claude Code / CCR 測試
make kilo         # Kilo 測試
make models       # 所有模型矩陣
make bedrock      # 只跑 Bedrock 模型
make vertex       # 只跑 Vertex 模型
make streaming    # Streaming 測試
make tools        # 工具調用測試
make reasoning    # Reasoning 測試
make no-logs      # 停用 kubectl log 驗證（無 k8s 存取時）
```

---

## 新增模型

只需在 `config/models.yaml` 增加一筆 entry，不需修改任何測試程式碼：

```yaml
bedrock:
  claude-new-model:
    model_id: "claude-new-model-alias"   # Gateway 對外的 alias
    upstream: bedrock
    capabilities: [streaming, tool_use, reasoning, vision, long_ctx]
```

執行 `make models` 即自動納入全模型矩陣測試。

---

## 環境變數

| 變數 | 預設值 | 說明 |
|---|---|---|
| `GATEWAY_BASE_URL` | `http://testhost` | LLM Gateway 入口 |
| `GATEWAY_API_KEY` | `XXXX` | Bearer Token |
| `DEFAULT_BEDROCK_MODEL` | `claude-sonnet-4.5` | 一般測試預設模型 |
| `DEFAULT_VERTEX_MODEL` | `gemini-3.1-flash-lite-preview` | Vertex 測試預設模型 |
| `DEFAULT_REASONING_MODEL` | `claude-sonnet-4.6` | Reasoning 測試預設模型 |
| `K8S_NAMESPACE` | `icgs` | Kubernetes namespace |
| `LITELLM_POD_PREFIX` | `litellm` | LiteLLM pod name prefix（後綴隨機） |
| `AI_PROXY_POD_PREFIX` | `apisix` | Apisix gateway pod name prefix（後綴隨機） |
| `ENABLE_LOG_VALIDATION` | `true` | 開啟 kubectl log assertions |
| `LOG_TAIL_LINES` | `200` | 擷取的 log 行數 |
| `LOG_CAPTURE_DELAY` | `2.0` | 請求後等待幾秒再取 log |
| `REQUEST_TIMEOUT` | `120.0` | HTTP timeout（秒） |

---

## CI 整合範例

```yaml
- name: LLM Gateway E2E Tests
  working-directory: e2e-tests
  env:
    GATEWAY_BASE_URL: ${{ secrets.LLM_GATEWAY_DEV_URL }}
    GATEWAY_API_KEY: ${{ secrets.LLM_GATEWAY_API_KEY }}
    ENABLE_LOG_VALIDATION: "false"
  run: |
    pip install -e ".[dev]"
    make smoke
    make test
```
