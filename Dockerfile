
FROM eclipse-temurin:17-jdk AS builder


WORKDIR /app


COPY pom.xml .
COPY checkstyle.xml .
COPY mvnw .
COPY .mvn .mvn

# Make the Maven wrapper executable (it's a shell script)
RUN chmod +x mvnw


# dependency:go-offline = download everything Maven needs
RUN ./mvnw dependency:go-offline -B

COPY src src


RUN ./mvnw package -B -DskipTests


FROM eclipse-temurin:17-jre


LABEL maintainer="taskmanagement-team"
LABEL version="1.0.0"
LABEL description="Task Management System API"

RUN apt-get update \
  && apt-get install -y --no-install-recommends curl \
  && rm -rf /var/lib/apt/lists/*

RUN groupadd -r appgroup && useradd -r -g appgroup -d /app -s /sbin/nologin appuser

WORKDIR /app


COPY --from=builder /app/target/*.jar app.jar


RUN chown -R appuser:appgroup /app


USER appuser


ENV SERVER_PORT=8082



EXPOSE 8082


HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=40s \
  CMD curl -f http://localhost:8082/actuator/health || exit 1



ENTRYPOINT ["java", \
  "-XX:+UseContainerSupport", \
  "-XX:MaxRAMPercentage=75.0", \
  "-Djava.security.egd=file:/dev/./urandom", \
  "-jar", "app.jar"]