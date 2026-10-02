package com.example.auth;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * The service used to carry its own {@code @GetMapping("/health")} endpoint
 * that answered with a plain-text string no probe group knew about. Actuator
 * owns both probe paths now; a second, differently shaped health endpoint only
 * gave the Helm probes a second thing to disagree about.
 */
@SpringBootApplication
public class AuthApplication {
    public static void main(String[] args) {
        SpringApplication.run(AuthApplication.class, args);
    }
}