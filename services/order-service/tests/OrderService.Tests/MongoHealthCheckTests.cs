using Microsoft.Extensions.Diagnostics.HealthChecks;
using MongoDB.Driver;
using Moq;
using OrderService.Health;
using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using Xunit;

namespace OrderService.Tests
{
    /// <summary>
    /// /ready обязан отражать доступность MongoDB, а не только факт, что
    /// процесс жив. До issue #36 у order-service не было /ready вовсе, а
    /// единственный health-эндпоинт жил под префиксом /api/v1/orders/health,
    /// куда пробы kubelet (/health) не ходили и получали 404.
    ///
    /// Тесты против настоящего MongoDB помечены [MongoFact] и пропускаются
    /// без него; тесты на поведение при ошибке драйвера идут на заглушке,
    /// потому что ошибку надо подделать, а не дождаться.
    /// </summary>
    public class MongoHealthCheckTests
    {
        private static async Task<HealthCheckResult> CheckAsync(IMongoDatabase database)
        {
            var check = new MongoHealthCheck(database);
            return await check.CheckHealthAsync(
                new HealthCheckContext(), CancellationToken.None);
        }

        [MongoFact]
        public async Task Healthy_when_mongo_is_reachable()
        {
            using var fixture = new MongoFixture();
            var scope = fixture.NewScope();

            var result = await CheckAsync(scope.Database);

            Assert.Equal(HealthStatus.Healthy, result.Status);
            Assert.Contains("reachable", result.Description);
        }

        [MongoFact]
        public async Task Unhealthy_when_nothing_listens_on_port()
        {
            using var fixture = new MongoFixture();

            // Порт, где никто не слушает: тот же драйвер и те же настройки,
            // но сервера нет. Проверка обязана сообщить Unhealthy, а не
            // бросить исключение наружу - иначе /ready отдал бы 500, и
            // kubelet не отличил бы "база недоступна" от "сервис сломан".
            var settings = MongoClientSettings.FromUrl(
                new MongoUrl("mongodb://127.0.0.1:1/"));
            settings.ServerSelectionTimeout = TimeSpan.FromMilliseconds(500);
            var unreachable = new MongoClient(settings);

            var result = await CheckAsync(unreachable.GetDatabase("order_db"));

            Assert.Equal(HealthStatus.Unhealthy, result.Status);
            Assert.NotNull(result.Exception);
        }

        [Fact]
        public async Task Unhealthy_when_driver_throws()
        {
            // Драйвер - внешняя зависимость, и его исключения не должны
            // выходить наружу: иначе HealthCheckService превратит их в
            // 500 вместо 503.
            var database = new Mock<IMongoDatabase>();
            database
                .Setup(d => d.ListCollectionNames(
                    It.IsAny<ListCollectionNamesOptions>(),
                    It.IsAny<CancellationToken>()))
                .Throws(new TimeoutException("server selection timed out"));

            var result = await CheckAsync(database.Object);

            Assert.Equal(HealthStatus.Unhealthy, result.Status);
            Assert.IsType<TimeoutException>(result.Exception);
        }

        [Fact]
        public async Task Healthy_when_driver_returns_cursor()
        {
            var cursor = new Mock<IAsyncCursor<string>>();
            cursor.Setup(c => c.MoveNextAsync(It.IsAny<CancellationToken>()))
                  .ReturnsAsync(false);

            var database = new Mock<IMongoDatabase>();
            database
                .Setup(d => d.ListCollectionNames(
                    It.IsAny<ListCollectionNamesOptions>(),
                    It.IsAny<CancellationToken>()))
                .Returns(cursor.Object);

            var result = await CheckAsync(database.Object);

            Assert.Equal(HealthStatus.Healthy, result.Status);
        }

        [Fact]
        public async Task Unhealthy_when_check_is_cancelled()
        {
            // Проба не должна висеть дольше отведённого времени: иначе
            // подвисание равносильно таймауту пробы, и kubelet убьёт
            // исправный под. Проверка обязана передавать токен драйверу и
            // превращать отмену в Unhealthy, а не в исключение наружу.
            var observed = new List<CancellationToken>();
            var database = new Mock<IMongoDatabase>();
            database
                .Setup(d => d.ListCollectionNames(
                    It.IsAny<ListCollectionNamesOptions>(),
                    It.IsAny<CancellationToken>()))
                .Returns((ListCollectionNamesOptions _, CancellationToken token) =>
                {
                    observed.Add(token);
                    throw new OperationCanceledException(token);
                });

            using var cts = new CancellationTokenSource();
            cts.Cancel();

            var check = new MongoHealthCheck(database.Object);
            var result = await check.CheckHealthAsync(
                new HealthCheckContext(), cts.Token);

            Assert.Equal(HealthStatus.Unhealthy, result.Status);
            Assert.Single(observed);
            Assert.True(observed[0].IsCancellationRequested,
                "токен отмены должен доходить до драйвера");
        }
    }
}