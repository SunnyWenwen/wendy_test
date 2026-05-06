package com.example.asyncapi.ccr.dto.anthropic;

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
public class AnthropicResponse {

    private String id;
    private String type;
    private String role;
    private List<ContentBlock> content;
    private String model;

    @JsonProperty("stop_reason")
    private String stopReason;

    /** Always serialized, even when null, to match Anthropic spec */
    @JsonInclude(JsonInclude.Include.ALWAYS)
    @JsonProperty("stop_sequence")
    private String stopSequence;

    private Usage usage;

    @Data
    @Builder
    @NoArgsConstructor
    @AllArgsConstructor
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public static class ContentBlock {
        private String type;   // "text" | "tool_use"
        private String text;
        private String id;
        private String name;
        private Object input;  // Map for tool_use
    }

    @Data
    @Builder
    @NoArgsConstructor
    @AllArgsConstructor
    public static class Usage {
        @JsonProperty("input_tokens")
        private int inputTokens;

        @JsonProperty("output_tokens")
        private int outputTokens;
    }
}
