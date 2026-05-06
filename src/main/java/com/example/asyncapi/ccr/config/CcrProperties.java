package com.example.asyncapi.ccr.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

import java.util.HashMap;
import java.util.Map;

@Data
@Component
@ConfigurationProperties(prefix = "ccr")
public class CcrProperties {
    /** Full URL of the OpenAI-compatible downstream endpoint (APISIX → LiteLLM) */
    private String downstreamUrl = "http://localhost:9080/v1/chat/completions";

    /** Bearer token forwarded to APISIX/LiteLLM */
    private String apiKey = "";

    /**
     * Map Anthropic model IDs to the model name expected by LiteLLM.
     * Key   = Anthropic model name sent by Claude Code
     * Value = LiteLLM model name (e.g. bedrock/..., vertex_ai/..., openai/...)
     */
    private Map<String, String> modelMapping = new HashMap<>();

    /** Connect timeout in ms */
    private int connectTimeoutMs = 10000;

    /** Read timeout in ms (0 = infinite, needed for streaming) */
    private int streamReadTimeoutMs = 0;

    /** Read timeout in ms for non-streaming calls */
    private int readTimeoutMs = 120000;
}
