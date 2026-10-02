using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using MongoDB.Bson;
using MongoDB.Bson.Serialization.Attributes;
using MongoDB.Driver;
using OrderService.Models;
using System;
using System.Threading;
using System.Threading.Tasks;

namespace OrderService.Services
{
    /// <summary>
    /// Документ блокировки reaper'а. _id - строковый идентификатор роли,
    /// а не ObjectId, поэтому здесь намеренно нет BsonRepresentation.
    /// </summary>
    public class ReaperLock
    {
        [BsonId]
        public string Id { get; set; } = default!;

        [BsonElement("holder")]
        public string Holder { get; set; } = default!;

        [BsonElement("expiresAt")]
        public DateTime ExpiresAt { get; set; }
    }

    /// <summary>
    /// Переводит заказы, по которым платёжный результат не пришёл за отведённое
    /// время, в терминальный PaymentTimeout. Issue #34.
    ///
    /// Переход выполняется одним условным UpdateMany: условие Status In Pending
    /// и запись нового значения - одна атомарная операция. Фазы «нашёл, потом
    /// обновил» нет, поэтому падение между ними невозможно, повторный прогон
    /// по тем же заказам ничего не меняет, а уже оплаченный заказ не может быть
    /// перебит в PaymentTimeout.
    ///
    /// В chart выставлено replicaCount: 2, значит reaper'ов будет два. Данные
    /// защищает условный апдейт, но сканировать коллекцию дважды незачем, поэтому
    /// работу выполняет только держатель блокировки.
    /// </summary>
    public class PaymentTimeoutReaper : BackgroundService
    {
        private const string LockId = "payment-timeout-reaper";
        private const string LockCollectionName = "reaper_locks";

        /// <summary>Как часто проверять статус платежа.</summary>
        private const int ReapIntervalSeconds = 30;

        /// <summary>
        /// Срок жизни блокировки. Больше интервала перебора, чтобы под не успел
        /// упасть и потерять lease между двумя тиками reaper'а.
        /// </summary>
        private const int LockTtlSeconds = 30;

        /// <summary>Как часто продлевать или перехватывать блокировку.</summary>
        private const int LockRenewSeconds = 10;

        private const int PaymentTimeoutMinutes = 15;

        private readonly ILogger<PaymentTimeoutReaper> _logger;
        private readonly IMongoCollection<Order> _orders;
        private readonly IMongoCollection<ReaperLock> _locks;

        /// <summary>Идентификатор процесса, который держит блокировку.</summary>
        private readonly string _holder = Guid.NewGuid().ToString("N");

        private bool _isLeader;

        public PaymentTimeoutReaper(ILogger<PaymentTimeoutReaper> logger, IMongoCollection<Order> orders)
        {
            _logger = logger;
            _orders = orders;
            _locks = orders.Database.GetCollection<ReaperLock>(LockCollectionName);
        }

        protected override async Task ExecuteAsync(CancellationToken stoppingToken)
        {
            try
            {
                await EnsureLockCollectionAsync(stoppingToken);
            }
            catch (Exception ex)
            {
                // Без коллекции блокировок работать нельзя: при replicaCount > 1
                // это означало бы reaper на каждой реплике. Лучше не стартовать.
                _logger.LogError(ex, "[REAPER] Cannot prepare lock collection {Collection}, reaper will not start.", LockCollectionName);
                throw;
            }

            var nextRenewAt = DateTime.MinValue;
            var nextReapAt = DateTime.MinValue;

            while (!stoppingToken.IsCancellationRequested)
            {
                try
                {
                    if (DateTime.UtcNow >= nextRenewAt)
                    {
                        nextRenewAt = DateTime.UtcNow.AddSeconds(LockRenewSeconds);
                        // Одна и та же операция и захватывает блокировку, и
                        // продлевает её, если она уже наша.
                        await UpdateLeadershipAsync(stoppingToken);
                    }

                    if (_isLeader && DateTime.UtcNow >= nextReapAt)
                    {
                        nextReapAt = DateTime.UtcNow.AddSeconds(ReapIntervalSeconds);
                        await ReapExpiredOrdersAsync(stoppingToken);
                    }
                }
                catch (Exception ex) when (ex is not OperationCanceledException)
                {
                    _logger.LogError(ex, "[REAPER] Tick failed, retrying in {Seconds}s.", ReapIntervalSeconds);
                }

                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(1), stoppingToken);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
            }

            _logger.LogInformation("[REAPER] Stopped. Holder {Holder} was leader: {IsLeader}.", _holder, _isLeader);
        }

        /// <summary>
        /// Захватывает или продлевает блокировку и обновляет признак лидерства,
        /// логируя смену в обоих направлениях.
        /// </summary>
        internal async Task UpdateLeadershipAsync(CancellationToken cancellationToken)
        {
            var now = DateTime.UtcNow;
            var filter = Builders<ReaperLock>.Filter.And(
                Builders<ReaperLock>.Filter.Eq(l => l.Id, LockId),
                Builders<ReaperLock>.Filter.Or(
                    Builders<ReaperLock>.Filter.Lt(l => l.ExpiresAt, now),
                    Builders<ReaperLock>.Filter.Eq(l => l.Holder, _holder)));

            var update = Builders<ReaperLock>.Update
                .Set(l => l.Holder, _holder)
                .Set(l => l.ExpiresAt, now.AddSeconds(LockTtlSeconds));

            var options = new FindOneAndUpdateOptions<ReaperLock>
            {
                IsUpsert = true,
                ReturnDocument = ReturnDocument.After
            };

            ReaperLock? result;
            try
            {
                result = await _locks.FindOneAndUpdateAsync(filter, update, options, cancellationToken);
            }
            catch (MongoCommandException ex) when (IsDuplicateKey(ex))
            {
                // Документ есть, но держит его другой не истёкший holder:
                // upsert пытается вставить и упирается в _id. Это не ошибка -
                // это отказ в захвате.
                result = null;
            }
            catch (MongoWriteException ex) when (ex.WriteError?.Category == ServerErrorCategory.DuplicateKey)
            {
                result = null;
            }

            var isLeader = result?.Holder == _holder;

            if (isLeader != _isLeader)
            {
                if (isLeader)
                {
                    _logger.LogInformation("[REAPER] Acquired lock '{LockId}', holder {Holder}.", LockId, _holder);
                }
                else
                {
                    _logger.LogInformation("[REAPER] Lost lock '{LockId}' or never acquired it, holder {Holder}. Another replica reaps.", LockId, _holder);
                }
                _isLeader = isLeader;
            }
        }

        /// <summary>
        /// Один проход: заказы без результата платежа дольше отведённого времени
        /// переводятся в PaymentTimeout. Возвращает количество изменённых заказов.
        /// </summary>
        internal async Task<long> ReapExpiredOrdersAsync(CancellationToken cancellationToken)
        {
            var cutoff = DateTime.UtcNow.AddMinutes(-PaymentTimeoutMinutes);

            // StatusChangedAt < cutoff, а не CreatedAt: отсчёт идёт от последнего
            // перехода статуса, иначе повторная попытка оплаты не сдвигала бы
            // отложенный статус заново.
            var filter = Builders<Order>.Filter.And(
                Builders<Order>.Filter.In(o => o.Status, OrderStatuses.Pending),
                Builders<Order>.Filter.Lt(o => o.StatusChangedAt, cutoff));

            var update = Builders<Order>.Update
                .Set(o => o.Status, OrderStatuses.PaymentTimeout)
                .Set(o => o.StatusChangedAt, DateTime.UtcNow);

            var result = await _orders.UpdateManyAsync(filter, update, cancellationToken: cancellationToken);

            if (result.ModifiedCount > 0)
            {
                _logger.LogInformation("[REAPER] Moved {Count} order(s) to {Status}: no payment result within {Minutes} min.",
                    result.ModifiedCount, OrderStatuses.PaymentTimeout, PaymentTimeoutMinutes);
            }

            return result.ModifiedCount;
        }

        /// <summary>
        /// Создаёт коллекцию блокировок и TTL-индекс. TTL-индекс - только защита
        /// от залипших документов: он срабатывает раз в минуту и для
        /// синхронизации не используется, решение принимает findOneAndUpdate.
        /// </summary>
        private async Task EnsureLockCollectionAsync(CancellationToken cancellationToken)
        {
            var database = _locks.Database;

            var existing = await database.ListCollectionNamesAsync(
                new ListCollectionNamesOptions
                {
                    Filter = Builders<BsonDocument>.Filter.Eq("name", LockCollectionName)
                },
                cancellationToken);

            if (!await existing.AnyAsync(cancellationToken))
            {
                try
                {
                    await database.CreateCollectionAsync(LockCollectionName, cancellationToken: cancellationToken);
                }
                catch (MongoCommandException ex) when (ex.CodeName == "NamespaceExists")
                {
                    // Создал соседний под - это нормально.
                }
            }

            var index = new CreateIndexModel<ReaperLock>(
                Builders<ReaperLock>.IndexKeys.Ascending(l => l.ExpiresAt),
                new CreateIndexOptions { Name = "expiresAt_ttl", ExpireAfter = TimeSpan.Zero });

            await _locks.Indexes.CreateOneAsync(index, cancellationToken: cancellationToken);
        }

        /// <summary>
        /// Duplicate key приходит от сервера по-разному в зависимости от
        /// команды: обычный insert/update оборачивается в MongoWriteException,
        /// а findAndModify - в MongoCommandException, потому что для него
        /// сервер отвечает ошибкой команды. Ловить надо оба, иначе конкуренция
        /// за блокировку выглядела бы как падение reaper'а.
        /// </summary>
        private static bool IsDuplicateKey(MongoCommandException ex) =>
            ex.Code == 11000 || ex.CodeName == "DuplicateKey";
    }
}
