package com.example.asyncapi.ccr.dto.anthropic;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.databind.JsonNode;
import lombok.Data;

import java.util.List;
import java.util.Map;

@Data
@JsonIgnoreProperties(ignoreUnknown = true)
public class AnthropicRequest {

    private String model;

    @JsonProperty("max_tokens")
    private Integer maxTokens;

    /** Top-level system prompt (plain text) */
    private String system;

    private List<Message> messages;
    private Double temperature;

    @JsonProperty("top_p")
    private Double topP;

    private Boolean stream;

    @JsonProperty("stop_sequences")
    private List<String> stopSequences;

    private List<Tool> tools;

    @JsonProperty("tool_choice")
    private Map<String, Object> toolChoice;

    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class Message {
        private String role;
        /**
         * Can be a plain String or a JSON array of ContentBlock objects.
         * Using JsonNode to handle both cases uniformly.
         */
        private JsonNode content;
    }

    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class Tool {
        private String name;
        private String description;

        /** JSON Schema for the tool's parameters */
        @JsonProperty("input_schema")
        private JsonNode inputSchema;
    }
}
