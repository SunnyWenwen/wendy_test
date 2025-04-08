package com.example.asyncapi.service.impl;

import com.example.asyncapi.entity.Task;
import com.example.asyncapi.service.CallbackService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.retry.annotation.Backoff;
import org.springframework.retry.annotation.Retryable;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;

import java.util.HashMap;
import java.util.Map;

@Service
@RequiredArgsConstructor
@Slf4j
public class CallbackServiceImpl implements CallbackService {

    private final RestTemplate restTemplate;

    @Value("${app.callback.retry.max-attempts}")
    private int maxAttempts;

    @Value("${app.callback.retry.initial-interval}")
    private long initialInterval;

    @Value("${app.callback.retry.multiplier}")
    private double multiplier;

    @Value("${app.callback.retry.max-interval}")
    private long maxInterval;

    @Override
    @Retryable(
        maxAttempts = 3,
        backoff = @Backoff(
            delay = 1000,
            multiplier = 2.0,
            maxDelay = 10000
        )
    )
    public void processCallback(Task task) {
        log.info("Processing callback for task: {}", task.getTaskId());

        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        headers.set("t-callbacktoken", task.getCallbackToken());

        Map<String, Object> body = new HashMap<>();
        body.put("taskId", task.getTaskId());
        body.put("status", task.getStatus());
        body.put("result", task.getResult());
        body.put("errorMessage", task.getErrorMessage());

        HttpEntity<Map<String, Object>> request = new HttpEntity<>(body, headers);

        try {
            restTemplate.postForEntity(task.getCallbackLocation(), request, String.class);
            log.info("Callback processed successfully for task: {}", task.getTaskId());
        } catch (Exception e) {
            log.error("Failed to process callback for task: {}", task.getTaskId(), e);
            throw e;
        }
    }
} 