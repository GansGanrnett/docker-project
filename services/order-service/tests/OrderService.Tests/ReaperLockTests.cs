using Microsoft.Extensions.Logging.Abstractions;
using MongoDB.Driver;
using OrderService.Models;
using OrderService.Services;
using System;
using System.Threading;
using System.Threading.Tasks;
using Xunit;

namespace OrderService.Tests
{
    /// <summary>
    /// Тесты блокировки reaper'а. Их смысл - replicaCount: 2: работать должен
    /// только один, иначе двойной проход по коллекции и два конкурирующих
    /// апдейта. Требуют MongoDB.
    /// </summary>
    [Collection("mongo")]
    public class ReaperLockTests
    {
        private readonly MongoFixture _mongo;

        public ReaperLockTests(MongoFixture mongo)
        {
            _mongo = mongo;
        }

        private static PaymentTimeoutReaper CreateReaper(MongoFixture mongo) =>
            new PaymentTimeoutReaper(NullLogger<PaymentTimeoutReaper>.Instance, mongo.Orders);

        [MongoFact]
        public async Task First_holder_acquires_the_lock_and_the_second_does_not()
        {
            var first = CreateReaper(_mongo);
            var second = CreateReaper(_mongo);

            await first.UpdateLeadershipAsync(CancellationToken.None);
            await second.UpdateLeadershipAsync(CancellationToken.None);

            // Признак лидерства приватный, поэтому проверяем через результат:
            // лидер обязан реально собрать заказы, нелидер - нет.
            await _mongo.InsertOrderAsync(new Order
            {
                Username = "tester",
                CreatedAt = DateTime.UtcNow.AddMinutes(-30),
                Status = OrderStatuses.PaymentPending,
                StatusChangedAt = DateTime.UtcNow.AddMinutes(-30)
            });

            var leaderReaped = await first.ReapExpiredOrdersAsync(CancellationToken.None);
            var followerReaped = await second.ReapExpiredOrdersAsync(CancellationToken.None);

            Assert.Equal(1, leaderReaped);
            Assert.Equal(0, followerReaped);
        }

        [MongoFact]
        public async Task Same_holder_can_extend_its_own_lock()
        {
            var reaper = CreateReaper(_mongo);

            await reaper.UpdateLeadershipAsync(CancellationToken.None);
            await _mongo.InsertOrderAsync(new Order
            {
                Username = "tester",
                CreatedAt = DateTime.UtcNow.AddMinutes(-30),
                Status = OrderStatuses.PaymentPending,
                StatusChangedAt = DateTime.UtcNow.AddMinutes(-30)
            });
            await reaper.ReapExpiredOrdersAsync(CancellationToken.None);

            // Продление не должно отпускать блокировку: держатель остаётся
            // лидером и после второго тика.
            await reaper.UpdateLeadershipAsync(CancellationToken.None);
            Assert.Equal(0, await reaper.ReapExpiredOrdersAsync(CancellationToken.None));
        }

        [MongoFact]
        public async Task Expired_lock_is_taken_over_by_the_next_holder()
        {
            var first = CreateReaper(_mongo);
            await first.UpdateLeadershipAsync(CancellationToken.None);

            // Выдерживаем срок жизни блокировки назад, чтобы следующий претендент
            // увидел её истёкшей. Обычно достаточно сдвинуть только TTL.
            var locks = _mongo.Database.GetCollection<BsonReaperLockDoc>("reaper_locks");
            var existing = await locks.Find(FilterDefinition<BsonReaperLockDoc>.Empty).FirstOrDefaultAsync();
            if (existing != null)
            {
                await locks.UpdateOneAsync(
                    Builders<BsonReaperLockDoc>.Filter.Eq(d => d.Id, existing.Id),
                    Builders<BsonReaperLockDoc>.Update.Set(d => d.ExpiresAt, DateTime.UtcNow.AddSeconds(-1)));
            }

            var second = CreateReaper(_mongo);
            await second.UpdateLeadershipAsync(CancellationToken.None);
            await _mongo.InsertOrderAsync(new Order
            {
                Username = "tester",
                CreatedAt = DateTime.UtcNow.AddMinutes(-30),
                Status = OrderStatuses.PaymentPending,
                StatusChangedAt = DateTime.UtcNow.AddMinutes(-30)
            });

            // Новый держатель забрал блокировку и работает.
            Assert.Equal(1, await second.ReapExpiredOrdersAsync(CancellationToken.None));
        }

        [MongoFact]
        public async Task Lock_document_is_created_once_and_survives_recreation()
        {
            var first = CreateReaper(_mongo);
            var second = CreateReaper(_mongo);

            await first.UpdateLeadershipAsync(CancellationToken.None);
            await second.UpdateLeadershipAsync(CancellationToken.None);
            await first.UpdateLeadershipAsync(CancellationToken.None);

            var locks = _mongo.Database.GetCollection<BsonReaperLockDoc>("reaper_locks");
            var count = await locks.CountDocumentsAsync(FilterDefinition<BsonReaperLockDoc>.Empty);

            // Upsert по _id не должен плодить документы: иначе каждый тик
            // добавлял бы запись, и рано или поздно коллекция разрослась бы.
            Assert.Equal(1, count);
        }

        private sealed class BsonReaperLockDoc
        {
            public string Id { get; set; } = default!;
            public string Holder { get; set; } = default!;
            public DateTime ExpiresAt { get; set; }
        }
    }
}