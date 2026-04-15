package com.example.taskmanagement.service.impl;

import com.example.taskmanagement.dto.TaskRequest;
import com.example.taskmanagement.dto.TaskResponse;
import com.example.taskmanagement.dto.TaskUpdateRequest;
import com.example.taskmanagement.entity.Task;
import com.example.taskmanagement.entity.User;
import com.example.taskmanagement.exception.ResourceNotFoundException;
import com.example.taskmanagement.exception.TaskAlreadyCompletedException;
import com.example.taskmanagement.repository.TaskRepository;
import com.example.taskmanagement.repository.UserRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.PageImpl;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Pageable;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class TaskServiceImplTest {

    @Mock
    private TaskRepository taskRepository;

    @Mock
    private UserRepository userRepository;

    @InjectMocks
    private TaskServiceImpl taskService;

    private User testUser;
    private Task testTask;

    @BeforeEach
    void setUp() {
        testUser = User.builder()
                .id(1L)
                .username("Bob")
                .email("Bob@test.com")
                .password("encoded")
                .build();

        testTask = Task.builder()
                .id(1L)
                .title("Prepare presentation")
                .description("Phase 3 review")
                .dueDate(LocalDate.of(2026, 12, 28))
                .completed(false)
                .user(testUser)
                .build();
        testTask.setCreatedAt(LocalDateTime.now());
        testTask.setUpdatedAt(LocalDateTime.now());
    }

    // ==================== CREATE ====================

    @Test
    void createTask_shouldReturnCreatedTask() {
        TaskRequest request = TaskRequest.builder()
                .title("Prepare presentation")
                .description("Phase 3 review")
                .dueDate(LocalDate.of(2026, 12, 28))
                .build();

        when(userRepository.findById(1L)).thenReturn(Optional.of(testUser));
        when(taskRepository.save(any(Task.class))).thenReturn(testTask);

        TaskResponse response = taskService.createTask(request, 1L);

        assertThat(response.getTitle()).isEqualTo("Prepare presentation");
        assertThat(response.getDescription()).isEqualTo("Phase 3 review");
        assertThat(response.isCompleted()).isFalse();
        verify(taskRepository).save(any(Task.class));
    }

    @Test
    void createTask_shouldThrowWhenUserNotFound() {
        TaskRequest request = TaskRequest.builder()
                .title("Test task")
                .build();

        when(userRepository.findById(99L)).thenReturn(Optional.empty());

        assertThatThrownBy(() -> taskService.createTask(request, 99L))
                .isInstanceOf(ResourceNotFoundException.class)
                .hasMessageContaining("User not found");
    }

    // ==================== GET TASKS (PAGINATION) ====================

    @Test
    void getTasksByUser_shouldReturnPaginatedTasks() {
        Pageable pageable = PageRequest.of(0, 5);
        Page<Task> taskPage = new PageImpl<>(List.of(testTask), pageable, 1);

        when(taskRepository.findByUserId(1L, pageable)).thenReturn(taskPage);

        Page<TaskResponse> result = taskService.getTasksByUser(1L, null, null, pageable);

        assertThat(result.getContent()).hasSize(1);
        assertThat(result.getTotalElements()).isEqualTo(1);
        assertThat(result.getContent().get(0).getTitle()).isEqualTo("Prepare presentation");
    }

    @Test
    void getTasksByUser_shouldFilterByCompleted() {
        Pageable pageable = PageRequest.of(0, 5);
        Page<Task> taskPage = new PageImpl<>(List.of(), pageable, 0);

        when(taskRepository.findByUserIdAndCompleted(1L, true, pageable)).thenReturn(taskPage);

        Page<TaskResponse> result = taskService.getTasksByUser(1L, true, null, pageable);

        assertThat(result.getContent()).isEmpty();
    }

    @Test
    void getTasksByUser_shouldFilterByTitle() {
        Pageable pageable = PageRequest.of(0, 5);
        Page<Task> taskPage = new PageImpl<>(List.of(testTask), pageable, 1);

        when(taskRepository.findByUserIdAndTitleContainingIgnoreCase(1L, "presentation", pageable))
                .thenReturn(taskPage);

        Page<TaskResponse> result = taskService.getTasksByUser(1L, null, "presentation", pageable);

        assertThat(result.getContent()).hasSize(1);
    }

    // ==================== GET BY ID ====================

    @Test
    void getTaskById_shouldReturnTask() {
        when(taskRepository.findByIdAndUserId(1L, 1L)).thenReturn(Optional.of(testTask));

        TaskResponse response = taskService.getTaskById(1L, 1L);

        assertThat(response.getId()).isEqualTo(1L);
        assertThat(response.getTitle()).isEqualTo("Prepare presentation");
    }

    @Test
    void getTaskById_shouldThrowWhenNotFound() {
        when(taskRepository.findByIdAndUserId(99L, 1L)).thenReturn(Optional.empty());

        assertThatThrownBy(() -> taskService.getTaskById(99L, 1L))
                .isInstanceOf(ResourceNotFoundException.class)
                .hasMessageContaining("Task not found");
    }

    @Test
    void getTaskById_shouldThrowWhenAccessingAnotherUsersTask() {
        // Task belongs to user 1, but user 2 is trying to access it
        when(taskRepository.findByIdAndUserId(1L, 2L)).thenReturn(Optional.empty());

        assertThatThrownBy(() -> taskService.getTaskById(1L, 2L))
                .isInstanceOf(ResourceNotFoundException.class);
    }

    // ==================== UPDATE ====================

    @Test
    void updateTask_shouldUpdateAllFields() {
        TaskRequest request = TaskRequest.builder()
                .title("Updated title")
                .description("Updated description")
                .dueDate(LocalDate.of(2026, 12, 30))
                .build();

        when(taskRepository.findByIdAndUserId(1L, 1L)).thenReturn(Optional.of(testTask));
        when(taskRepository.save(any(Task.class))).thenReturn(testTask);

        TaskResponse response = taskService.updateTask(1L, request, 1L);

        verify(taskRepository).save(any(Task.class));
        assertThat(testTask.getTitle()).isEqualTo("Updated title");
        assertThat(testTask.getDescription()).isEqualTo("Updated description");
    }

    @Test
    void updateTask_shouldThrowWhenTaskNotFound() {
        TaskRequest request = TaskRequest.builder()
                .title("Updated title")
                .build();

        when(taskRepository.findByIdAndUserId(99L, 1L)).thenReturn(Optional.empty());

        assertThatThrownBy(() -> taskService.updateTask(99L, request, 1L))
                .isInstanceOf(ResourceNotFoundException.class);
    }

    // ==================== PATCH ====================

    @Test
    void patchTask_shouldUpdateOnlyProvidedFields() {
        TaskUpdateRequest request = TaskUpdateRequest.builder()
                .title("Patched title")
                .build();

        when(taskRepository.findByIdAndUserId(1L, 1L)).thenReturn(Optional.of(testTask));
        when(taskRepository.save(any(Task.class))).thenReturn(testTask);

        taskService.patchTask(1L, request, 1L);

        assertThat(testTask.getTitle()).isEqualTo("Patched title");
        // Description should remain unchanged
        assertThat(testTask.getDescription()).isEqualTo("Phase 3 review");
    }

    @Test
    void patchTask_shouldThrowWhenTaskAlreadyCompleted() {
        testTask.setCompleted(true);

        TaskUpdateRequest request = TaskUpdateRequest.builder()
                .title("Try to update")
                .build();

        when(taskRepository.findByIdAndUserId(1L, 1L)).thenReturn(Optional.of(testTask));

        assertThatThrownBy(() -> taskService.patchTask(1L, request, 1L))
                .isInstanceOf(TaskAlreadyCompletedException.class)
                .hasMessageContaining("already completed");

        verify(taskRepository, never()).save(any());
    }

    // ==================== MARK COMPLETE ====================

    @Test
    void markComplete_shouldSetCompletedTrue() {
        when(taskRepository.findByIdAndUserId(1L, 1L)).thenReturn(Optional.of(testTask));
        when(taskRepository.save(any(Task.class))).thenReturn(testTask);

        TaskResponse response = taskService.markComplete(1L, 1L);

        assertThat(testTask.isCompleted()).isTrue();
        verify(taskRepository).save(any(Task.class));
    }

    @Test
    void markComplete_shouldThrowWhenAlreadyCompleted() {
        testTask.setCompleted(true);

        when(taskRepository.findByIdAndUserId(1L, 1L)).thenReturn(Optional.of(testTask));

        assertThatThrownBy(() -> taskService.markComplete(1L, 1L))
                .isInstanceOf(TaskAlreadyCompletedException.class)
                .hasMessageContaining("already completed");
    }

    // ==================== DELETE ====================

    @Test
    void deleteTask_shouldDeleteTask() {
        when(taskRepository.findByIdAndUserId(1L, 1L)).thenReturn(Optional.of(testTask));

        taskService.deleteTask(1L, 1L);

        verify(taskRepository).delete(testTask);
    }

    @Test
    void deleteTask_shouldThrowWhenNotFound() {
        when(taskRepository.findByIdAndUserId(99L, 1L)).thenReturn(Optional.empty());

        assertThatThrownBy(() -> taskService.deleteTask(99L, 1L))
                .isInstanceOf(ResourceNotFoundException.class);
    }
}
