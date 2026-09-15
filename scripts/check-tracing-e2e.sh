#!/bin/bash
TEST_ID="check-auto-$(date +%s)"
echo "Executing tracking check for ID: $TEST_ID"
TOKEN=$(curl -s -X POST http://localhost:30080/api/v1/auth/login -H "X-Correlation-ID: $TEST_ID" -H "Content-Type: application/json" -d '{"username":"admin", "password":"secure_pass"}' | grep -oP '"access_token":"\K[^"]+')
if [ -z "$TOKEN" ]; then echo "Verification failed"; exit 1; fi
echo "Verification successfully initiated"
