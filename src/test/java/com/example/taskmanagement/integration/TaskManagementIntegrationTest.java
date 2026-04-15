package com.example.taskmanagement.integration;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;

import static org.hamcrest.Matchers.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@SpringBootTest
@AutoConfigureMockMvc
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TaskManagementIntegrationTest {

    @Autowired
    private MockMvc mockMvc;

    private final ObjectMapper objectMapper = new ObjectMapper();

    private static String accessToken;
    private static String accessToken2;
    private static Long taskId;

    @BeforeAll
    static void init() {
        accessToken = null;
        accessToken2 = null;
        taskId = null;
    }

    // ==================== AUTH TESTS ====================

    @Test
    @Order(1)
    void register_shouldReturn201AndToken() throws Exception {
        String body = """
                {
                  "username": "Bob",
                  "email": "Bob@test.com",
                  "password": "Password123"
                }
                """;

        MvcResult result = mockMvc.perform(post("/api/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.accessToken").isNotEmpty())
                .andExpect(jsonPath("$.username").value("Bob"))
                .andExpect(jsonPath("$.password").doesNotExist())
                .andReturn();

        JsonNode json = objectMapper.readTree(result.getResponse().getContentAsString());
        accessToken = json.get("accessToken").asText();
    }

    @Test
    @Order(2)
    void register_duplicateEmail_shouldReturn409() throws Exception {
        String body = """
                {
                  "username": "Bob",
                  "email": "Bob@test.com",
                  "password": "Password123"
                }
                """;

        mockMvc.perform(post("/api/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.message").value(containsString("already exists")));
    }

    @Test
    @Order(3)
    void register_invalidInput_shouldReturn400() throws Exception {
        String body = """
                {
                  "username": "",
                  "email": "invalid",
                  "password": "12"
                }
                """;

        mockMvc.perform(post("/api/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value("Validation failed"))
                .andExpect(jsonPath("$.errors").isMap());
    }

    @Test
    @Order(4)
    void login_shouldReturn200AndToken() throws Exception {
        String body = """
                {
                  "username": "Bob",
                  "password": "Password123"
                }
                """;

        MvcResult result = mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.accessToken").isNotEmpty())
                .andExpect(jsonPath("$.username").value("Bob"))
                .andReturn();

        JsonNode json = objectMapper.readTree(result.getResponse().getContentAsString());
        accessToken = json.get("accessToken").asText();
    }

    @Test
    @Order(5)
    void login_invalidCredentials_shouldReturn401() throws Exception {
        String body = """
                {
                  "username": "Bob",
                  "password": "WrongPassword"
                }
                """;

        mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value(containsString("Invalid")));
    }

    @Test
    @Order(6)
    void login_invalidInput_shouldReturn400() throws Exception {
        String body = """
                {
                  "username": "",
                  "password": ""
                }
                """;

        mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value("Validation failed"))
                .andExpect(jsonPath("$.errors").isMap());
    }

    // ==================== TASK CRUD TESTS ====================

    @Test
    @Order(10)
    void createTask_shouldReturn201() throws Exception {
        String body = """
                {
                  "title": "Prepare presentation",
                  "description": "Phase 3 review",
                  "dueDate": "2026-12-28"
                }
                """;

        MvcResult result = mockMvc.perform(post("/api/tasks")
                        .header("Authorization", "Bearer " + accessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.id").isNumber())
                .andExpect(jsonPath("$.title").value("Prepare presentation"))
                .andExpect(jsonPath("$.description").value("Phase 3 review"))
                .andExpect(jsonPath("$.completed").value(false))
                .andExpect(jsonPath("$.createdAt").isNotEmpty())
                .andReturn();

        JsonNode json = objectMapper.readTree(result.getResponse().getContentAsString());
        taskId = json.get("id").asLong();
    }

    @Test
    @Order(11)
    void createTask_validationError_shouldReturn400() throws Exception {
        String body = """
                {
                  "title": "",
                  "description": "No title"
                }
                """;

        mockMvc.perform(post("/api/tasks")
                        .header("Authorization", "Bearer " + accessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.errors.title").exists());
    }

    @Test
    @Order(11)
    void createTask_pastDueDate_shouldReturn400() throws Exception {
        String body = """
                {
                  "title": "Past date task",
                  "description": "Should fail",
                  "dueDate": "2020-01-01"
                }
                """;

        mockMvc.perform(post("/api/tasks")
                        .header("Authorization", "Bearer " + accessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.errors.dueDate").exists());
    }

    @Test
    @Order(12)
    void getTasks_shouldReturnPaginated() throws Exception {
        mockMvc.perform(get("/api/tasks")
                        .param("page", "0")
                        .param("size", "5")
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.content").isArray())
                .andExpect(jsonPath("$.content", hasSize(greaterThanOrEqualTo(1))))
                .andExpect(jsonPath("$.totalElements").isNumber())
                .andExpect(jsonPath("$.totalPages").isNumber())
                .andExpect(jsonPath("$.size").value(5));
    }

    @Test
    @Order(13)
    void getTaskById_shouldReturnTask() throws Exception {
        mockMvc.perform(get("/api/tasks/" + taskId)
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.id").value(taskId))
                .andExpect(jsonPath("$.title").value("Prepare presentation"));
    }

    @Test
    @Order(14)
    void getTaskById_notFound_shouldReturn404() throws Exception {
        mockMvc.perform(get("/api/tasks/99999")
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value(containsString("not found")));
    }

    @Test
    @Order(20)
    void updateTask_shouldReturn200() throws Exception {
        String body = """
                {
                  "title": "Updated presentation",
                  "description": "Updated phase 3 review",
                  "dueDate": "2026-12-30"
                }
                """;

        mockMvc.perform(put("/api/tasks/" + taskId)
                        .header("Authorization", "Bearer " + accessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.title").value("Updated presentation"))
                .andExpect(jsonPath("$.description").value("Updated phase 3 review"));
    }

    @Test
    @Order(21)
    void updateTask_notFound_shouldReturn404() throws Exception {
        String body = """
                {
                  "title": "Nonexistent",
                  "description": "Does not exist",
                  "dueDate": "2026-12-30"
                }
                """;

        mockMvc.perform(put("/api/tasks/99999")
                        .header("Authorization", "Bearer " + accessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isNotFound());
    }

    @Test
    @Order(22)
    void patchTask_shouldPartiallyUpdate() throws Exception {
        String body = """
                {
                  "description": "Patched description only"
                }
                """;

        mockMvc.perform(patch("/api/tasks/" + taskId)
                        .header("Authorization", "Bearer " + accessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.description").value("Patched description only"))
                .andExpect(jsonPath("$.title").value("Updated presentation"));
    }

    @Test
    @Order(23)
    void getTasks_filterByTitle_shouldReturnMatching() throws Exception {
        mockMvc.perform(get("/api/tasks")
                        .param("title", "Updated")
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.content").isArray())
                .andExpect(jsonPath("$.content[0].title", containsStringIgnoringCase("Updated")));
    }

    @Test
    @Order(30)
    void markComplete_shouldReturn200() throws Exception {
        mockMvc.perform(patch("/api/tasks/" + taskId + "/complete")
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.completed").value(true));
    }

    @Test
    @Order(31)
    void markComplete_alreadyCompleted_shouldReturn400() throws Exception {
        mockMvc.perform(patch("/api/tasks/" + taskId + "/complete")
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value(containsString("already completed")));
    }

    @Test
    @Order(32)
    void patchTask_alreadyCompleted_shouldReturn400() throws Exception {
        String body = """
                {
                  "title": "Try to edit completed task"
                }
                """;

        mockMvc.perform(patch("/api/tasks/" + taskId)
                        .header("Authorization", "Bearer " + accessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value(containsString("already completed")));
    }

    @Test
    @Order(33)
    void getTasks_filterByCompleted_shouldReturnFiltered() throws Exception {
        mockMvc.perform(get("/api/tasks")
                        .param("completed", "true")
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.content").isArray())
                .andExpect(jsonPath("$.content[0].completed").value(true));
    }

    @Test
    @Order(40)
    void deleteTask_shouldReturn204() throws Exception {
        mockMvc.perform(delete("/api/tasks/" + taskId)
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isNoContent());
    }

    @Test
    @Order(41)
    void getDeletedTask_shouldReturn404() throws Exception {
        mockMvc.perform(get("/api/tasks/" + taskId)
                        .header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isNotFound());
    }

    // ==================== SECURITY TESTS ====================

    @Test
    @Order(50)
    void accessWithoutToken_shouldReturn401or403() throws Exception {
        mockMvc.perform(get("/api/tasks")
                        .param("page", "0")
                        .param("size", "5"))
                .andExpect(status().is(anyOf(equalTo(401), equalTo(403))));
    }

    @Test
    @Order(51)
    void accessWithInvalidToken_shouldReturn401or403() throws Exception {
        mockMvc.perform(get("/api/tasks")
                        .param("page", "0")
                        .param("size", "5")
                        .header("Authorization", "Bearer invalid.jwt.token"))
                .andExpect(status().is(anyOf(equalTo(401), equalTo(403))));
    }

    @Test
    @Order(60)
    void userCannotAccessAnotherUsersTask() throws Exception {
        // Register a second user
        String registerBody = """
                {
                  "username": "OtherUser",
                  "email": "other@test.com",
                  "password": "Password123"
                }
                """;

        MvcResult regResult = mockMvc.perform(post("/api/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(registerBody))
                .andExpect(status().isCreated())
                .andReturn();

        JsonNode regJson = objectMapper.readTree(regResult.getResponse().getContentAsString());
        accessToken2 = regJson.get("accessToken").asText();

        // User 1 creates a task
        String taskBody = """
                {
                  "title": "Private task",
                  "description": "Only for Bob",
                  "dueDate": "2026-12-28"
                }
                """;

        MvcResult taskResult = mockMvc.perform(post("/api/tasks")
                        .header("Authorization", "Bearer " + accessToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(taskBody))
                .andExpect(status().isCreated())
                .andReturn();

        JsonNode taskJson = objectMapper.readTree(taskResult.getResponse().getContentAsString());
        Long privateTaskId = taskJson.get("id").asLong();

        // User 2 tries to access User 1's task → should get 404 (not found for that user)
        mockMvc.perform(get("/api/tasks/" + privateTaskId)
                        .header("Authorization", "Bearer " + accessToken2))
                .andExpect(status().isNotFound());

        // User 2 tries to delete User 1's task → should get 404
        mockMvc.perform(delete("/api/tasks/" + privateTaskId)
                        .header("Authorization", "Bearer " + accessToken2))
                .andExpect(status().isNotFound());

        // User 2 tries to update User 1's task → should get 404
        String updateBody = """
                {
                  "title": "Hijack attempt",
                  "description": "Should fail",
                  "dueDate": "2026-12-28"
                }
                """;
        mockMvc.perform(put("/api/tasks/" + privateTaskId)
                        .header("Authorization", "Bearer " + accessToken2)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(updateBody))
                .andExpect(status().isNotFound());

        // User 2 tries to patch User 1's task → should get 404
        String patchBody = """
                {
                  "title": "Patch hijack"
                }
                """;
        mockMvc.perform(patch("/api/tasks/" + privateTaskId)
                        .header("Authorization", "Bearer " + accessToken2)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(patchBody))
                .andExpect(status().isNotFound());
    }
}
