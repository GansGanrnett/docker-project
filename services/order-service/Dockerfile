ARG REGISTRY=mcr.microsoft.com
FROM ${REGISTRY}/dotnet/sdk:8.0 AS builder
WORKDIR /source
# Явный путь к проекту и к restore: в каталоге сервиса теперь лежит
# tests/ со своим .csproj, и "dotnet publish" без аргумента перестаёт
# однозначно выбирать проект. Образ не должен зависеть от того, как
# MSBuild разберётся с двумя файлами в одной папке.
COPY OrderService.csproj ./
RUN dotnet restore OrderService.csproj
COPY . .
RUN dotnet publish OrderService.csproj -c Release -o /app --no-restore

ARG REGISTRY=mcr.microsoft.com
FROM ${REGISTRY}/dotnet/aspnet:8.0
WORKDIR /app
# The base image ships perl-base 5.36.0-7+deb12u3; CVE-2026-13221,
# CVE-2026-42496 and CVE-2026-8376 are fixed only in deb12u4. A plain
# "apt-get install" does not upgrade already-installed packages, so
# upgrade explicitly at build time.
RUN apt-get update && apt-get upgrade -y && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app .

EXPOSE 8084
# Wildcard "+" fails to bind in this container (IPv6 loopback only, no global
# address): KestrelServerImpl.BindAsync throws TaskCanceledException and the
# host dies before serving. Bind the concrete IPv4 address instead.
ENV ASPNETCORE_URLS=http://0.0.0.0:8084
ENTRYPOINT ["dotnet", "OrderService.dll"]
