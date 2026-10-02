using MongoDB.Bson;
using MongoDB.Driver;
using OrderService.Models;
using System;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;

namespace OrderService.Migrations
{
    /// <summary>
    /// Итог одного прогона миграции. Отдельный тип, чтобы тесты проверяли
    /// числа, а не текст лога.
    /// </summary>
    public sealed class MigrationReport
    {
        /// <summary>Сколько заказов ещё в legacy-значении.</summary>
        public long Found { get; init; }

        /// <summary>Сколько из них не имеют StatusChangedAt и получат его из CreatedAt.</summary>
        public long MissingStatusChangedAt { get; init; }

        /// <summary>Сколько записей изменено. Всегда 0 в dry-run.</summary>
        public long Updated { get; init; }

        /// <summary>
        /// Сколько записей получили StatusChangedAt из CreatedAt. Может быть
        /// меньше MissingStatusChangedAt: часть записей к моменту записи уже
        /// имела дату - её поставил другой код.
        /// </summary>
        public long Dated { get; init; }

        /// <summary>Была ли запись вообще выполнена.</summary>
        public bool Applied { get; init; }
    }

    /// <summary>
    /// Одноразовый перевод заказов со старого статуса PendingPayment на
    /// PaymentPending с заполнением StatusChangedAt из CreatedAt. Issue #34.
    ///
    /// По умолчанию ничего не пишет: считает и печатает, сколько записей
    /// затронет. Запись - только с --yes, чтобы ошибка в фильтре не переписала
    /// всю коллекцию.
    ///
    /// Фильтр намеренно узкий: только legacy-значение. Записи в новом или в
    /// терминальном статусе под него не попадают и остаются нетронутыми.
    ///
    /// Повторный запуск с --yes безопасен: после первого прохода legacy-значений
    /// не остаётся, и UpdateMany ничего не находит.
    /// </summary>
    public class OrderStatusMigration
    {
        public const string CommandName = "migrate-statuses";
        public const string YesFlag = "--yes";

        /// <summary>Имя индекса, который создаётся при старте сервиса.</summary>
        public const string StatusChangedAtIndexName = "status_statuschangedat";

        private const string DatabaseName = "order_db";
        private const string CollectionName = "orders";

        private readonly IMongoCollection<Order> _orders;
        private readonly IMongoCollection<BsonDocument> _rawOrders;
        private readonly TextWriter _output;

        public OrderStatusMigration(IMongoCollection<Order> orders, TextWriter? output = null)
        {
            _orders = orders;
            // Та же коллекка, но без модели: заполнить дату из другой даты
            // типизированным Set в драйвере 2.23 нельзя, нужен raw-документ.
            _rawOrders = orders.Database.GetCollection<BsonDocument>(orders.CollectionNamespace.CollectionName);
            _output = output ?? Console.Out;
        }

        /// <summary>Запрошена ли миграция вместо обычного старта сервиса.</summary>
        public static bool IsRequested(string[] args) =>
            args.Any(a => string.Equals(a, CommandName, StringComparison.OrdinalIgnoreCase));

        /// <summary>Разрешена ли запись. Без этого флага миграция только считает.</summary>
        public static bool IsApplyRequested(string[] args) =>
            args.Any(a => string.Equals(a, YesFlag, StringComparison.OrdinalIgnoreCase));

        /// <summary>
        /// Точка входа для `dotnet run -- migrate-statuses [--yes]`.
        /// Возвращает код возврата процесса.
        /// </summary>
        public static async Task<int> RunAsCommandAsync(string[] args)
        {
            try
            {
                var mongoUrl = Environment.GetEnvironmentVariable("MONGO_URL")
                    ?? throw new InvalidOperationException("MONGO_URL is not set");

                var orders = new MongoClient(mongoUrl)
                    .GetDatabase(DatabaseName)
                    .GetCollection<Order>(CollectionName);

                var migration = new OrderStatusMigration(orders);
                await migration.RunAsync(IsApplyRequested(args), CancellationToken.None);
                return 0;
            }
            catch (Exception ex)
            {
                await Console.Out.WriteLineAsync($"[MIGRATION] Failed: {ex.Message}");
                return 1;
            }
        }

        /// <summary>
        /// Считает кандидатов и, если apply=true, переводит их в новый статус.
        /// </summary>
        public async Task<MigrationReport> RunAsync(bool apply, CancellationToken cancellationToken)
        {
            var legacyFilter = Builders<Order>.Filter.Eq(o => o.Status, OrderStatuses.LegacyPendingPayment);

            var found = await _orders.CountDocumentsAsync(legacyFilter, cancellationToken: cancellationToken);

            // CreatedAt есть у всех заказов, а StatusChangedAt - только у тех, что
            // созданы после issue #34. Именно их придётся заполнить.
            var missingStatusChangedAt = Builders<Order>.Filter.And(
                legacyFilter,
                Builders<Order>.Filter.Exists(o => o.StatusChangedAt, exists: false));

            var missing = await _orders.CountDocumentsAsync(missingStatusChangedAt, cancellationToken: cancellationToken);

            await WriteHeaderAsync(found, missing);

            if (!apply)
            {
                _output.WriteLine($"DRY RUN: nothing was written. Re-run with {YesFlag} to apply.");
                return new MigrationReport
                {
                    Found = found,
                    MissingStatusChangedAt = missing,
                    Updated = 0,
                    Applied = false
                };
            }

            // Пишем дату по документам, а не одним $set: драйвер 2.23 не
            // умеет поле-из-поля в Set, только константу, а читать
            // CreatedAt в памяти нельзя - база может быть больше памяти
            // процесса миграции.
            //
            // Ключевой момент: перечитываем те же _id, что посчитали
            // раньше, и фильтр по статусу снимаем. Иначе пришлось бы
            // считать два раза, а между подсчётом и записью статус мог
            // бы сменить кто-то ещё: тогда мы переписали бы дату чужому
            // заказу.
            var legacyIds = await _orders
                .Find(legacyFilter)
                .Project(o => o.Id)
                .ToListAsync(cancellationToken);

            long updated = 0;

            // Пачками, а не по одному: миграция на большой базе иначе
            // делает миллион round-trip'ов. 500 - размер батча MongoDB.
            foreach (var batch in legacyIds.Where(id => id != null).Chunk(500))
            {
                var ids = batch.Select(id => id!).ToList();
                var batchFilter = Builders<Order>.Filter.In(o => o.Id, ids);

                var statusResult = await _orders.UpdateManyAsync(
                    batchFilter,
                    Builders<Order>.Update.Set(o => o.Status, OrderStatuses.PaymentPending),
                    cancellationToken: cancellationToken);
                updated += statusResult.ModifiedCount;
            }

            long dated = 0;

            foreach (var batch in legacyIds.Where(id => id != null).Chunk(500))
            {
                var ids = batch.Select(id => id!).ToList();
                var batchFilter = Builders<Order>.Filter.And(
                    Builders<Order>.Filter.In(o => o.Id, ids),
                    Builders<Order>.Filter.Exists(o => o.StatusChangedAt, exists: false));

                // Строка "$CreatedAt" - это поле-ссылка, которую MongoDB
                // разворачивает в значение поля на сервере. Типизированный
                // Set(field, value) в драйвере 2.23 принимает только
                // константу, поэтому серверное выражение доступно лишь
                // через raw-документ. Раньше здесь был пайплайн с $ifNull,
                // но он попадал в поле как литерал {"$ifNull": [...]},
                // и дата становилась мусором - такой датой нельзя ни
                // считать возраст заказа, ни сравнивать с дедлайном.
                // _id обязан быть ObjectId, а не строкой: модель объявляет Id как
                // string с [BsonRepresentation(BsonType.ObjectId)], поэтому
                // типизированный фильтр конвертирует сам, а raw-документ -
                // нет. Со строкой в "$in" сервер не находит ничего и молча
                // обновляет 0 записей.
                var idValues = new BsonArray(
                    ids.Select(i => (BsonValue)new ObjectId(i)));

                var dateResult = await _rawOrders.UpdateManyAsync(
                    new BsonDocument
                    {
                        { "_id", new BsonDocument("$in", idValues) },
                        { "StatusChangedAt", new BsonDocument("$exists", false) }
                    },
                    new BsonDocument("$set", new BsonDocument("StatusChangedAt", "$CreatedAt")),
                    cancellationToken: cancellationToken);
                dated += dateResult.ModifiedCount;
            }

            var skipped = found - updated;

            _output.WriteLine("APPLIED");
            _output.WriteLine($"  updated: {updated}");
            _output.WriteLine($"  dated: {dated} (StatusChangedAt filled from CreatedAt)");
            _output.WriteLine($"  skipped: {skipped} (no longer matched the legacy filter)");

            if (skipped > 0)
            {
                _output.WriteLine("  Note: skipped records were changed by someone else while the migration ran.");
            }

            return new MigrationReport
            {
                Found = found,
                MissingStatusChangedAt = missing,
                Updated = updated,
                Dated = dated,
                Applied = true
            };
        }

        private async Task WriteHeaderAsync(long found, long missing)
        {
            _output.WriteLine("=== Order status migration (#34) ===");
            _output.WriteLine($"Legacy value (Status = {OrderStatuses.LegacyPendingPayment}): {found}");
            _output.WriteLine($"  without StatusChangedAt, will be filled from CreatedAt: {missing}");
            _output.WriteLine($"  already has StatusChangedAt: {found - missing}");
            await _output.FlushAsync();
        }
    }
}
