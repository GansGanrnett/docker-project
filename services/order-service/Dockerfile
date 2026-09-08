FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /app

# Копируем только .csproj и восстанавливаем зависимости
COPY src/OrderService/*.csproj ./src/OrderService/
RUN dotnet restore ./src/OrderService/OrderService.csproj

# Копируем весь остальной код
COPY . .

# Собираем и публикуем
WORKDIR /app/src/OrderService
RUN dotnet publish -c Release -o out

# Финальный образ
FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app/src/OrderService/out .
EXPOSE 8084
ENTRYPOINT ["dotnet", "OrderService.dll"]
