FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /app
COPY OrderService.sln .
COPY OrderService/*.csproj ./OrderService/
RUN dotnet restore
COPY . .
WORKDIR /app/OrderService
RUN dotnet publish -c Release -o out

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app/OrderService/out .
EXPOSE 8084
ENTRYPOINT ["dotnet", "OrderService.dll"]

