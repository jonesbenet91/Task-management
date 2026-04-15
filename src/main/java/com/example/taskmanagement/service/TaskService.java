package com.example.taskmanagement.service;

import com.example.taskmanagement.dto.TaskRequest;
import com.example.taskmanagement.dto.TaskResponse;
import com.example.taskmanagement.dto.TaskUpdateRequest;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;

public interface TaskService {

    TaskResponse createTask(TaskRequest request, Long userId);

    Page<TaskResponse> getTasksByUser(Long userId, Boolean completed, String title, Pageable pageable);

    TaskResponse getTaskById(Long taskId, Long userId);

    TaskResponse updateTask(Long taskId, TaskRequest request, Long userId);

    TaskResponse patchTask(Long taskId, TaskUpdateRequest request, Long userId);

    TaskResponse markComplete(Long taskId, Long userId);

    void deleteTask(Long taskId, Long userId);
}
