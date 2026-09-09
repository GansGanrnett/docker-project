FROM ://microsoft.com AS builder
WORKDIR /app
COPY src/OrderService/*.csproj ./src/OrderService/
RUN dotnet restore ./src/OrderService/OrderService.csproj
COPY . .
RUN dotnet publish src/OrderService/OrderService.csproj -c Release -o out

FROM ://microsoft.com
WORKDIR /app
COPY --from=builder /app/out .
EXPOSE 8080
CMD ["dotnet", "OrderService.dll"]
