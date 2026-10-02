using MongoDB.Bson;
using MongoDB.Driver;
using OrderService.Migrations;
using OrderService.Models;
using System;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Xunit;

namespace OrderService.Tests
{
    /// <summary>
    /// Тесты разбора аргументов запуска миграции. База не нужна.
    /// </summary>
    public class OrderStatusMigrationArgumentTests
    {
        [Fact]
        public void Command_is_recognised_case_insensitively()
        {
            Assert.True(OrderStatusMigration.IsRequested(new[] { "migrate-statuses" }));
            Assert.True(OrderStatusMigration.IsRequested(new[] { "Migrate-Statuses" }));
            Assert.True(OrderStatusMigration.IsRequested(new[] { "--verbosity", "q", "migrate-statuses" }));
        }

        public void Empty_startup_arguments_are_not_mistaken_for_the_migration()
        {
            Assert.False(OrderStatusMigration.IsRequested(Array.Empty<string>()));
        }

        public void Ascii_argv_values_are_not_mistaken_for_the_migration()
        {
            Assert.False(OrderStatusMigration.IsRequested(new[] { "--urls", "http://localhost:5000" }));
        }

        public void Similar_but_different_command_is_not_the_migration()
        {
            // Префикс "migrate" - тоже не наш случай: команда должна совпадать
            // целиком, иначе опечатка в вызове тихо ушла бы в миграцию.
            Assert.False(OrderStatusMigration.IsRequested(new[] { "migrate" }));
            Assert.False(OrderStatusMigration.IsRequested(new[] { "migrate-statuses-please" }));
        }

        [Fact]
        public void Yes_flag_is_opt_in_only()
        {
            Assert.False(OrderStatusMigration.IsApplyRequested(new[] { "migrate-statuses" }));
            Assert.True(OrderStatusMigration.IsApplyRequested(new[] { "migrate-statuses", "--yes" }));
            Assert.True(OrderStatusMigration.IsApplyRequested(new[] { "--YES" }));
        }

        public void Bare_command_does_not_enable_writing()
        {
            Assert.False(OrderStatusMigration.IsApplyRequested(new[] { "migrate-statuses" }));
        }

        public void Almost_yes_flags_do_not_enable_writing()
        {
            Assert.False(OrderStatusMigration.IsApplyRequested(Array.Empty<string>()));
            Assert.False(OrderStatusMigration.IsApplyRequested(new[] { "-y" }));
            Assert.False(OrderStatusMigration.IsApplyRequested(new[] { "yes" }));
            Assert.False(OrderStatusMigration.IsApplyRequested(new[] { "YES" }));
        }

        [Fact]
        public void Index_name_is_stable()
        {
            // Имя индекса проверяется в тестах, поэтому оно часть контракта:
            // переименование без правки тестов и комментария в Program.cs
            // приведёт к созданию второго индекса при каждом старте.
            Assert.Equal("status_statuschangedat", OrderStatusMigration.StatusChangedAtIndexName);
        }
    }

    /// <summary>
    /// Тесты самой миграции: что она находит, что пишет и что не трогает.
    /// Требуют MongoDB.
    /// </summary>
    [Collection("mongo")]
    public class OrderStatusMigrationTests
    {
        private readonly MongoFixture _mongo;

        public OrderStatusMigrationTests(MongoFixture mongo)
        {
            _mongo = mongo;
        }

        private OrderStatusMigration CreateMigration(out StringWriter output)
        {
            output = new StringWriter();
            return new OrderStatusMigration(_mongo.Orders, output);
        }

        private async Task<string> InsertLegacyOrderAsync(DateTime createdAt)
        {
            var order = new Order
            {
                Username = "tester",
                CreatedAt = createdAt,
                Status = OrderStatuses.LegacyPendingPayment
            };
            var id = await _mongo.InsertOrderAsync(order);
            // Типизированная модель не умеет не записывать поле, убираем его
            // из сырого BSON - так документ выглядит ровно как в базе сейчас.
            await _mongo.UnsetStatusChangedAtAsync(id);
            return id;
        }

        private async Task<string> InsertPendingOrderAsync(DateTime createdAt)
        {
            var id = await _mongo.InsertOrderAsync(new Order
            {
                Username = "tester",
                CreatedAt = createdAt,
                Status = OrderStatuses.PaymentPending,
                StatusChangedAt = createdAt
            });
            return id;
        }

        [MongoFact]
        public async Task Dry_run_counts_legacy_records_and_writes_nothing()
        {
            var id = await InsertLegacyOrderAsync(DateTime.UtcNow.AddDays(-1));

            var report = await CreateMigration(out var output).RunAsync(apply: false, CancellationToken.None);

            Assert.Equal(1, report.Found);
            Assert.Equal(1, report.MissingStatusChangedAt);
            Assert.Equal(0, report.Updated);
            Assert.False(report.Applied);
            Assert.Contains("DRY RUN", output.ToString());

            var raw = await _mongo.FindRawAsync(id);
            Assert.Equal(OrderStatuses.LegacyPendingPayment, raw!["Status"].AsString);
            Assert.False(raw.Contains("StatusChangedAt"));
        }

        [MongoFact]
        public async Task Apply_renames_status_and_fills_StatusChangedAt_from_CreatedAt()
        {
            var createdAt = DateTime.UtcNow.AddDays(-1);
            var id = await InsertLegacyOrderAsync(createdAt);

            var report = await CreateMigration(out _).RunAsync(apply: true, CancellationToken.None);

            Assert.Equal(1, report.Found);
            Assert.Equal(1, report.Updated);
            Assert.True(report.Applied);

            var stored = await _mongo.FindAsync(id);
            Assert.Equal(OrderStatuses.PaymentPending, stored!.Status);
            Assert.NotNull(stored.StatusChangedAt);
            Assert.True(stored.StatusChangedAt!.Value > DateTime.UtcNow.AddDays(-2));
        }

        [MongoFact]
        public async Task Second_apply_finds_nothing_and_changes_nothing()
        {
            await InsertLegacyOrderAsync(DateTime.UtcNow.AddDays(-1));

            await CreateMigration(out _).RunAsync(apply: true, CancellationToken.None);
            var second = await CreateMigration(out _).RunAsync(apply: true, CancellationToken.None);

            Assert.Equal(0, second.Found);
            Assert.Equal(0, second.Updated);
        }

        [MongoFact]
        public async Task Already_pending_orders_are_not_counted_and_not_touched()
        {
            var createdAt = DateTime.UtcNow.AddDays(-1);
            var id = await InsertPendingOrderAsync(createdAt);
            await _mongo.UnsetStatusChangedAtAsync(id);

            var report = await CreateMigration(out _).RunAsync(apply: true, CancellationToken.None);

            Assert.Equal(0, report.Found);
            Assert.Equal(0, report.Updated);
            // Дата остаётся незаполненной: миграция чинит только legacy.
            var raw = await _mongo.FindRawAsync(id);
            Assert.False(raw!.Contains("StatusChangedAt"));
        }

        [MongoFact]
        public async Task Terminal_orders_are_left_untouched()
        {
            var id = await _mongo.InsertOrderAsync(new Order
            {
                Username = "tester",
                CreatedAt = DateTime.UtcNow.AddDays(-1),
                Status = OrderStatuses.Paid,
                StatusChangedAt = DateTime.UtcNow.AddDays(-1)
            });

            await CreateMigration(out _).RunAsync(apply: true, CancellationToken.None);

            var stored = await _mongo.FindAsync(id);
            Assert.Equal(OrderStatuses.Paid, stored!.Status);
        }

        [MongoFact]
        public async Task Existing_StatusChangedAt_is_kept_not_overwritten()
        {
            // В базе возможен заказ с legacy-статусом, но уже с датой - если его
            // успел проставить какой-то другой код. Перетирать дату было бы
            // откатом истории заказа назад.
            var id = await _mongo.InsertOrderAsync(new Order
            {
                Username = "tester",
                CreatedAt = DateTime.UtcNow.AddDays(-10),
                Status = OrderStatuses.LegacyPendingPayment,
                StatusChangedAt = DateTime.UtcNow.AddMinutes(-5)
            });

            var report = await CreateMigration(out _).RunAsync(apply: true, CancellationToken.None);

            Assert.Equal(1, report.Found);
            Assert.Equal(0, report.MissingStatusChangedAt);

            var stored = await _mongo.FindAsync(id);
            Assert.Equal(OrderStatuses.PaymentPending, stored!.Status);
            Assert.True(stored.StatusChangedAt!.Value > DateTime.UtcNow.AddMinutes(-10));
        }

        [MongoFact]
        public async Task Mixed_collection_is_partially_migrated()
        {
            var legacyA = await InsertLegacyOrderAsync(DateTime.UtcNow.AddDays(-1));
            var legacyB = await InsertLegacyOrderAsync(DateTime.UtcNow.AddDays(-2));
            var modern = await InsertPendingOrderAsync(DateTime.UtcNow);
            var paid = await _mongo.InsertOrderAsync(new Order
            {
                Username = "tester",
                CreatedAt = DateTime.UtcNow.AddDays(-3),
                Status = OrderStatuses.Paid,
                StatusChangedAt = DateTime.UtcNow.AddDays(-3)
            });

            var report = await CreateMigration(out _).RunAsync(apply: true, CancellationToken.None);

            Assert.Equal(2, report.Found);
            Assert.Equal(2, report.Updated);

            Assert.Equal(OrderStatuses.PaymentPending, (await _mongo.FindAsync(legacyA))!.Status);
            Assert.Equal(OrderStatuses.PaymentPending, (await _mongo.FindAsync(legacyB))!.Status);
            Assert.Equal(OrderStatuses.PaymentPending, (await _mongo.FindAsync(modern))!.Status);
            Assert.Equal(OrderStatuses.Paid, (await _mongo.FindAsync(paid))!.Status);
        }

        [MongoFact]
        public async Task Report_output_lists_updated_and_skipped_counts()
        {
            await InsertLegacyOrderAsync(DateTime.UtcNow.AddDays(-1));

            var output = new StringWriter();
            var migration = new OrderStatusMigration(_mongo.Orders, output);
            await migration.RunAsync(apply: true, CancellationToken.None);

            var text = output.ToString();
            Assert.Contains("APPLIED", text);
            Assert.Contains("updated: 1", text);
            Assert.Contains("skipped: 0", text);
        }

        [MongoFact]
        public async Task Migrated_order_is_reaped_by_the_timeout_reaper_as_before()
        {
            // Смысл миграции именно в этом: переведённый заказ обязан снова
            // попадать под reaper, иначе он навсегда остался бы висеть.
            var createdAt = DateTime.UtcNow.AddMinutes(-30);
            var id = await InsertLegacyOrderAsync(createdAt);

            await CreateMigration(out _).RunAsync(apply: true, CancellationToken.None);

            var reaper = new Services.PaymentTimeoutReaper(
                Microsoft.Extensions.Logging.Abstractions.NullLogger<Services.PaymentTimeoutReaper>.Instance,
                _mongo.Orders);
            var reaped = await reaper.ReapExpiredOrdersAsync(CancellationToken.None);

            Assert.Equal(1, reaped);
            Assert.Equal(OrderStatuses.PaymentTimeout, (await _mongo.FindAsync(id))!.Status);
        }

        [MongoFact]
        public async Task Migration_output_is_written_even_when_nothing_matched()
        {
            var output = new StringWriter();
            var migration = new OrderStatusMigration(_mongo.Orders, output);

            var report = await migration.RunAsync(apply: false, CancellationToken.None);

            Assert.Equal(0, report.Found);
            Assert.Contains("Order status migration", output.ToString());
        }
    }
}