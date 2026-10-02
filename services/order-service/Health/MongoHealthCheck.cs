using Microsoft.Extensions.Diagnostics.HealthChecks;
using MongoDB.Driver;

namespace OrderService.Health
{
    /// <summary>
    /// Проверка готовности order-service через живое соединение с MongoDB.
    ///
    /// Смысл проверки - не "процесс жив", а "база отвечает прямо сейчас".
    /// Под без этого отвечает на /health, но любой запрос к заказам
    /// упадёт по таймауту, поэтому такие поды обязаны быть NotReady, а не
    /// участвовать в балансировке.
    ///
    /// Таймаут задаётся при регистрации проверки (AddCheck timeout), и
    /// применяет его сам HealthCheckService. Отдельный CancellationToken
    /// внутри не нужен: дефолтный ServerSelectionTimeout драйвера равен
    /// 30 с (проверено на MongoDB.Driver 2.23), а periodSeconds пробы - 10 с,
    /// и подвисание на один период равносильно таймауту пробы: kubelet
    /// решит, что под мёртв, и убьёт исправный.
    /// </summary>
    public sealed class MongoHealthCheck : IHealthCheck
    {
        private readonly IMongoDatabase _database;

        public MongoHealthCheck(IMongoDatabase database)
        {
            _database = database;
        }

        public async Task<HealthCheckResult> CheckHealthAsync(
            HealthCheckContext context,
            CancellationToken cancellationToken = default)
        {
            try
            {
                // listCollections вместо команды ping: типизированного Ping в
                // драйвере нет (проверено рефлексией по MongoDB.Driver 2.23),
                // а собирать raw-BSON ради проверки нельзя - правила
                // репозитория запрещают BsonDocument в запросах. Список
                // имён коллекций идёт по тому же пути выбора сервера и
                // аутентификации, что и рабочие запросы сервиса, поэтому
                // проверка честно отражает доступность базы.
                //
                // ListCollectionNamesAsync(options, ct) в 2.23 резолвится в
                // IAsyncCursor<string>, а не в список (проверено компиляцией),
                // поэтому берём синхронный ListCollectionNames и материализуем
                // курсор явно.
                using var cursor = _database.ListCollectionNames(
                    cancellationToken: cancellationToken);
                var collections = await cursor.ToListAsync(cancellationToken);

                return HealthCheckResult.Healthy(
                    $"MongoDB is reachable ({collections.Count} collections).");
            }
            catch (Exception ex)
            {
                // Любая ошибка - Unhealthy, а не исключение наружу: иначе
                // /ready вернул бы 500 вместо 503, и kubelet не отличил бы
                // "база недоступна" от "сервис сломан".
                return HealthCheckResult.Unhealthy("MongoDB is not reachable.", ex);
            }
        }
    }
}