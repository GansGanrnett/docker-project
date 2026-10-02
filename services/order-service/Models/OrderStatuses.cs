namespace OrderService.Models
{
    /// <summary>
    /// Статусы заказа. Единственный источник истины: сравнения статусов идут
    /// по строке, поэтому литералы разбросаны по сервису и рано или поздно
    /// расходятся. Issue #34.
    /// </summary>
    public static class OrderStatuses
    {
        /// <summary>
        /// Создан, платёж инициирован, финального ответа платёжной системы нет.
        /// Начальное состояние заказа: из него заказ уходит ровно в одно из
        /// <see cref="Paid"/>, <see cref="PaymentDeclined"/>, <see cref="PaymentTimeout"/>.
        /// </summary>
        public const string PaymentPending = "PaymentPending";

        /// <summary>Платёж подтверждён. Терминальное.</summary>
        public const string Paid = "Paid";

        /// <summary>Платёжная система отклонила платёж. Терминальное.</summary>
        public const string PaymentDeclined = "PaymentDeclined";

        /// <summary>
        /// Платёж не завершён за отведённое время и более не ожидается.
        /// Терминальное. Ставит PaymentTimeoutReaper.
        /// </summary>
        public const string PaymentTimeout = "PaymentTimeout";

        /// <summary>
        /// Значение, которым заказы записывались до issue #34. В базе ещё есть.
        /// Только чтение: никогда не пишем это значение, только трактуем как
        /// <see cref="PaymentPending"/> при выборках. Снимается после миграции.
        /// </summary>
        public const string LegacyPendingPayment = "PendingPayment";

        /// <summary>
        /// Значения, из которых заказ ещё может уйти в терминальное состояние.
        /// Используется в фильтрах MongoDB, поэтому включает legacy-значение.
        /// </summary>
        public static readonly string[] Pending = { PaymentPending, LegacyPendingPayment };

        /// <summary>Из терминальных состояний переходов нет.</summary>
        public static readonly string[] Terminal = { Paid, PaymentDeclined, PaymentTimeout };
    }
}
