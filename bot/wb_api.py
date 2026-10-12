"""
Модуль получения данных по магазину Wildberries.

Здесь собраны все запросы к API Wildberries. Данные берутся из трех групп:

1. Отчеты (statistics-api) - заказы и выкупы
2. Продвижение (advert-api) - затраты на рекламу
3. Аналитика (seller-analytics-api) - воронка продаж, из нее считаются конверсии

Токен передается в заголовке Authorization. Для работы бота нужен ключ
с категориями "Статистика", "Продвижение" и "Аналитика".

Остальной код бота про устройство API ничего не знает: он просто вызывает
get_daily_stats и получает готовый словарь с показателями.
"""

import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

# Часовой пояс, в котором Wildberries считает даты
MOSCOW = ZoneInfo("Europe/Moscow")

# Адреса групп API. У каждой группы свой адрес, это нормально
STATS_HOST = "https://statistics-api.wildberries.ru"
ADVERT_HOST = "https://advert-api.wildberries.ru"
ANALYTICS_HOST = "https://seller-analytics-api.wildberries.ru"

# Сколько секунд ждем ответ. Отчеты по заказам бывают большими и идут долго
TIMEOUT = 90

# Сколько раз повторяем запрос, если Wildberries просит подождать
MAX_RETRIES = 3

# Сколько минут держим готовый отчет в памяти.
# Данные за вчерашний день почти не меняются, а лимиты у Wildberries жесткие:
# по заказам и выкупам разрешен только один запрос в минуту. Поэтому повторные
# запросы за тот же день отдаем из памяти, не беспокоя API
CACHE_MINUTES = 30

# Хранилище готовых отчетов: ключ это пара (API-ключ, дата),
# значение это время получения и сами данные
_cache = {}


class WBApiError(Exception):
    """
    Ошибка при обращении к API Wildberries.

    Текст такой ошибки бот показывает пользователю, поэтому он написан
    понятным языком, а не кодами ответов.
    """


def get_yesterday() -> date:
    """
    Возвращает вчерашнюю дату по московскому времени.

    Считать именно по Москве важно: Wildberries все даты понимает как МСК.
    Если считать по времени сервера, то бот на зарубежном хостинге
    ночью запросил бы данные не за тот день.
    """
    return datetime.now(MOSCOW).date() - timedelta(days=1)


# ---------------------------------------------------------------------------
# Общая функция запроса
# ---------------------------------------------------------------------------

def _request(method: str, url: str, api_key: str, **kwargs):
    """
    Отправляет запрос к API Wildberries и возвращает разобранный ответ.

    Берет на себя три вещи:
    - подставляет токен в заголовок Authorization
    - повторяет запрос, если Wildberries ответил 429 (слишком часто)
    - превращает технические ошибки в понятные сообщения
    """
    headers = {"Authorization": api_key}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.request(
                method, url, headers=headers, timeout=TIMEOUT, **kwargs
            )
        except requests.Timeout:
            raise WBApiError(
                "Wildberries слишком долго не отвечает. Попробуйте запросить отчет позже."
            )
        except requests.RequestException as error:
            logger.error("Сетевая ошибка при запросе %s: %s", url, error)
            raise WBApiError("Не удалось связаться с Wildberries. Проверьте интернет.")

        if response.status_code == 200:
            # Иногда при отсутствии данных приходит пустое тело
            if not response.content:
                return []
            try:
                return response.json()
            except ValueError:
                raise WBApiError("Wildberries прислал ответ в неожиданном формате.")

        # 204 означает, что данных за период просто нет
        if response.status_code == 204:
            return []

        if response.status_code == 401:
            raise WBApiError(
                "Wildberries не принял ключ. Проверьте, что он не истек "
                "(ключ живет 180 дней) и что при его создании отмечены категории "
                "Статистика, Продвижение и Аналитика."
            )

        if response.status_code == 403:
            raise WBApiError(
                "У ключа нет доступа к этим данным. "
                "Создайте новый ключ с категориями Статистика, Продвижение и Аналитика."
            )

        if response.status_code == 429:
            # Wildberries сам говорит в заголовке, сколько секунд ждать.
            # Лимит по заказам и выкупам это один запрос в минуту,
            # поэтому ждать иногда приходится до полутора минут
            pause = int(response.headers.get("X-Ratelimit-Retry", 30))
            pause = min(max(pause, 5), 90)
            logger.warning(
                "Лимит запросов, попытка %s из %s, ждем %s сек", attempt, MAX_RETRIES, pause
            )
            if attempt < MAX_RETRIES:
                time.sleep(pause)
                continue
            raise WBApiError(
                "Wildberries ограничивает частоту запросов. Попробуйте через минуту."
            )

        logger.error("Ответ %s от %s: %s", response.status_code, url, response.text[:300])
        raise WBApiError(
            f"Wildberries вернул ошибку {response.status_code}. Попробуйте позже."
        )

    raise WBApiError("Не удалось получить данные от Wildberries.")


# ---------------------------------------------------------------------------
# Заказы и выкупы
# ---------------------------------------------------------------------------

def get_orders(api_key: str, report_date: date) -> dict:
    """
    Возвращает заказы за указанный день: сумму в рублях и количество.

    Параметр flag=1 означает "отдай все заказы за эту дату".
    Без него API отдает заказы по дате последнего изменения, а это другое.

    Отмененные заказы не считаем: признак отмены лежит в поле isCancel.
    Сумму берем из priceWithDisc, это цена со скидкой продавца,
    то есть то, что реально составляет выручку.
    """
    rows = _request(
        "GET",
        f"{STATS_HOST}/api/v1/supplier/orders",
        api_key,
        params={"dateFrom": report_date.isoformat(), "flag": 1},
    )

    orders = [row for row in rows if not row.get("isCancel")]

    return {
        "orders_count": len(orders),
        "orders_rub": int(sum(row.get("priceWithDisc", 0) for row in orders)),
        "cancels_count": len(rows) - len(orders),
    }


def get_buyouts(api_key: str, report_date: date) -> dict:
    """
    Возвращает выкупы за указанный день: сумму в рублях и количество.

    В этот отчет попадают и выкупы, и возвраты. Отличаются они по полю saleID:
    строка начинается с S - это продажа, с R - возврат товара на склад.
    Возвраты вычитаем, чтобы цифра показывала реальный результат дня.
    """
    rows = _request(
        "GET",
        f"{STATS_HOST}/api/v1/supplier/sales",
        api_key,
        params={"dateFrom": report_date.isoformat(), "flag": 1},
    )

    sales = [row for row in rows if str(row.get("saleID", "")).startswith("S")]
    returns = [row for row in rows if str(row.get("saleID", "")).startswith("R")]

    sales_sum = sum(row.get("priceWithDisc", 0) for row in sales)
    # По возвратам суммы могут приходить с минусом, поэтому берем модуль
    # и вычитаем сами. Так формула верна в обоих случаях
    returns_sum = sum(abs(row.get("priceWithDisc", 0)) for row in returns)

    return {
        "buyouts_count": len(sales) - len(returns),
        "buyouts_rub": int(sales_sum - returns_sum),
        "returns_count": len(returns),
    }


# ---------------------------------------------------------------------------
# Затраты на рекламу
# ---------------------------------------------------------------------------

def get_ad_spend(api_key: str, report_date: date) -> int:
    """
    Возвращает затраты на рекламу за указанный день в рублях.

    Метод отдает историю списаний за период. Складываем поле updSum
    по всем строкам: это и есть потраченная сумма.
    """
    day = report_date.isoformat()
    rows = _request(
        "GET",
        f"{ADVERT_HOST}/adv/v1/upd",
        api_key,
        params={"from": day, "to": day},
    )

    if not isinstance(rows, list):
        return 0

    return int(sum(row.get("updSum", 0) for row in rows))


# ---------------------------------------------------------------------------
# Воронка продаж и конверсии
# ---------------------------------------------------------------------------

def get_funnel(api_key: str, report_date: date) -> dict:
    """
    Возвращает воронку продаж за указанный день по всему магазину.

    API отдает данные по каждой карточке товара отдельно и страницами
    максимум по 1000 штук, поэтому проходим по всем страницам и складываем.

    Проценты конверсий API тоже присылает, но по каждой карточке свои.
    Складывать или усреднять проценты нельзя, поэтому берем из ответа
    только счетчики, а конверсии по магазину считаем сами.
    """
    day = report_date.isoformat()
    url = f"{ANALYTICS_HOST}/api/analytics/v3/sales-funnel/products"

    card_views = 0
    cart_adds = 0
    orders_count = 0

    offset = 0
    limit = 1000
    page = 0

    while True:
        page += 1
        body = {
            "selectedPeriod": {"start": day, "end": day},
            "nmIds": [],
            "brandNames": [],
            "subjectIds": [],
            "tagIds": [],
            "skipDeletedNm": False,
            "limit": limit,
            "offset": offset,
        }

        answer = _request("POST", url, api_key, json=body)

        # На всякий случай проверяем, что пришел ожидаемый ответ,
        # а не пустой список или что-то неожиданное
        if not isinstance(answer, dict):
            logger.warning("Воронка вернула неожиданный ответ")
            break

        products = answer.get("data", {}).get("products", [])

        for item in products:
            period = item.get("statistic", {}).get("selected", {})
            card_views += period.get("openCount", 0) or 0
            cart_adds += period.get("cartCount", 0) or 0
            orders_count += period.get("orderCount", 0) or 0

        # Если вернулось меньше, чем просили, значит это была последняя страница
        if len(products) < limit:
            break

        offset += limit
        # У этого метода лимит 3 запроса в минуту, поэтому между
        # страницами выдерживаем паузу, иначе получим отказ
        time.sleep(21)

        # Страховка от бесконечного цикла
        if page >= 20:
            logger.warning("Воронка: остановились на 20 странице")
            break

    return {
        "card_views": card_views,
        "cart_adds": cart_adds,
        "funnel_orders": orders_count,
    }


# ---------------------------------------------------------------------------
# Расчетные показатели
# ---------------------------------------------------------------------------

def calculate_drr(ad_spend_rub: float, orders_rub: float) -> float:
    """
    Считает ДРР (долю рекламных расходов) в процентах.

    Формула: расходы на рекламу делим на сумму заказов и умножаем на 100.
    Если заказов не было, возвращаем 0, чтобы не делить на ноль.
    """
    if not orders_rub:
        return 0.0
    return round(ad_spend_rub / orders_rub * 100, 1)


def calculate_conversion(result: float, base: float) -> float:
    """
    Считает конверсию в процентах: сколько из base дошли до result.

    Если база нулевая, возвращаем 0, чтобы не делить на ноль.
    """
    if not base:
        return 0.0
    return round(result / base * 100, 1)


# ---------------------------------------------------------------------------
# Главная функция: собирает все показатели за день
# ---------------------------------------------------------------------------

def get_daily_stats(api_key: str, report_date: date = None) -> dict:
    """
    Возвращает показатели магазина за указанный день.

    Аргументы:
        api_key: ключ доступа к кабинету продавца Wildberries
        report_date: за какой день нужны данные (по умолчанию за вчера)

    Делает четыре запроса к разным группам API и собирает результат
    в один словарь. Формат словаря тот же, что был с тестовыми данными,
    поэтому остальной код бота менять не пришлось.

    Готовый отчет запоминается на CACHE_MINUTES минут. Если за это время
    запросить тот же день повторно, ответ придет мгновенно и без обращения
    к Wildberries, чтобы не упираться в лимиты.
    """
    if report_date is None:
        report_date = get_yesterday()

    if not api_key:
        raise WBApiError("Не задан API-ключ Wildberries. Добавьте его командой /setkey.")

    # Сначала смотрим, не запрашивали ли мы эти данные недавно
    cached = _cache.get((api_key, report_date))
    if cached and time.time() - cached[0] < CACHE_MINUTES * 60:
        logger.info("Отдаем данные за %s из памяти", report_date)
        return cached[1]

    logger.info("Запрашиваем данные Wildberries за %s", report_date)

    orders = get_orders(api_key, report_date)
    buyouts = get_buyouts(api_key, report_date)
    ad_spend_rub = get_ad_spend(api_key, report_date)
    funnel = get_funnel(api_key, report_date)

    stats = {
        "date": report_date,
        "orders_rub": orders["orders_rub"],
        "orders_count": orders["orders_count"],
        "buyouts_rub": buyouts["buyouts_rub"],
        "buyouts_count": buyouts["buyouts_count"],
        "ad_spend_rub": ad_spend_rub,
        # ДРР это доля рекламных расходов: сколько процентов от суммы заказов
        # мы потратили на рекламу
        "drr_percent": calculate_drr(ad_spend_rub, orders["orders_rub"]),
        # Счетчики воронки, на них считаются конверсии
        "card_views": funnel["card_views"],
        "cart_adds": funnel["cart_adds"],
        # Конверсия из перехода в корзину: сколько процентов тех, кто открыл
        # карточку, положили товар в корзину
        "conversion_to_cart_percent": calculate_conversion(
            funnel["cart_adds"], funnel["card_views"]
        ),
        # Конверсия из корзины в заказ: сколько процентов тех, кто положил
        # товар в корзину, оформили заказ
        "conversion_cart_to_order_percent": calculate_conversion(
            funnel["funnel_orders"], funnel["cart_adds"]
        ),
        # Конверсия из перехода в заказ: вся воронка целиком
        "conversion_to_order_percent": calculate_conversion(
            funnel["funnel_orders"], funnel["card_views"]
        ),
    }

    # Запоминаем результат, чтобы повторный запрос не бил по лимитам
    _cache[(api_key, report_date)] = (time.time(), stats)

    return stats
