package com.example.auth;

import com.auth0.jwt.JWT;
import com.auth0.jwt.algorithms.Algorithm;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.util.Date;

@Service
public class JwtService {

    private final Algorithm algorithm;
    private final String issuer;
    private final long expiresInMs;

    public JwtService(Algorithm algorithm, 
                      @Value("${jwt.issuer}") String issuer, 
                      @Value("${jwt.expires-in-ms}") long expiresInMs) {
        this.algorithm = algorithm;
        this.issuer = issuer;
        this.expiresInMs = expiresInMs;
    }

    public String generateToken(String username, String role) {
        Date now = new Date();
        Date expiryDate = new Date(now.getTime() + expiresInMs);

        return JWT.create()
                .withIssuer(issuer)
                .withSubject(username)
                .withClaim("role", role)
                .withIssuedAt(now)
                .withExpiresAt(expiryDate)
                .sign(algorithm);
    }
}
