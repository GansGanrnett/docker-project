package com.example.auth;

import org.junit.jupiter.api.Test;
import org.springframework.core.io.FileSystemResource;

import static org.assertj.core.api.Assertions.assertThatThrownBy;

public class JwtConfigStartupTest {

    @Test
    void startupFailsClearlyWhenPrivatePemMissing() throws Exception {
        JwtConfig config = new JwtConfig();
        // Reflective set to simulate missing file (no real secret needed)
        java.lang.reflect.Field f = JwtConfig.class.getDeclaredField("privateKeyResource");
        f.setAccessible(true);
        f.set(config, new FileSystemResource("/nonexistent/private.pem"));

        assertThatThrownBy(() -> config.jwtAlgorithm())
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("private.pem missing")
                .hasMessageContaining("docs/ARCHITECTURE.md §1");
    }
}
