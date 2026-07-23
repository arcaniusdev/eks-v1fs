# syntax=docker/dockerfile:1
#
# Java port of the V1FS scanning app. Multi-stage: build the shaded fat jar with
# Maven, then run it on a slim JRE as a non-root user. No arch-specific bits —
# the JVM makes the image multi-arch (amd64 / arm64) from one Dockerfile.

# --- build stage ---
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /build
# Cache dependencies first (pom rarely changes relative to source).
COPY pom.xml .
RUN mvn -q -e -B -DskipTests dependency:go-offline || true
COPY src ./src
RUN mvn -q -e -B -DskipTests package \
    && cp target/scanner-app.jar /scanner-app.jar

# --- runtime stage ---
FROM eclipse-temurin:17-jre-jammy AS runtime

# Non-root user (UID 999) — matches the Python image; read-only root fs safe.
RUN groupadd --gid 999 scanner \
    && useradd --uid 999 --gid 999 --no-create-home --shell /usr/sbin/nologin scanner

WORKDIR /app
COPY --from=build /scanner-app.jar /app/scanner-app.jar

USER 999:999

# Health/readiness probes are served on HEALTH_PORT (default 8080).
EXPOSE 8080

# Nothing is written outside /tmp; the JVM honors a read-only root filesystem.
# -XX:+UseContainerSupport (default on 17) sizes the heap to the pod cgroup.
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75.0", "-jar", "/app/scanner-app.jar"]
