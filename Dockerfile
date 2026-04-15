# =============================================================================
# Multi-Stage Dockerfile for Task Management System
# =============================================================================
#
# WHY MULTI-STAGE?
# ─────────────────
# A multi-stage build uses TWO separate images:
#   Stage 1 (builder): A BIG image with Maven + JDK to compile our code
#   Stage 2 (runner):  A TINY image with only JRE to run the compiled JAR
#
# This means:
#   - Build tools (Maven, compiler) are NOT in the final image
#   - Final image is ~200MB instead of ~800MB
#   - Smaller image = faster deployment + smaller attack surface
#
# =============================================================================

# =============================================================================
# STAGE 1: BUILD
# =============================================================================
# We start FROM a full JDK image that includes Maven capabilities.
# "AS builder" gives this stage a name so we can reference it later.
FROM eclipse-temurin:17-jdk AS builder

# Set the working directory inside the container.
# All subsequent commands run relative to this path.
WORKDIR /app

# COPY dependency files first (pom.xml + Maven wrapper).
# WHY SEPARATE? Docker caches each layer. If pom.xml hasn't changed,
# Docker reuses the cached dependency layer — saves ~5min on rebuilds.
COPY pom.xml .
COPY mvnw .
COPY .mvn .mvn

# Make the Maven wrapper executable (it's a shell script)
RUN chmod +x mvnw

# Download all dependencies WITHOUT building the code.
# This layer gets cached and only re-runs if pom.xml changes.
# -B = batch mode (no interactive prompts)
# dependency:go-offline = download everything Maven needs
RUN ./mvnw dependency:go-offline -B

# NOW copy the source code. This layer changes on every code change,
# but the dependency layer above is still cached.
COPY src src

# Build the application, skip tests (tests ran in CI already).
# The result is a JAR file in target/
# -DskipTests = don't run tests again, CI already verified them
RUN ./mvnw package -B -DskipTests

# =============================================================================
# STAGE 2: RUN
# =============================================================================
# We use a JRE-only image (no compiler, no Maven) — much smaller!
# "eclipse-temurin:17-jre" is ~200MB vs ~400MB+ for the full JDK.
FROM eclipse-temurin:17-jre

# Add metadata labels to the image (best practice)
LABEL maintainer="taskmanagement-team"
LABEL version="1.0.0"
LABEL description="Task Management System API"

# Create a non-root user for security.
# WHY? Running as root inside a container is dangerous — if an attacker
# breaks into the container, they have root access to the container filesystem.
# A non-root user limits the damage.
RUN groupadd -r appgroup && useradd -r -g appgroup -d /app -s /sbin/nologin appuser

WORKDIR /app

# Copy ONLY the built JAR from Stage 1 (the builder).
# Everything else (source code, Maven, .m2 cache) is discarded.
# --from=builder refers to the first stage we named "builder".
COPY --from=builder /app/target/*.jar app.jar

# Change ownership to non-root user
RUN chown -R appuser:appgroup /app

# Switch to non-root user
USER appuser

# =============================================================================
# ENVIRONMENT VARIABLES
# =============================================================================
# These are DEFAULT values. They can be overridden at runtime using:
#   docker run -e SERVER_PORT=9090 ...
# or in docker-compose.yml.
#
# IMPORTANT: Never put real passwords here! These are just defaults.
# Real secrets should be passed via environment variables at runtime.
# =============================================================================
ENV SERVER_PORT=8082
ENV SPRING_PROFILES_ACTIVE=prod

# Tell Docker this container listens on port 8082.
# NOTE: EXPOSE doesn't actually publish the port — it's documentation.
# You still need -p 8082:8082 when running the container.
EXPOSE 8082

# =============================================================================
# HEALTH CHECK
# =============================================================================
# Docker will periodically hit this URL to check if the app is alive.
# If it fails 3 times in a row, Docker marks the container as "unhealthy".
# --interval=30s  = check every 30 seconds
# --timeout=10s   = wait up to 10 seconds for a response
# --retries=3     = mark unhealthy after 3 consecutive failures
# --start-period=40s = give the app 40 seconds to start up before checking
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=40s \
  CMD curl -f http://localhost:8082/actuator/health || exit 1

# =============================================================================
# ENTRYPOINT
# =============================================================================
# ENTRYPOINT vs CMD:
#   ENTRYPOINT = the command that ALWAYS runs (can't be easily overridden)
#   CMD = default arguments (can be overridden)
#
# We use ENTRYPOINT in "exec form" (JSON array) because:
#   1. The Java process becomes PID 1 (receives signals directly)
#   2. Ctrl+C / docker stop sends SIGTERM to Java (graceful shutdown)
#   3. No shell interpretation issues
#
# JVM FLAGS EXPLAINED:
#   -XX:+UseContainerSupport  → JVM respects Docker memory limits
#   -XX:MaxRAMPercentage=75.0 → Use max 75% of container's memory for heap
#   -Djava.security.egd=...   → Faster random number generation (startup speed)
ENTRYPOINT ["java", \
  "-XX:+UseContainerSupport", \
  "-XX:MaxRAMPercentage=75.0", \
  "-Djava.security.egd=file:/dev/./urandom", \
  "-jar", "app.jar"]
