package com.example.asyncapi.repository;

import com.example.asyncapi.entity.Task;
import com.example.asyncapi.entity.TaskStatus;
import org.springframework.data.jpa.repository.JpaRepository;
import java.util.List;

public interface TaskRepository extends JpaRepository<Task, Long> {
    Task findByTaskId(String taskId);
    List<Task> findByStatusAndCallbackLocationIsNotNull(TaskStatus status);
} 