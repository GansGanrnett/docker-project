using MongoDB.Bson;
using MongoDB.Driver;
using OrderService.Models;
using System;
using System.Threading.Tasks;
using Xunit;

namespace OrderService.Tests
{
    /// <summary>
    /// Живая MongoDB на время тестов. Каждый прогон работает в базе с
    /// уникальным именем, поэтому тесты не мешают друг другу и не задевают
    /// данные разработчика.
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
                    // Проверяем связь сразу: иначе первый упавший тест сообщит
                    // о нечитаемой ошибке вместо понятной "нет MongoDB".
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

        public IMongoCollection<Order> Orders =>
            Database.GetCollection<Order>("orders");

        public IMongoDatabase Database =>
            _database ?? throw new InvalidOperationException("MongoDB is not available");

        /// <summary>Вставляет заказ и возвращает его id.</summary>
        public async Task<string> InsertOrderAsync(Order order)
        {
            await Orders.InsertOneAsync(order);
            return order.Id!;
        }

        public Task<Order?> FindAsync(string id) =>
            Orders.Find(o => o.Id == id).FirstOrDefaultAsync();

        /// <summary>
        /// Читает документ как сырой BSON. Нужен для проверки, что поле
        /// действительно отсутствует: типизированная модель скрыла бы это,
        /// вернув null в обоих случаях - и с полем, и без него.
        /// </summary>
        public Task<BsonDocument?> FindRawAsync(string id) =>
            Database
                .GetCollection<BsonDocument>("orders")
                .Find(Builders<BsonDocument>.Filter.Eq("_id", new ObjectId(id)))
                .FirstOrDefaultAsync();

        /// <summary>
        /// Убирает поле StatusChangedAt из документа, минуя типизированную
        /// модель: присвоение null оставило бы поле со значением null, а
        /// миграция ищет именно отсутствие поля.
        /// </summary>
        public Task UnsetStatusChangedAtAsync(string id) =>
            Database.GetCollection<BsonDocument>("orders").UpdateOneAsync(
                Builders<BsonDocument>.Filter.Eq("_id", new ObjectId(id)),
                Builders<BsonDocument>.Update.Unset("StatusChangedAt"));

        /// <summary>Ставит произвольный статус в обход модели.</summary>
        public Task SetStatusRawAsync(string id, string status) =>
            Database.GetCollection<BsonDocument>("orders").UpdateOneAsync(
                Builders<BsonDocument>.Filter.Eq("_id", new ObjectId(id)),
                Builders<BsonDocument>.Update.Set("Status", status));

        public void Dispose()
        {
            if (!Available)
            {
                return;
            }

            try
            {
                // База тестовая и одноразовая: падение при удалении не повод
                // провалить тест, который уже прошёл.
                _database!.Client.DropDatabase(DatabaseName);
            }
            catch (MongoException) { }
        }
    }

    [CollectionDefinition("mongo")]
    public sealed class MongoCollection : ICollectionFixture<MongoFixture> { }
}