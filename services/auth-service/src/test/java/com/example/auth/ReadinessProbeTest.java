package com.example.auth;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.actuate.health.Health;
import org.springframework.boot.actuate.health.HealthIndicator;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;

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
 */
@SpringBootTest(
        classes = ReadinessProbeTest.ProbeContext.class,
        webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {
                // Without this the auto-configured Postgres DataSource would
                // build its own db indicator, and overriding a bean by that
                // name would fail the context before any assertion ran.
                "spring.autoconfigure.exclude="
                        + "org.springframework.boot.autoconfigure.jdbc.DataSourceAutoConfiguration,"
                        + "org.springframework.boot.autoconfigure.orm.jpa.HibernateJpaAutoConfiguration"
        })
class ReadinessProbeTest {

    private static final AtomicBoolean DATABASE_UP = new AtomicBoolean(true);

    @Autowired
    private TestRestTemplate rest;

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

    @Test
    void livenessStaysGreenWhileTheDatabaseIsDown() {
        DATABASE_UP.set(false);

        ResponseEntity<String> response = rest.getForEntity("/health", String.class);

        assertThat(response.getStatusCode())
                .as("a database outage must not restart every auth pod")
                .isEqualTo(HttpStatus.OK);
    }

    @Test
    void readinessReports503WhileTheDatabaseIsDown() {
        DATABASE_UP.set(false);

        ResponseEntity<String> response = rest.getForEntity("/ready", String.class);

        assertThat(response.getStatusCode())
                .as("traffic must not reach an auth service that cannot serve it")
                .isEqualTo(HttpStatus.SERVICE_UNAVAILABLE);
    }

    @Test
    void readinessReports200OnceTheDatabaseIsUp() {
        DATABASE_UP.set(true);

        ResponseEntity<String> response = rest.getForEntity("/ready", String.class);

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.OK);
    }

    @Test
    void probePathsAreServedOnTheApplicationPort() {
        // The Helm probes hit /health and /ready, not /actuator/... - if the
        // additional-path mapping is dropped, these return 404 while
        // /actuator/health/... keeps working and looks perfectly healthy.
        assertThat(rest.getForEntity("/health", String.class).getStatusCode())
                .isEqualTo(HttpStatus.OK);
        assertThat(rest.getForEntity("/ready", String.class).getStatusCode())
                .isEqualTo(HttpStatus.OK);
    }
}