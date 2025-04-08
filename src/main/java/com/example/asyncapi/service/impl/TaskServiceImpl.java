package com.example.asyncapi.service.impl;

import com.example.asyncapi.dto.TaskRequest;
import com.example.asyncapi.dto.TaskResponse;
import com.example.asyncapi.dto.TaskUpdateRequest;
import com.example.asyncapi.entity.Task;
import com.example.asyncapi.entity.TaskStatus;
import com.example.asyncapi.repository.TaskRepository;
import com.example.asyncapi.service.TaskService;
import lombok.RequiredArgsConstructor;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.UUID;

@Service
@RequiredArgsConstructor
public class TaskServiceImpl implements TaskService {

    private final TaskRepository taskRepository;
    private final RedisTemplate<String, String> redisTemplate;
    private final RabbitTemplate rabbitTemplate;

    @Override
    @Transactional
    public TaskResponse createTask(TaskRequest request, String callbackLocation, String callbackToken) {
        Task task = new Task();
        task.setTaskId(UUID.randomUUID().toString());
        task.setStatus(TaskStatus.CREATING);
        task.setCallbackLocation(callbackLocation);
        task.setCallbackToken(callbackToken);

        task = taskRepository.save(task);

        // Store in Redis for quick access
        redisTemplate.opsForValue().set("task:" + task.getTaskId(), task.getStatus().name());

        // Send to RabbitMQ for processing
        rabbitTemplate.convertAndSend("task.queue", task.getTaskId());

        return convertToResponse(task);
    }

    @Override
    public TaskResponse getTask(String taskId) {
        Task task = taskRepository.findByTaskId(taskId);
        if (task == null) {
            throw new RuntimeException("Task not found");
        }
        return convertToResponse(task);
    }

    @Override
    @Transactional
    public TaskResponse updateTask(String taskId, TaskUpdateRequest request) {
        Task task = taskRepository.findByTaskId(taskId);
        if (task == null) {
            throw new RuntimeException("Task not found");
        }

        task.setStatus(request.getStatus());
        task.setResult(request.getResult());
        task.setErrorMessage(request.getErrorMessage());

        task = taskRepository.save(task);

        // Update Redis
        redisTemplate.opsForValue().set("task:" + task.getTaskId(), task.getStatus().name());

        // If task is complete and has callback location, send to callback queue
        if (task.getStatus() == TaskStatus.COMPLETE && task.getCallbackLocation() != null) {
            rabbitTemplate.convertAndSend("callback.queue", task);
        }

        return convertToResponse(task);
    }

    private TaskResponse convertToResponse(Task task) {
        TaskResponse response = new TaskResponse();
        response.setTaskId(task.getTaskId());
        response.setStatus(task.getStatus());
        response.setCreatedAt(task.getCreatedAt());
        response.setUpdatedAt(task.getUpdatedAt());
        return response;
    }
} 