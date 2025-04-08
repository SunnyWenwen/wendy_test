package com.example.asyncapi.service;

import com.example.asyncapi.entity.Task;

public interface CallbackService {
    void processCallback(Task task);
} 