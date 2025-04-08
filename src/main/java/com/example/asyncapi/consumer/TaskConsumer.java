package com.example.asyncapi.consumer;

import com.example.asyncapi.entity.Task;
import com.example.asyncapi.entity.TaskStatus;
import com.example.asyncapi.repository.TaskRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

@Component
@RequiredArgsConstructor
@Slf4j
public class TaskConsumer {

    private final TaskRepository taskRepository;

    @RabbitListener(queues = "${rabbitmq.task.queue}")
    @Transactional
    public void processTask(String taskId) {
        log.info("Processing task: {}", taskId);
        
        Task task = taskRepository.findByTaskId(taskId);
        if (task == null) {
            log.error("Task not found: {}", taskId);
            return;
        }

        try {
            // Update task status to RUNNING
            task.setStatus(TaskStatus.RUNNING);
            taskRepository.save(task);

            // Simulate task processing
            Thread.sleep(5000);

            // Update task status to PENDING
            task.setStatus(TaskStatus.PENDING);
            taskRepository.save(task);

            log.info("Task processed successfully: {}", taskId);
        } catch (Exception e) {
            log.error("Error processing task: {}", taskId, e);
            task.setStatus(TaskStatus.FAILURE);
            task.setErrorMessage(e.getMessage());
            taskRepository.save(task);
        }
    }
} 