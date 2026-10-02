using System;
using System.Net.Sockets;
using Xunit;

namespace OrderService.Tests
{
    /// <summary>
    /// Пропуск интеграционных тестов, когда MongoDB недоступна.
    ///
    /// Решение принимается на этапе discovery, а не внутри тела теста: xUnit
    /// не умеет динамически пропускать уже запущенный тест, зато умеет
    /// помечать его Skip до запуска. Поэтому проверка доступности Mongo
    /// сводится к TCP-пробе порта - полноценная авторизация тут не нужна,
    /// нужен лишь ответ "слушает ли кто-то на этом порту".
    ///
    /// Локально это даёт зелёную сборку без Docker. В CI сервис MongoDB
    /// обязателен, и при его отсутствии падать должен сам CI, а не молча
    /// зелёный прогон с пропущенными тестами - это делает отдельный шаг
    /// проверки в workflow.
    /// </summary>
    internal static class MongoProbe
    {
        private const int ConnectTimeoutMs = 1500;

        private static readonly Lazy<bool> Reachable = new(Check, LazyThreadSafetyMode.ExecutionAndPublication);

        public static bool IsReachable => Reachable.Value;

        private static bool Check()
        {
            var url = Environment.GetEnvironmentVariable("TEST_MONGO_URL")
                ?? Environment.GetEnvironmentVariable("MONGO_URL");

            if (string.IsNullOrWhiteSpace(url))
            {
                return false;
            }

            if (!Uri.TryCreate(url, UriKind.Absolute, out var uri))
            {
                return false;
            }

            var port = uri.IsDefaultPort ? 27017 : uri.Port;

            try
            {
                using var socket = new TcpClient();
                var connect = socket.ConnectAsync(uri.Host, port);
                return connect.Wait(ConnectTimeoutMs) && socket.Connected;
            }
            catch (Exception ex) when (ex is SocketException or AggregateException or InvalidOperationException)
            {
                return false;
            }
        }

        internal static string SkipReason =>
            "MongoDB is not reachable. Set TEST_MONGO_URL (or MONGO_URL) to run integration tests.";
    }

    /// <summary>Факт, требующий живой MongoDB. Без неё пропускается.</summary>
    public sealed class MongoFactAttribute : FactAttribute
    {
        public MongoFactAttribute()
        {
            if (!MongoProbe.IsReachable)
            {
                Skip = MongoProbe.SkipReason;
            }
        }
    }

    /// <summary>Теория, требующая живой MongoDB. Без неё пропускается целиком.</summary>
    public sealed class MongoTheoryAttribute : TheoryAttribute
    {
        public MongoTheoryAttribute()
        {
            if (!MongoProbe.IsReachable)
            {
                Skip = MongoProbe.SkipReason;
            }
        }
    }
}