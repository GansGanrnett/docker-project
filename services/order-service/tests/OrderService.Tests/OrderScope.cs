using MongoDB.Bson;
using MongoDB.Driver;
using OrderService.Models;
using System;
using System.Threading.Tasks;

namespace OrderService.Tests
{
    /// <summary>
    /// Изолированная область работы одного теста: своя коллекция заказов в
    /// общей базе.
    ///
    /// Именно коллекция, а не база, потому что xUnit создаёт новый экземпляр
    /// класса теста на каждый метод, но переиспользует одну базу на всю
    /// коллекцию. Общая база означала бы общие счётчики: тест, считающий
    /// "сколько заказов осталось в PaymentPending", видел бы заказы соседних
    /// тестов и падал бы не по своей вине.
    /// </summary>
    public sealed class OrderScope
    {
        private readonly IMongoDatabase _database;
        private readonly string _collectionName;

        public OrderScope(IMongoDatabase database)
        {
            _database = database;
            _collectionName = $"orders_{Guid.NewGuid():N}";
        }

        public IMongoCollection<Order> Orders => _database.GetCollection<Order>(_collectionName);

        /// <summary>
        /// Та же база нужна для коллекции блокировок reaper'а: он работает
        /// по orders.Database, поэтому и здесь берётся та же база.
        /// </summary>
        public IMongoDatabase Database => _database;

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
        /// вернув null в обоих случаях - и с полем, и без него. И чтобы
        /// поймать мусор в поле: десериализация дала бы исключение с
        /// невнятным текстом, а здесь видно, что именно лежит в документе.
        /// </summary>
        public Task<BsonDocument?> FindRawAsync(string id) =>
            Raw.Find(Builders<BsonDocument>.Filter.Eq("_id", new ObjectId(id)))
                .FirstOrDefaultAsync();

        /// <summary>Коллекка заказов как сырой BSON, для точечных правок.</summary>
        public IMongoCollection<BsonDocument> Raw =>
            _database.GetCollection<BsonDocument>(_collectionName);

        /// <summary>
        /// Убирает поле StatusChangedAt из документа, минуя типизированную
        /// модель: присвоение null оставило бы поле со значением null, а
        /// миграция ищет именно отсутствие поля.
        /// </summary>
        public Task UnsetStatusChangedAtAsync(string id) =>
            Raw.UpdateOneAsync(
                Builders<BsonDocument>.Filter.Eq("_id", new ObjectId(id)),
                Builders<BsonDocument>.Update.Unset("StatusChangedAt"));

        /// <summary>Ставит произвольный статус в обход модели.</summary>
        public Task SetStatusRawAsync(string id, string status) =>
            Raw.UpdateOneAsync(
                Builders<BsonDocument>.Filter.Eq("_id", new ObjectId(id)),
                Builders<BsonDocument>.Update.Set("Status", status));

        /// <summary>Ставит произвольную дату последнего перехода в обход модели.</summary>
        public Task SetStatusChangedAtRawAsync(string id, DateTime value) =>
            Raw.UpdateOneAsync(
                Builders<BsonDocument>.Filter.Eq("_id", new ObjectId(id)),
                Builders<BsonDocument>.Update.Set("StatusChangedAt", value));

        public Task<long> CountPendingAsync() =>
            Orders.CountDocumentsAsync(
                Builders<Order>.Filter.Eq(o => o.Status, Models.OrderStatuses.PaymentPending));

        public Task<long> CountStatusAsync(string status) =>
            Orders.CountDocumentsAsync(Builders<Order>.Filter.Eq(o => o.Status, status));

        public async Task<IReadOnlyList<string>> StatusesAsync()
        {
            var orders = await Orders.Find(Builders<Order>.Filter.Empty).ToListAsync();
            var statuses = new string[orders.Count];
            for (var i = 0; i < orders.Count; i++)
            {
                statuses[i] = orders[i].Status;
            }

            return statuses;
        }

        /// <summary>Сколько заказов в этом наборе.</summary>
        public Task<long> CountAsync() =>
            Orders.CountDocumentsAsync(Builders<Order>.Filter.Empty);

        public void Dispose()
        {
            try
            {
                _database.DropCollection(_collectionName);
            }
            catch (MongoException)
            {
                // База тестовая и одноразовая: падение при удалении не повод
                // провалить тест, который уже прошёл.
            }
        }
    }
}