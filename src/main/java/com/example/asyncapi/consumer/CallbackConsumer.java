package com.example.asyncapi.consumer;

import com.example.asyncapi.entity.Task;
import com.example.asyncapi.service.CallbackService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.stereotype.Component;

@Component
@RequiredArgsConstructor
@Slf4j
public class CallbackConsumer {

    private final CallbackService callbackService;

    @RabbitListener(queues = "${rabbitmq.callback.queue}")
    public void processCallback(Task task) {
        log.info("Received callback request for task: {}", task.getTaskId());
        try {
            callbackService.processCallback(task);
        } catch (Exception e) {
            log.error("Failed to process callback for task: {}", task.getTaskId(), e);
            // The @Retryable annotation in CallbackService will handle retries
        }
    }
} 