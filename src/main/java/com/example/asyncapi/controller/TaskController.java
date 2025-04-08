package com.example.asyncapi.controller;

import com.example.asyncapi.dto.TaskRequest;
import com.example.asyncapi.dto.TaskResponse;
import com.example.asyncapi.dto.TaskUpdateRequest;
import com.example.asyncapi.service.TaskService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/tasks")
@RequiredArgsConstructor
public class TaskController {

    private final TaskService taskService;

    @PostMapping
    public ResponseEntity<TaskResponse> createTask(
            @Valid @RequestBody TaskRequest request,
            @RequestHeader(value = "t-callback-location", required = false) String callbackLocation,
            @RequestHeader(value = "t-callbacktoken", required = false) String callbackToken) {
        return ResponseEntity.ok(taskService.createTask(request, callbackLocation, callbackToken));
    }

    @GetMapping("/{taskId}")
    public ResponseEntity<TaskResponse> getTask(@PathVariable String taskId) {
        return ResponseEntity.ok(taskService.getTask(taskId));
    }

    @PatchMapping("/{taskId}")
    public ResponseEntity<TaskResponse> updateTask(
            @PathVariable String taskId,
            @Valid @RequestBody TaskUpdateRequest request) {
        return ResponseEntity.ok(taskService.updateTask(taskId, request));
    }
} 