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
        /// Сколько записей мы отправили на заполнение даты. Может быть больше
        /// Updated: если между чтением и записью заказ сменил статус, его
        /// апдейт не применится, и updated это покажет, а dated - нет.
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
        private readonly TextWriter _output;

        public OrderStatusMigration(IMongoCollection<Order> orders, TextWriter? output = null)
        {
            _orders = orders;
            _output = output ?? Console.Out;
        }

        /// <summary>
        /// Пишет батч и возвращает, сколько записей изменено.
        ///
        /// IsOrdered = false: один сбойный документ не должен останавливать
        /// всю миграцию - остальные батчи всё равно корректны.
        /// </summary>
        private async Task<long> WriteBatchAsync(
            IReadOnlyCollection<WriteModel<Order>> models,
            CancellationToken cancellationToken)
        {
            try
            {
                var result = await _orders.BulkWriteAsync(
                    models,
                    new BulkWriteOptions { IsOrdered = false },
                    cancellationToken: cancellationToken);

                return result.ModifiedCount;
            }
            catch (MongoBulkWriteException<Order> ex)
            {
                // Ошибки не глотаем: тихий частичный прогон хуже явного
                // падения - иначе миграция отчитается об успехе, а часть
                // заказов останется в legacy-статусе навсегда.
                var errors = ex.WriteErrors?.ToList() ?? new List<BulkWriteError>();

                _output.WriteLine(
                    $"  ERROR: {errors.Count} of {models.Count} updates failed in a batch.");

                foreach (var error in errors)
                {
                    _output.WriteLine($"    index {error.Index}: code {error.Code} {error.Message}");
                }

                throw;
            }
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

            // Стратегия "прочитать, затем типизированно записать".
            //
            // Три предыдущие попытки наехали на одну и ту же ловушку:
            // server-side reference в $set. $ifNull в обычном $set
            // сохранялся как литерал {"$ifNull": [...]}, строка
            // "$CreatedAt" - как строка "$CreatedAt" (сервер разворачивает
            // поле-ссылку только в aggregation pipeline, а не в обычном
            // $set). Обе попытки выглядели как успешная запись, пока
            // тест не пытался прочитать поле обратно.
            //
            // Поэтому здесь нет ни одного выражения на стороне сервера:
            // CreatedAt читается клиентом и пишется константой того же
            // типа DateTime, который уже лежит в базе.
            //
            // Читаем полные документы, без проекции. Проекция тут -
            // единственное, что принесло проблем: на класс без BSON-атрибутов
            // драйвер отдаёт дефолты молча, а BsonDocument в проекции в
            // драйвере 2.23 вообще не переводится в пайплайн и роняет
            // ExpressionToPipelineStageTranslator. Лишние поля в памяти -
            // это десятки мегабайт на миграцию, не повод писать код,
            // который нельзя предсказать.
            var legacy = await _orders
                .Find(legacyFilter)
                .ToListAsync(cancellationToken);

            long updated = 0;
            long dated = 0;

            // Пачками по 500: миграция на большой базе иначе делает
            // миллион round-trip'ов. 500 - размер батча MongoDB.
            foreach (var batch in legacy.Where(o => o.Id != null).Chunk(500))
            {
                var models = new List<WriteModel<Order>>(batch.Length);

                foreach (var order in batch)
                {
                    // Фильтр включает и текущий статус: между чтением и
                    // записью заказ мог сменить кто-то ещё, и переписывать
                    // дату уже не нашему заказу нельзя.
                    var filter = Builders<Order>.Filter.And(
                        Builders<Order>.Filter.Eq(o => o.Id, order.Id),
                        Builders<Order>.Filter.Eq(o => o.Status, OrderStatuses.LegacyPendingPayment));

                    var update = Builders<Order>.Update
                        .Set(o => o.Status, OrderStatuses.PaymentPending);

                    if (order.StatusChangedAt == null)
                    {
                        // Дату ставим только когда её нет: перетирать
                        // существующую значило бы откатить историю заказа
                        // назад.
                        update = update.Set(o => o.StatusChangedAt, order.CreatedAt.ToUniversalTime());
                        dated++;
                    }

                    models.Add(new UpdateOneModel<Order>(filter, update));
                }

                updated += await WriteBatchAsync(models, cancellationToken);
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
