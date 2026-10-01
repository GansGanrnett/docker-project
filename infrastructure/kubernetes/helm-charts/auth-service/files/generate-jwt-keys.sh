#!/bin/sh
# Генерация ключевой пары RS256 для auth-service.
#
# Каноническое место хранения - здесь, рядом с чартом. Скрипт смонтирован в
# ConfigMap и выполняется hook-job'ом, поэтому hook-job и локальный запуск
# используют ровно одну и ту же реализацию. Точка входа для разработчика -
# scripts/generate-jwt-keys.sh (тонкая обёртка над этим файлом).
#
# Раньше шаг не был описан нигде: private.pem отсутствовал в репозитории (он в
# .gitignore), а auth-service падал при старте на создании бина jwtAlgorithm().
# Специально поэтому README не может быть единственным носителем этого шага -
# он и есть фикст #33.
#
# Форматы подобраны под то, что ждёт код:
#   private.pem -> PKCS#8 ("BEGIN PRIVATE KEY"), KeyFactory + PKCS8EncodedKeySpec
#   public.pem  -> X.509 SPKI ("BEGIN PUBLIC KEY"),  X509EncodedKeySpec
#
# Использование:
#   generate-jwt-keys.sh [OUT_DIR]          сгенерировать, если ключей нет
#   generate-jwt-keys.sh --check [OUT_DIR]  только проверить, ничего не менять
#
# Помимо OUT_DIR публичный ключ копируется в каталоги потребителей, если они
# уже существуют. Compose монтирует public.pem в API Gateway и Order Service из
# services/api-gateway/configs, то есть без копирования стек поднимается на
#половину: эти два контейнера падают, а auth-service работает. Копировать
# безопасно - публичный ключ публичен по определению; приватный остаётся
# только в OUT_DIR.
#
# В hook-job эти каталоги не существуют, поэтому шаг там просто пропускается.
#
# Идемпотентность: если пара уже есть и валидна - ничего не делаем. Ключи нельзя
# перегенерировать при каждом развёртывании: все выпущенные токены станут
# невалидными и реплики перестанут доверять друг другу.
#
# Требуется: openssl. В кластере ставится в hook-job, см. values.yaml -> jwt.keygen.

set -eu

KEY_SIZE="${JWT_KEY_SIZE:-4096}"
SCRIPT_NAME="generate-jwt-keys.sh"

die() {
    printf '%s: %s\n' "$SCRIPT_NAME" "$*" >&2
    exit 1
}

usage() {
    # Печатаем всю шапку до первой не-комментарной строки. Жёсткий диапазон
    # строк однажды отрежет описание посередине - и это случилось уже один раз.
    sed -n '2,${/^#/!q;s/^#\{1,\} \{0,1\}//p;}' "$0"
}

# --- разбор аргументов -------------------------------------------------------

MODE=generate
if [ "${1:-}" = "--check" ]; then
    MODE=check
    shift
elif [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
    exit 0
fi

# Каноническое расположение. Публикация ключа потребителям выполняется только
# для него - произвольный OUT_DIR означает "работаем вне репозитория".
DEFAULT_OUT_DIR="services/auth-service/configs"
OUT_DIR="${1:-$DEFAULT_OUT_DIR}"
PRIVATE="$OUT_DIR/private.pem"
PUBLIC="$OUT_DIR/public.pem"

command -v openssl >/dev/null 2>&1 || die "openssl не найден в PATH"

# --- проверка пары -----------------------------------------------------------

# Модуль приватного ключа обязан совпадать с модулем публичного: это ловит
# рассинхрон после того, как "перевыпустили только половину ключей".
# $1 - файл, $2 - дополнительные флаги ("", "-pubin").
modulus() {
    # shellcheck disable=SC2086
    openssl rsa $2 -in "$1" -noout -modulus 2>/dev/null || true
}

check_pair() {
    [ -f "$PRIVATE" ] || die "отсутствует $PRIVATE"
    [ -f "$PUBLIC" ] || die "отсутствует $PUBLIC"

    # Заголовки обязаны быть ровно те, что разбирает JwtConfig: он вырезает
    # BEGIN/END и декодирует остаток base64. Неверный заголовок - это
    # FileNotFoundException на старте либо ClassCastException в generatePrivate.
    grep -q '^-----BEGIN PRIVATE KEY-----$' "$PRIVATE" \
        || die "$PRIVATE: ожидается PKCS#8 (BEGIN PRIVATE KEY)"
    grep -q '^-----BEGIN PUBLIC KEY-----$' "$PUBLIC" \
        || die "$PUBLIC: ожидается X.509 SPKI (BEGIN PUBLIC KEY)"

    # openssl pkey -check валидирует структуру ASN.1 целиком, а не только base64.
    openssl pkey -in "$PRIVATE" -noout -check >/dev/null 2>&1 \
        || die "$PRIVATE: не читается как приватный ключ (битый base64 или ASN.1)"
    openssl pkey -pubin -in "$PUBLIC" -noout >/dev/null 2>&1 \
        || die "$PUBLIC: не читается как публичный ключ (битый base64 или ASN.1)"

    priv_mod="$(modulus "$PRIVATE" "")"
    pub_mod="$(modulus "$PUBLIC" "-pubin")"
    [ -n "$priv_mod" ] || die "$PRIVATE: не удалось прочитать модуль"
    [ -n "$pub_mod" ] || die "$PUBLIC: не удалось прочитать модуль"
    [ "$priv_mod" = "$pub_mod" ] || die "пара ключей не совпадает: модули разные"

    bits="$(openssl rsa -in "$PRIVATE" -noout -text 2>/dev/null \
        | sed -n 's/.*Private-Key: (\([0-9][0-9]*\) bit.*/\1/p')"
    printf 'ключи валидны, RSA-%s\n' "${bits:-неизвестно}"
}

# --- публикация публичного ключа потребителям --------------------------------

# Каталоги, где сервисы ожидают найти public.pem. Состав взят из docker-compose.yml:
# auth-service монтирует весь configs, api-gateway - свой configs целиком, а
# order-service монтирует public.pem из каталога api-gateway. Копируем только в
# уже существующие каталоги: создавать их наугад опаснее, чем не скопировать.
CONSUMERS="services/auth-service/configs services/api-gateway/configs"

publish_public() {
    # Публикация имеет смысл только для канонического расположения. Если ключи
    # кладут в произвольный OUT_DIR - как это делает CI с временным каталогом -
    # трогать каталоги репозитория нельзя: шаг превратился бы в побочный эффект
    # проверки и оставлял бы за собой изменённые файлы.
    if [ "$OUT_DIR" != "$DEFAULT_OUT_DIR" ]; then
        return 0
    fi

    for dir in $CONSUMERS; do
        [ -d "$dir" ] || continue
        # При set -e конструкция "тест && continue" убила бы скрипт, когда тест
        # ложен, поэтому здесь именно if.
        if [ "$dir" = "$OUT_DIR" ]; then
            continue
        fi

        # Коммитимые public.pem перезаписываются без спроса, и это осознанно.
        # В репозитории нет ни одного приватного ключа, значит лежащий в git
        # публичный ключ не соответствует ни одной доступной паре и бесполезен.
        # Если оставить его на месте, compose стартует с ключом, который не
        # проверит ни один токен, - и отказ будет выглядеть как баг в JWT, а
        # не как устаревшая копия ключа.
        if cmp -s "$dir/public.pem" "$PUBLIC"; then
            continue
        fi

        if [ -f "$dir/public.pem" ]; then
            printf '%s: %s/public.pem устарел, заменён ключом из текущей пары\n' \
                "$SCRIPT_NAME" "$dir"
        fi
        cp "$PUBLIC" "$dir/public.pem"
        chmod 644 "$dir/public.pem"
    done
}

# --- режим check -------------------------------------------------------------

if [ "$MODE" = check ]; then
    check_pair
    exit 0
fi

# --- режим generate ----------------------------------------------------------

mkdir -p "$OUT_DIR"

if [ -f "$PRIVATE" ] && [ -f "$PUBLIC" ]; then
    # Пара уже есть: валидная - выходим молча, битая - сообщаем и НЕ трогаем.
    # Молча перегенерировать нельзя: это инвалидирует все выпущенные токены.
    if check_pair >/dev/null 2>&1; then
        printf 'ключи уже существуют и валидны, генерация пропущена\n'
        # Публикацию повторяем: каталоги потребителей могли появиться уже после
        # первой генерации, и тогда compose поднялся бы наполовину.
        publish_public
        exit 0
    fi
    printf '%s: существующая пара не прошла проверку, файлы оставлены как есть\n' \
        "$SCRIPT_NAME" >&2
    printf '  удалите %s и %s вручную, если хотите перевыпустить ключи\n' \
        "$PRIVATE" "$PUBLIC" >&2
    exit 1
fi

# Не дописываем половину пары: приватный без публичного и наоборот.
if [ -f "$PRIVATE" ] || [ -f "$PUBLIC" ]; then
    die "найдена только половина пары ($PRIVATE / $PUBLIC). Удалите её и запустите заново"
fi

umask 077

printf 'генерация RSA-%s в %s ...\n' "$KEY_SIZE" "$OUT_DIR"
openssl genpkey -algorithm RSA -pkeyopt "rsa_keygen_bits:$KEY_SIZE" -out "$PRIVATE" 2>/dev/null \
    || die "openssl genpkey не удался"
chmod 600 "$PRIVATE"

openssl pkey -in "$PRIVATE" -pubout -out "$PUBLIC" 2>/dev/null \
    || die "не удалось вывести публичный ключ"
chmod 644 "$PUBLIC"

# Проверяем то, что приготовили, - чтобы ошибка всплыла в логе генерации,
# а не через минуту в CrashLoopBackOff пода.
check_pair >/dev/null

publish_public

printf 'готово:\n  %s (600)\n  %s (644)\n' "$PRIVATE" "$PUBLIC"