using MongoDB.Bson;
using MongoDB.Driver;
using System;
using Xunit;

namespace OrderService.Tests
{
    /// <summary>
    /// Живая MongoDB на время тестов. Каждый прогон работает в базе с
    /// уникальным именем, поэтому тесты не задевают данные разработчика и
    /// не мешают друг другу на уровне базы.
    ///
    /// Если MongoDB недоступна, интеграционные тесты пропускаются, а не падают:
    /// без базы проверять переходы статусов нечем, но и рушить сборку из-за
    /// отсутствия локального Docker не надо. В CI сервис MongoDB обязателен,
    /// и пропусков там быть не должно.
    /// </summary>
    public sealed class MongoFixture : IDisposable
    {
        private readonly IMongoDatabase? _database;

        public MongoFixture()
        {
            var url = Environment.GetEnvironmentVariable("TEST_MONGO_URL")
                ?? Environment.GetEnvironmentVariable("MONGO_URL");

            if (!string.IsNullOrEmpty(url))
            {
                try
                {
                    var client = new MongoClient(url);
                    // Проверяем связь сразу: иначе первый упавший тест сообщил
                    // бы о нечитаемой ошибке вместо понятной "нет MongoDB".
                    client.GetDatabase("admin").RunCommand<BsonDocument>(
                        new BsonDocument("ping", 1));

                    // Имя с дефисом Mongo запрещает, поэтому только [_a-z0-9].
                    DatabaseName = $"order_test_{Guid.NewGuid():N}";
                    _database = client.GetDatabase(DatabaseName);
                    Available = true;
                }
                catch (MongoException)
                {
                    Available = false;
                }
                catch (TimeoutException)
                {
                    Available = false;
                }
            }
        }

        /// <summary>Доступна ли MongoDB. На false интеграционные тесты пропускаются.</summary>
        public bool Available { get; }

        public string DatabaseName { get; } = "order_test_unavailable";

        public IMongoDatabase Database =>
            _database ?? throw new InvalidOperationException("MongoDB is not available");

        /// <summary>
        /// Новая изолированная область под один тест. Коллекция заказов в ней
        /// своя, иначе тесты делили бы счётчики и падали бы не по своей вине.
        /// </summary>
        public OrderScope NewScope() => new OrderScope(Database);

        public void Dispose()
        {
            if (!Available)
            {
                return;
            }

            try
            {
                _database!.Client.DropDatabase(DatabaseName);
            }
            catch (MongoException) { }
        }
    }

    [CollectionDefinition("mongo")]
    public sealed class MongoCollection : ICollectionFixture<MongoFixture> { }
}