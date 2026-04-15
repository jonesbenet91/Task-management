package com.example.taskmanagement.service.impl;

import com.example.taskmanagement.dto.AuthResponse;
import com.example.taskmanagement.dto.LoginRequest;
import com.example.taskmanagement.dto.RegisterRequest;
import com.example.taskmanagement.entity.User;
import com.example.taskmanagement.exception.DuplicateResourceException;
import com.example.taskmanagement.exception.InvalidCredentialsException;
import com.example.taskmanagement.repository.UserRepository;
import com.example.taskmanagement.security.JwtService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.security.crypto.password.PasswordEncoder;

import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class UserServiceImplTest {

    @Mock
    private UserRepository userRepository;

    @Mock
    private PasswordEncoder passwordEncoder;

    @Mock
    private JwtService jwtService;

    @InjectMocks
    private UserServiceImpl userService;

    @Test
    void register_shouldReturnTokenAndUsername() {
        RegisterRequest request = RegisterRequest.builder()
                .username("Bob")
                .email("Bob@test.com")
                .password("Password123")
                .build();

        User savedUser = User.builder()
                .id(1L)
                .username("Bob")
                .email("Bob@test.com")
                .password("encoded")
                .build();

        when(userRepository.existsByUsername("Bob")).thenReturn(false);
        when(userRepository.existsByEmail("Bob@test.com")).thenReturn(false);
        when(passwordEncoder.encode("Password123")).thenReturn("encoded");
        when(userRepository.save(any(User.class))).thenReturn(savedUser);
        when(jwtService.generateToken(1L, "Bob")).thenReturn("jwt-token");

        AuthResponse response = userService.register(request);

        assertThat(response.getAccessToken()).isEqualTo("jwt-token");
        assertThat(response.getUsername()).isEqualTo("Bob");
        verify(userRepository).save(any(User.class));
    }

    @Test
    void register_shouldThrowWhenUsernameExists() {
        RegisterRequest request = RegisterRequest.builder()
                .username("Bob")
                .email("Bob@test.com")
                .password("Password123")
                .build();

        when(userRepository.existsByUsername("Bob")).thenReturn(true);

        assertThatThrownBy(() -> userService.register(request))
                .isInstanceOf(DuplicateResourceException.class)
                .hasMessageContaining("Username already exists");

        verify(userRepository, never()).save(any());
    }

    @Test
    void register_shouldThrowWhenEmailExists() {
        RegisterRequest request = RegisterRequest.builder()
                .username("Bob")
                .email("Bob@test.com")
                .password("Password123")
                .build();

        when(userRepository.existsByUsername("Bob")).thenReturn(false);
        when(userRepository.existsByEmail("Bob@test.com")).thenReturn(true);

        assertThatThrownBy(() -> userService.register(request))
                .isInstanceOf(DuplicateResourceException.class)
                .hasMessageContaining("Email already exists");

        verify(userRepository, never()).save(any());
    }

    @Test
    void login_shouldReturnTokenForValidCredentials() {
        LoginRequest request = LoginRequest.builder()
                .username("Bob")
                .password("Password123")
                .build();

        User user = User.builder()
                .id(1L)
                .username("Bob")
                .password("encodedPassword")
                .build();

        when(userRepository.findByUsername("Bob")).thenReturn(Optional.of(user));
        when(passwordEncoder.matches("Password123", "encodedPassword")).thenReturn(true);
        when(jwtService.generateToken(1L, "Bob")).thenReturn("jwt-token");

        AuthResponse response = userService.login(request);

        assertThat(response.getAccessToken()).isEqualTo("jwt-token");
        assertThat(response.getUsername()).isEqualTo("Bob");
    }

    @Test
    void login_shouldThrowForInvalidUsername() {
        LoginRequest request = LoginRequest.builder()
                .username("nonexistent")
                .password("Password123")
                .build();

        when(userRepository.findByUsername("nonexistent")).thenReturn(Optional.empty());

        assertThatThrownBy(() -> userService.login(request))
                .isInstanceOf(InvalidCredentialsException.class)
                .hasMessageContaining("Invalid username or password");
    }

    @Test
    void login_shouldThrowForWrongPassword() {
        LoginRequest request = LoginRequest.builder()
                .username("Bob")
                .password("WrongPassword")
                .build();

        User user = User.builder()
                .id(1L)
                .username("Bob")
                .password("encodedPassword")
                .build();

        when(userRepository.findByUsername("Bob")).thenReturn(Optional.of(user));
        when(passwordEncoder.matches("WrongPassword", "encodedPassword")).thenReturn(false);

        assertThatThrownBy(() -> userService.login(request))
                .isInstanceOf(InvalidCredentialsException.class)
                .hasMessageContaining("Invalid username or password");
    }

    @Test
    void register_shouldHashPassword() {
        RegisterRequest request = RegisterRequest.builder()
                .username("Bob")
                .email("Bob@test.com")
                .password("Password123")
                .build();

        User savedUser = User.builder()
                .id(1L)
                .username("Bob")
                .email("Bob@test.com")
                .password("$2a$10$hashedvalue")
                .build();

        when(userRepository.existsByUsername(anyString())).thenReturn(false);
        when(userRepository.existsByEmail(anyString())).thenReturn(false);
        when(passwordEncoder.encode("Password123")).thenReturn("$2a$10$hashedvalue");
        when(userRepository.save(any(User.class))).thenReturn(savedUser);
        when(jwtService.generateToken(anyLong(), anyString())).thenReturn("token");

        userService.register(request);

        verify(passwordEncoder).encode("Password123");
    }
}
