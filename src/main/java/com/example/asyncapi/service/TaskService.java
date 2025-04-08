package com.example.asyncapi.service;

import com.example.asyncapi.dto.TaskRequest;
import com.example.asyncapi.dto.TaskResponse;
import com.example.asyncapi.dto.TaskUpdateRequest;

public interface TaskService {
    TaskResponse createTask(TaskRequest request, String callbackLocation, String callbackToken);
    TaskResponse getTask(String taskId);
    TaskResponse updateTask(String taskId, TaskUpdateRequest request);
} 