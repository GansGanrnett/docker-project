FROM alpine:3.19
RUN apk add --no-cache curl
RUN echo "auth-service is successfully built via GitHub Actions" > /app_status.txt
CMD ["cat", "/app_status.txt"]
# Trigger CI Build v1
