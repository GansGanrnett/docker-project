#!/bin/bash
set -e
for svc in auth-service catalog-service cart-service order-service payment-service api-gateway; do
  docker build -t project-root/$svc:latest -f services/$svc/Dockerfile services/$svc
done

