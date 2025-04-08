package com.example.asyncapi.dto;

import com.example.asyncapi.entity.TaskStatus;
import jakarta.validation.constraints.NotNull;
import lombok.Data;

@Data
public class TaskUpdateRequest {
    @NotNull(message = "Status is required")
    private TaskStatus status;
    
    private String result;
    private String errorMessage;
} 