package com.example.auth;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.health.contributor.Health;
import org.springframework.boot.health.contributor.HealthIndicator;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpStatus;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * auth-service had no tests at all, so the probe contract lived only in YAML.
 * This checks the part that is easy to break silently: the readiness group must
 * contain db, /ready must answer 503 while the database is down, and liveness
 * must stay green through the same outage so that kubelet keeps the pod alive
 * instead of restart-looping it.
 *
 * <p>The context is deliberately narrow. Component scanning would pull in
 * JwtConfig, which reads an RSA private key from configs/private.pem at startup
 * - a key that must never be committed, so the test cannot provide it. The
 * health groups under test still come from the real application.yml, because
 * Boot loads it from the classpath no matter which class starts the context.
 *
 * <p>The db indicator is a test double registered under the name "db". It
 * stands in for the real DataSource indicator so the suite needs no Postgres;
 * what is being tested is the grouping and the status codes, not the driver.
 *
 * <p>Boot 4 removed {@code TestRestTemplate}, so the probes are called through
 * the JDK {@link HttpClient} against the random test port. The behaviour under
 * test - status codes on the application port - is unchanged.
 */
@SpringBootTest(
        classes = ReadinessProbeTest.ProbeContext.class,
        webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {
                // Without this the auto-configured Postgres DataSource would
                // build its own db indicator, and overriding a bean by that
                // name would fail the context before any assertion ran.
                "spring.autoconfigure.exclude="
                        + "org.springframework.boot.jdbc.autoconfigure.DataSourceAutoConfiguration,"
                        + "org.springframework.boot.hibernate.autoconfigure.HibernateJpaAutoConfiguration"
        })
class ReadinessProbeTest {

    private static final AtomicBoolean DATABASE_UP = new AtomicBoolean(true);

    @LocalServerPort
    private int port;

    private final HttpClient http = HttpClient.newHttpClient();

    @AfterEach
    void resetIndicator() {
        DATABASE_UP.set(true);
    }

    @Configuration(proxyBeanMethods = false)
    @EnableAutoConfiguration
    static class ProbeContext {
        @Bean
        HealthIndicator db() {
            return () -> DATABASE_UP.get()
                    ? Health.up().build()
                    : Health.down().build();
        }
    }

    private int statusOf(String path) {
        try {
            HttpResponse<String> response = http.send(
                    HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                            .GET()
                            .build(),
                    HttpResponse.BodyHandlers.ofString());
            return response.statusCode();
        } catch (Exception e) {
            throw new IllegalStateException("GET " + path + " failed", e);
        }
    }

    @Test
    void livenessStaysGreenWhileTheDatabaseIsDown() {
        DATABASE_UP.set(false);

        assertThat(statusOf("/health"))
                .as("a database outage must not restart every auth pod")
                .isEqualTo(HttpStatus.OK.value());
    }

    @Test
    void readinessReports503WhileTheDatabaseIsDown() {
        DATABASE_UP.set(false);

        assertThat(statusOf("/ready"))
                .as("traffic must not reach an auth service that cannot serve it")
                .isEqualTo(HttpStatus.SERVICE_UNAVAILABLE.value());
    }

    @Test
    void readinessReports200OnceTheDatabaseIsUp() {
        DATABASE_UP.set(true);

        assertThat(statusOf("/ready")).isEqualTo(HttpStatus.OK.value());
    }

    @Test
    void probePathsAreServedOnTheApplicationPort() {
        // The Helm probes hit /health and /ready, not /actuator/... - if the
        // additional-path mapping is dropped, these return 404 while
        // /actuator/health/... keeps working and looks perfectly healthy.
        assertThat(statusOf("/health")).isEqualTo(HttpStatus.OK.value());
        assertThat(statusOf("/ready")).isEqualTo(HttpStatus.OK.value());
    }
}
