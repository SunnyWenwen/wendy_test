package com.example.asyncapi.ccr.controller;

import com.example.asyncapi.ccr.dto.anthropic.AnthropicRequest;
import com.example.asyncapi.ccr.dto.anthropic.AnthropicResponse;
import com.example.asyncapi.ccr.service.ApiTransformService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * Accepts Anthropic Messages API requests from Claude Code,
 * transforms them to OpenAI /chat/completions format, and proxies
 * the response back in Anthropic format.
 *
 * Claude Code must point ANTHROPIC_BASE_URL at this service.
 */
@Slf4j
@RestController
@RequestMapping("/v1")
@RequiredArgsConstructor
public class AnthropicProxyController {

    private final ApiTransformService transformService;

    /**
     * Non-streaming: returns full Anthropic response JSON.
     * Streaming:     returns an SSE stream in Anthropic event format.
     */
    @PostMapping(
            value = "/messages",
            consumes = MediaType.APPLICATION_JSON_VALUE,
            produces = {MediaType.APPLICATION_JSON_VALUE, MediaType.TEXT_EVENT_STREAM_VALUE}
    )
    public Object messages(@RequestBody AnthropicRequest request) {
        log.debug("CCR → model={} stream={}", request.getModel(), request.getStream());

        if (Boolean.TRUE.equals(request.getStream())) {
            return streamingResponse(request);
        }
        return syncResponse(request);
    }

    private AnthropicResponse syncResponse(AnthropicRequest req) {
        return transformService.proxy(req);
    }

    private SseEmitter streamingResponse(AnthropicRequest req) {
        return transformService.proxyStreaming(req);
    }
}
