package com.example.asyncapi.ccr.service;

import com.example.asyncapi.ccr.config.CcrProperties;
import com.example.asyncapi.ccr.dto.anthropic.AnthropicRequest;
import com.example.asyncapi.ccr.dto.anthropic.AnthropicResponse;
import com.example.asyncapi.ccr.dto.openai.OpenAiRequest;
import com.example.asyncapi.ccr.dto.openai.OpenAiResponse;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.*;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

@Slf4j
@Service
@RequiredArgsConstructor
public class ApiTransformService {

    private final CcrProperties props;
    private final ObjectMapper objectMapper;
    private final RestTemplate restTemplate;

    private final ExecutorService executor = Executors.newCachedThreadPool();

    // ── Non-streaming proxy ───────────────────────────────────────────────────

    public AnthropicResponse proxy(AnthropicRequest req) {
        OpenAiRequest openAiReq = toOpenAiRequest(req);
        HttpHeaders headers = buildHeaders();
        ResponseEntity<OpenAiResponse> resp = restTemplate.exchange(
                props.getDownstreamUrl(),
                HttpMethod.POST,
                new HttpEntity<>(openAiReq, headers),
                OpenAiResponse.class);
        return toAnthropicResponse(resp.getBody(), req.getModel());
    }

    // ── Streaming proxy ───────────────────────────────────────────────────────

    public SseEmitter proxyStreaming(AnthropicRequest req) {
        OpenAiRequest openAiReq = toOpenAiRequest(req);
        SseEmitter emitter = new SseEmitter(0L);
        String msgId = newMsgId();

        executor.submit(() -> {
            try {
                doStream(openAiReq, req.getModel(), msgId, emitter);
            } catch (Exception e) {
                log.error("CCR streaming error", e);
                emitter.completeWithError(e);
            }
        });
        return emitter;
    }

    // ── Request transform: Anthropic → OpenAI ────────────────────────────────

    OpenAiRequest toOpenAiRequest(AnthropicRequest req) {
        List<OpenAiRequest.Message> messages = new ArrayList<>();

        if (req.getSystem() != null && !req.getSystem().isBlank()) {
            messages.add(OpenAiRequest.Message.builder()
                    .role("system").content(req.getSystem()).build());
        }

        for (AnthropicRequest.Message m : req.getMessages()) {
            messages.addAll(convertAnthropicMessage(m));
        }

        List<OpenAiRequest.Tool> tools = null;
        if (req.getTools() != null && !req.getTools().isEmpty()) {
            tools = req.getTools().stream()
                    .map(t -> OpenAiRequest.Tool.builder()
                            .type("function")
                            .function(OpenAiRequest.Function.builder()
                                    .name(t.getName())
                                    .description(t.getDescription())
                                    .parameters(t.getInputSchema())
                                    .build())
                            .build())
                    .toList();
        }

        OpenAiRequest.StreamOptions streamOptions = Boolean.TRUE.equals(req.getStream())
                ? OpenAiRequest.StreamOptions.builder().includeUsage(true).build()
                : null;

        return OpenAiRequest.builder()
                .model(resolveModel(req.getModel()))
                .messages(messages)
                .maxTokens(req.getMaxTokens())
                .temperature(req.getTemperature())
                .topP(req.getTopP())
                .stream(req.getStream())
                .streamOptions(streamOptions)
                .stop(req.getStopSequences())
                .tools(tools)
                .build();
    }

    /**
     * A single Anthropic message can expand into multiple OpenAI messages when
     * it contains tool_result blocks (each becomes a separate "tool" role message).
     */
    private List<OpenAiRequest.Message> convertAnthropicMessage(AnthropicRequest.Message msg) {
        List<OpenAiRequest.Message> result = new ArrayList<>();
        JsonNode content = msg.getContent();

        if (content == null) return result;

        if (content.isTextual()) {
            result.add(OpenAiRequest.Message.builder()
                    .role(msg.getRole()).content(content.asText()).build());
            return result;
        }

        if (!content.isArray()) return result;

        // Array of content blocks ─────────────────────────────────────────────
        List<OpenAiRequest.ToolCall> toolCalls = new ArrayList<>();
        List<OpenAiRequest.Message> toolResults = new ArrayList<>();
        StringBuilder text = new StringBuilder();

        for (JsonNode block : content) {
            String type = block.path("type").asText("");
            switch (type) {
                case "text" -> text.append(block.path("text").asText());

                case "tool_use" -> {
                    String argsJson;
                    try {
                        argsJson = objectMapper.writeValueAsString(block.path("input"));
                    } catch (Exception e) {
                        argsJson = "{}";
                    }
                    toolCalls.add(OpenAiRequest.ToolCall.builder()
                            .id(block.path("id").asText())
                            .type("function")
                            .function(OpenAiRequest.FunctionCall.builder()
                                    .name(block.path("name").asText())
                                    .arguments(argsJson)
                                    .build())
                            .build());
                }

                case "tool_result" -> {
                    String toolContent = extractToolResultText(block.path("content"));
                    toolResults.add(OpenAiRequest.Message.builder()
                            .role("tool")
                            .toolCallId(block.path("tool_use_id").asText())
                            .content(toolContent)
                            .build());
                }
            }
        }

        if (!toolCalls.isEmpty()) {
            // assistant message that invoked tools
            result.add(OpenAiRequest.Message.builder()
                    .role(msg.getRole())
                    .content(!text.isEmpty() ? text.toString() : null)
                    .toolCalls(toolCalls)
                    .build());
        } else if (!toolResults.isEmpty()) {
            // user message carrying tool results
            result.addAll(toolResults);
        } else if (!text.isEmpty()) {
            result.add(OpenAiRequest.Message.builder()
                    .role(msg.getRole()).content(text.toString()).build());
        }

        return result;
    }

    private String extractToolResultText(JsonNode content) {
        if (content.isTextual()) return content.asText();
        if (content.isArray()) {
            StringBuilder sb = new StringBuilder();
            for (JsonNode block : content) {
                if ("text".equals(block.path("type").asText())) {
                    sb.append(block.path("text").asText());
                }
            }
            return sb.toString();
        }
        return "";
    }

    private String resolveModel(String anthropicModel) {
        return props.getModelMapping().getOrDefault(anthropicModel, anthropicModel);
    }

    // ── Response transform: OpenAI → Anthropic ───────────────────────────────

    AnthropicResponse toAnthropicResponse(OpenAiResponse oai, String originalModel) {
        List<AnthropicResponse.ContentBlock> blocks = new ArrayList<>();

        if (oai != null && oai.getChoices() != null && !oai.getChoices().isEmpty()) {
            OpenAiResponse.Message msg = oai.getChoices().get(0).getMessage();
            if (msg != null) {
                if (msg.getContent() != null && !msg.getContent().isBlank()) {
                    blocks.add(AnthropicResponse.ContentBlock.builder()
                            .type("text").text(msg.getContent()).build());
                }
                if (msg.getToolCalls() != null) {
                    for (OpenAiResponse.ToolCall tc : msg.getToolCalls()) {
                        Object input = parseJson(tc.getFunction().getArguments());
                        blocks.add(AnthropicResponse.ContentBlock.builder()
                                .type("tool_use")
                                .id(tc.getId())
                                .name(tc.getFunction().getName())
                                .input(input)
                                .build());
                    }
                }
            }
        }

        String stopReason = "end_turn";
        if (oai != null && oai.getChoices() != null && !oai.getChoices().isEmpty()) {
            stopReason = mapStopReason(oai.getChoices().get(0).getFinishReason());
        }

        AnthropicResponse.Usage usage = null;
        if (oai != null && oai.getUsage() != null) {
            usage = AnthropicResponse.Usage.builder()
                    .inputTokens(nvl(oai.getUsage().getPromptTokens()))
                    .outputTokens(nvl(oai.getUsage().getCompletionTokens()))
                    .build();
        }

        return AnthropicResponse.builder()
                .id(newMsgId())
                .type("message")
                .role("assistant")
                .content(blocks)
                .model(originalModel)
                .stopReason(stopReason)
                .stopSequence(null)
                .usage(usage)
                .build();
    }

    private String mapStopReason(String reason) {
        if (reason == null) return "end_turn";
        return switch (reason) {
            case "stop" -> "end_turn";
            case "length" -> "max_tokens";
            case "tool_calls" -> "tool_use";
            default -> "end_turn";
        };
    }

    // ── Streaming implementation ──────────────────────────────────────────────

    private void doStream(OpenAiRequest req, String originalModel, String msgId,
                          SseEmitter emitter) throws Exception {

        URL url = new URL(props.getDownstreamUrl());
        HttpURLConnection conn = (HttpURLConnection) url.openConnection();
        conn.setRequestMethod("POST");
        conn.setDoOutput(true);
        conn.setConnectTimeout(props.getConnectTimeoutMs());
        conn.setReadTimeout(props.getStreamReadTimeoutMs());
        conn.setRequestProperty("Content-Type", "application/json");
        conn.setRequestProperty("Accept", "text/event-stream");
        conn.setRequestProperty("Cache-Control", "no-cache");
        if (!props.getApiKey().isBlank()) {
            conn.setRequestProperty("Authorization", "Bearer " + props.getApiKey());
        }
        conn.getOutputStream().write(objectMapper.writeValueAsBytes(req));

        // ── Anthropic SSE prologue ────────────────────────────────────────────
        send(emitter, "message_start",
                """
                {"type":"message_start","message":{"id":"%s","type":"message","role":"assistant",\
                "content":[],"model":"%s","stop_reason":null,"stop_sequence":null,\
                "usage":{"input_tokens":0,"output_tokens":1}}}""".formatted(msgId, originalModel));

        send(emitter, "content_block_start",
                """
                {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}""");

        send(emitter, "ping", "{\"type\":\"ping\"}");

        // ── Streaming state ───────────────────────────────────────────────────
        int outputTokens = 0;
        int inputTokens = 0;
        String finishReason = null;
        // tracks tool-call block indices: toolCallId → anthropic block index
        Map<String, Integer> tcBlockIndex = new LinkedHashMap<>();
        int nextBlockIndex = 1; // index 0 is the text block

        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(conn.getInputStream(), StandardCharsets.UTF_8))) {

            String line;
            while ((line = reader.readLine()) != null) {
                if (!line.startsWith("data: ")) continue;
                String data = line.substring(6).trim();
                if ("[DONE]".equals(data)) break;

                OpenAiResponse chunk;
                try {
                    chunk = objectMapper.readValue(data, OpenAiResponse.class);
                } catch (Exception e) {
                    log.warn("CCR: unparseable SSE chunk: {}", data);
                    continue;
                }

                if (chunk.getUsage() != null) {
                    inputTokens = nvl(chunk.getUsage().getPromptTokens());
                    outputTokens = nvl(chunk.getUsage().getCompletionTokens());
                }

                if (chunk.getChoices() == null || chunk.getChoices().isEmpty()) continue;
                OpenAiResponse.Choice choice = chunk.getChoices().get(0);
                if (choice.getFinishReason() != null) finishReason = choice.getFinishReason();

                OpenAiResponse.Delta delta = choice.getDelta();
                if (delta == null) continue;

                // ── text delta ────────────────────────────────────────────────
                if (delta.getContent() != null && !delta.getContent().isEmpty()) {
                    send(emitter, "content_block_delta",
                            objectMapper.writeValueAsString(Map.of(
                                    "type", "content_block_delta",
                                    "index", 0,
                                    "delta", Map.of("type", "text_delta", "text", delta.getContent()))));
                }

                // ── tool-call deltas ──────────────────────────────────────────
                if (delta.getToolCalls() != null) {
                    for (OpenAiResponse.ToolCall tc : delta.getToolCalls()) {
                        String tcId = tc.getId();

                        if (tcId != null && !tcBlockIndex.containsKey(tcId)) {
                            // New tool call starting → open a tool_use content block
                            int blockIdx = nextBlockIndex++;
                            tcBlockIndex.put(tcId, blockIdx);
                            String name = tc.getFunction() != null ? tc.getFunction().getName() : "";
                            send(emitter, "content_block_start",
                                    objectMapper.writeValueAsString(Map.of(
                                            "type", "content_block_start",
                                            "index", blockIdx,
                                            "content_block", Map.of("type", "tool_use",
                                                    "id", tcId, "name", name, "input", Map.of()))));
                        }

                        if (tc.getFunction() != null && tc.getFunction().getArguments() != null) {
                            String resolvedId = tcId != null ? tcId
                                    : tcBlockIndex.keySet().stream().reduce((a, b) -> b).orElse(null);
                            if (resolvedId != null && tcBlockIndex.containsKey(resolvedId)) {
                                int blockIdx = tcBlockIndex.get(resolvedId);
                                send(emitter, "content_block_delta",
                                        objectMapper.writeValueAsString(Map.of(
                                                "type", "content_block_delta",
                                                "index", blockIdx,
                                                "delta", Map.of("type", "input_json_delta",
                                                        "partial_json", tc.getFunction().getArguments()))));
                            }
                        }
                    }
                }
            }
        }

        // ── Anthropic SSE epilogue ────────────────────────────────────────────
        // Close text block
        send(emitter, "content_block_stop",
                "{\"type\":\"content_block_stop\",\"index\":0}");

        // Close tool_use blocks
        for (int idx : tcBlockIndex.values()) {
            send(emitter, "content_block_stop",
                    "{\"type\":\"content_block_stop\",\"index\":" + idx + "}");
        }

        // message_delta — use string formatting to include null stop_sequence
        String stopReason = mapStopReason(finishReason);
        send(emitter, "message_delta",
                "{\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"%s\",\"stop_sequence\":null},\"usage\":{\"output_tokens\":%d}}"
                        .formatted(stopReason, outputTokens));

        send(emitter, "message_stop", "{\"type\":\"message_stop\"}");
        emitter.complete();
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private void send(SseEmitter emitter, String event, String data) throws Exception {
        emitter.send(SseEmitter.event().name(event).data(data));
    }

    private HttpHeaders buildHeaders() {
        HttpHeaders h = new HttpHeaders();
        h.setContentType(MediaType.APPLICATION_JSON);
        if (!props.getApiKey().isBlank()) h.setBearerAuth(props.getApiKey());
        return h;
    }

    private String newMsgId() {
        return "msg_" + UUID.randomUUID().toString().replace("-", "").substring(0, 24);
    }

    private int nvl(Integer v) {
        return v == null ? 0 : v;
    }

    private Object parseJson(String json) {
        if (json == null) return Map.of();
        try {
            return objectMapper.readValue(json, Map.class);
        } catch (Exception e) {
            return json;
        }
    }
}
