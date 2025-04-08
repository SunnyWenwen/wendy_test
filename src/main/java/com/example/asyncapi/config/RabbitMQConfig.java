package com.example.asyncapi.config;

import org.springframework.amqp.core.*;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class RabbitMQConfig {

    public static final String TASK_QUEUE = "task.queue";
    public static final String CALLBACK_QUEUE = "callback.queue";

    @Bean
    public Queue taskQueue() {
        return new Queue(TASK_QUEUE, true);
    }

    @Bean
    public Queue callbackQueue() {
        return new Queue(CALLBACK_QUEUE, true);
    }

    @Bean
    public DirectExchange taskExchange() {
        return new DirectExchange("task.exchange");
    }

    @Bean
    public DirectExchange callbackExchange() {
        return new DirectExchange("callback.exchange");
    }

    @Bean
    public Binding taskBinding(Queue taskQueue, DirectExchange taskExchange) {
        return BindingBuilder.bind(taskQueue)
                .to(taskExchange)
                .with("task.routing.key");
    }

    @Bean
    public Binding callbackBinding(Queue callbackQueue, DirectExchange callbackExchange) {
        return BindingBuilder.bind(callbackQueue)
                .to(callbackExchange)
                .with("callback.routing.key");
    }
} 