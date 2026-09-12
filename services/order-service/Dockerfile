ARG REGISTRY=mcr.microsoft.com
FROM ${REGISTRY}/dotnet/sdk:8.0 AS builder
WORKDIR /source
COPY *.csproj .
RUN dotnet restore
COPY . .
RUN dotnet publish -c Release -o /app --no-restore

ARG REGISTRY=mcr.microsoft.com
FROM ${REGISTRY}/dotnet/aspnet:8.0
WORKDIR /app
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app .

EXPOSE 8084
ENV ASPNETCORE_URLS=http://+:8084
ENTRYPOINT ["dotnet", "OrderService.dll"]
