package com.example.taskmanagement.exception;

public class TaskAlreadyCompletedException extends RuntimeException {

    public TaskAlreadyCompletedException(String message) {
        super(message);
    }
}
