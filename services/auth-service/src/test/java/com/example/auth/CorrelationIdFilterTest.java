package com.example.auth;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

import java.io.IOException;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;

class CorrelationIdFilterTest {

    private CorrelationIdFilter filter = new CorrelationIdFilter();

    @Test
    void testExistingHeaderPropagated() throws Exception {
        MockHttpServletRequest req = new MockHttpServletRequest();
        req.addHeader("X-Correlation-ID", "test-correlation-id");
        MockHttpServletResponse res = new MockHttpServletResponse();
        FilterChain chain = (request, response) -> {};

        filter.doFilter(req, res, chain);

        assertEquals("test-correlation-id", res.getHeader("X-Correlation-ID"));
    }

    @Test
    void testMissingHeaderGeneratesUUID() throws Exception {
        MockHttpServletRequest req = new MockHttpServletRequest();
        MockHttpServletResponse res = new MockHttpServletResponse();
        FilterChain chain = (request, response) -> {};

        filter.doFilter(req, res, chain);

        String correlation = res.getHeader("X-Correlation-ID");
        assertNotNull(correlation);
        assertFalse(correlation.isBlank());
        UUID.fromString(correlation); // valid UUID format
    }

    @Test
    void testEmptyHeaderGeneratesUUID() throws Exception {
        MockHttpServletRequest req = new MockHttpServletRequest();
        req.addHeader("X-Correlation-ID", "   ");
        MockHttpServletResponse res = new MockHttpServletResponse();
        FilterChain chain = (request, response) -> {};

        filter.doFilter(req, res, chain);

        String correlation = res.getHeader("X-Correlation-ID");
        assertNotNull(correlation);
        assertFalse(correlation.isBlank());
    }
}
