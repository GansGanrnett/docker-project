using System.Runtime.CompilerServices;

// Тесты живут в отдельном проекте и обращаются к внутренним членам
// PaymentStatusConsumer: HandleMessageAsync и ResolveTargetStatus помечены
// internal намеренно, чтобы не открывать наружу обработку сообщений.
// Issue #34.
[assembly: InternalsVisibleTo("OrderService.Tests")]
