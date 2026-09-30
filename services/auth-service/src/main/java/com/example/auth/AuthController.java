package com.example.auth;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.HashMap;
import java.util.Map;

@RestController
public class AuthController {

    private final JwtService jwtService;

    public AuthController(JwtService jwtService) {
        this.jwtService = jwtService;
    }

    @PostMapping("/login")
    public ResponseEntity<?> login(@RequestBody Map<String, String> loginRequest) {
        String username = loginRequest.get("username");
        String password = loginRequest.get("password");

        // Учётные данные берутся из окружения (см. .env / compose), не хардкодятся
        String adminUsername = System.getenv().getOrDefault("AUTH_ADMIN_USER", "admin");
        String adminPassword = System.getenv().getOrDefault("AUTH_ADMIN_PASSWORD", "");
        if (!adminPassword.isEmpty() && adminUsername.equals(username) && adminPassword.equals(password)) {
            String token = jwtService.generateToken(username, "ROLE_ADMIN");

            Map<String, String> response = new HashMap<>();
            response.put("access_token", token);
            response.put("token_type", "Bearer");

            return ResponseEntity.ok(response);
        }

        Map<String, String> error = new HashMap<>();
        error.put("error", "Unauthorized. Invalid username or password.");
        return ResponseEntity.status(401).body(error);
    }
}
