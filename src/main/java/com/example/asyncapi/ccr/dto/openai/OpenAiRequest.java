package com.example.asyncapi.ccr.dto.openai;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@JsonInclude(JsonInclude.Include.NON_NULL)
public class OpenAiRequest {

    private String model;
    private List<Message> messages;

    @JsonProperty("max_tokens")
    private Integer maxTokens;

    private Double temperature;

    @JsonProperty("top_p")
    private Double topP;

    private Boolean stream;

    /** Requests usage stats inside stream chunks; requires stream=true */
    @JsonProperty("stream_options")
    private StreamOptions streamOptions;

    private List<String> stop;
    private List<Tool> tools;

    @JsonProperty("tool_choice")
    private Object toolChoice;

    // ── nested types ─────────────────────────────────────────────────────────

    @Data
    @Builder
    @NoArgsConstructor
    @AllArgsConstructor
    public static class Message {
        private String role;

        /**
         * Serialized as null for assistant-tool-call messages so downstream APIs
         * receive the field (even when empty) as the OpenAI spec requires.
         */
        @JsonInclude(JsonInclude.Include.ALWAYS)
        private Object content;

        @JsonProperty("tool_calls")
        @JsonInclude(JsonInclude.Include.NON_NULL)
        private List<ToolCall> toolCalls;

        @JsonProperty("tool_call_id")
        @JsonInclude(JsonInclude.Include.NON_NULL)
        private String toolCallId;
    }

    @Data
    @Builder
    @NoArgsConstructor
    @AllArgsConstructor
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public static class ToolCall {
        private String id;
        private String type;
        private FunctionCall function;
    }

    @Data
    @Builder
    @NoArgsConstructor
    @AllArgsConstructor
    public static class FunctionCall {
        private String name;
        private String arguments; // JSON-encoded string
    }

    @Data
    @Builder
    @NoArgsConstructor
    @AllArgsConstructor
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public static class Tool {
        private String type;       // always "function"
        private Function function;
    }

    @Data
    @Builder
    @NoArgsConstructor
    @AllArgsConstructor
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public static class Function {
        private String name;
        private String description;
        private Object parameters; // JSON Schema (passed through from Anthropic input_schema)
    }

    @Data
    @Builder
    @NoArgsConstructor
    @AllArgsConstructor
    public static class StreamOptions {
        @JsonProperty("include_usage")
        private Boolean includeUsage;
    }
}
