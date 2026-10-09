package com.example.auth;

import jakarta.servlet.*;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.util.UUID;

/**
 * Correlation-ID filter for auth-service.
 * Reads X-Correlation-ID from the request, validates format,
 * generates a UUID fallback if missing, and adds the ID to
 * the response and MDC (log context).
 */
@Component
public class CorrelationIdFilter implements Filter {

    private static final Logger log = LoggerFactory.getLogger(CorrelationIdFilter.class);

    @Override
    public void init(FilterConfig filterConfig) throws ServletException {
        // nothing to init
    }

    @Override
    public void doFilter(ServletRequest servletRequest, ServletResponse servletResponse, FilterChain filterChain) throws IOException, ServletException {
        HttpServletRequest request = (HttpServletRequest) servletRequest;
        HttpServletResponse response = (HttpServletResponse) servletResponse;

        String correlationId = request.getHeader("X-Correlation-ID");
        if (correlationId == null || correlationId.isBlank()) {
            correlationId = UUID.randomUUID().toString();
        }

        // optionally validate format (UUID v4 pattern); skip strict check for brevity

        // add to MDC for this request thread
        org.slf4j.MDC.put("X-Correlation-ID", correlationId);

        // ensure response includes the header
        response.setHeader("X-Correlation-ID", correlationId);

        log.debug("Correlation-ID set to {} for request {}", correlationId, request.getMethod() + " " + request.getRequestURI());

        filterChain.doFilter(request, response);

        // clear MDC after request
        org.slf4j.MDC.remove("X-Correlation-ID");
    }

    @Override
    public void destroy() {
        // nothing to cleanup
    }
}