package com.example.asyncapi.ccr.dto.openai;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

import java.util.List;

/** Covers both streaming chunks (object=chat.completion.chunk) and full responses */
@Data
@JsonIgnoreProperties(ignoreUnknown = true)
public class OpenAiResponse {

    private String id;
    private String object;
    private String model;
    private List<Choice> choices;
    private Usage usage;

    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class Choice {
        private Integer index;
        private Message message;   // non-streaming
        private Delta delta;       // streaming

        @JsonProperty("finish_reason")
        private String finishReason;
    }

    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class Message {
        private String role;
        private String content;

        @JsonProperty("tool_calls")
        private List<ToolCall> toolCalls;
    }

    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class Delta {
        private String role;
        private String content;

        @JsonProperty("tool_calls")
        private List<ToolCall> toolCalls;
    }

    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class ToolCall {
        private Integer index;
        private String id;
        private String type;
        private FunctionCall function;
    }

    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class FunctionCall {
        private String name;
        private String arguments;
    }

    @Data
    @JsonIgnoreProperties(ignoreUnknown = true)
    public static class Usage {
        @JsonProperty("prompt_tokens")
        private Integer promptTokens;

        @JsonProperty("completion_tokens")
        private Integer completionTokens;

        @JsonProperty("total_tokens")
        private Integer totalTokens;
    }
}
