package com.example.taskmanagement.repository;

import com.example.taskmanagement.entity.Task;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.util.Optional;

@Repository
public interface TaskRepository extends JpaRepository<Task, Long> {

    Page<Task> findByUserId(Long userId, Pageable pageable);

    Page<Task> findByUserIdAndCompleted(Long userId, boolean completed, Pageable pageable);

    Page<Task> findByUserIdAndTitleContainingIgnoreCase(Long userId, String title, Pageable pageable);

    Page<Task> findByUserIdAndCompletedAndTitleContainingIgnoreCase(Long userId, boolean completed, String title, Pageable pageable);

    Optional<Task> findByIdAndUserId(Long id, Long userId);
}
