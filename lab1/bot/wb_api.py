"""
Модуль получения данных по магазину Wildberries.

ВАЖНО: сейчас этот модуль возвращает тестовые данные.
Когда будем подключать настоящий API Wildberries, менять нужно будет
только функцию get_daily_stats. Остальной код бота трогать не придется,
потому что формат возвращаемых данных останется таким же.
"""

import random
from datetime import date, timedelta


def get_yesterday() -> date:
    """Возвращает вчерашнюю дату."""
    return date.today() - timedelta(days=1)


def get_daily_stats(api_key: str, report_date: date = None) -> dict:
    """
    Возвращает показатели магазина за указанный день.

    Аргументы:
        api_key: ключ доступа к кабинету продавца Wildberries
        report_date: за какой день нужны данные (по умолчанию за вчера)

    Возвращает словарь с показателями. Именно в этом формате потом
    будут приходить настоящие данные из API Wildberries.
    """
    if report_date is None:
        report_date = get_yesterday()

    # Проверяем, что ключ вообще передан.
    # Когда подключим настоящий API, здесь будет проверка его валидности.
    if not api_key:
        raise ValueError("Не задан API-ключ Wildberries")

    # Тестовые данные генерируем на основе даты.
    # Это сделано для того, чтобы за один и тот же день цифры были
    # одинаковыми, а за разные дни отличались, как в реальной жизни.
    generator = random.Random(report_date.toordinal())

    orders_count = generator.randint(18, 60)
    average_price = generator.randint(1200, 3500)
    orders_rub = orders_count * average_price

    # Выкупают обычно не все, что заказали: примерно от 60 до 85 процентов
    buyout_share = generator.uniform(0.60, 0.85)
    buyouts_count = max(1, int(orders_count * buyout_share))
    buyouts_rub = int(buyouts_count * average_price)

    # Расходы на рекламу обычно составляют от 5 до 20 процентов от заказов
    ad_share = generator.uniform(0.05, 0.20)
    ad_spend_rub = int(orders_rub * ad_share)

    # Воронка продаж: переход в карточку товара, добавление в корзину, заказ
    card_views = generator.randint(1500, 6000)
    cart_adds = int(card_views * generator.uniform(0.05, 0.12))
    # Заказов не может быть больше, чем добавлений в корзину
    cart_adds = max(cart_adds, orders_count)

    return {
        "date": report_date,
        "orders_rub": orders_rub,
        "orders_count": orders_count,
        "buyouts_rub": buyouts_rub,
        "buyouts_count": buyouts_count,
        "ad_spend_rub": ad_spend_rub,
        # ДРР это доля рекламных расходов: сколько процентов от суммы заказов
        # мы потратили на рекламу
        "drr_percent": calculate_drr(ad_spend_rub, orders_rub),
        # Сколько раз открыли карточку товара и сколько раз положили в корзину.
        # Эти цифры нужны, чтобы посчитать конверсии
        "card_views": card_views,
        "cart_adds": cart_adds,
        # Конверсия из перехода в корзину: сколько процентов тех, кто открыл
        # карточку, положили товар в корзину
        "conversion_to_cart_percent": calculate_conversion(cart_adds, card_views),
        # Конверсия из корзины в заказ: сколько процентов тех, кто положил
        # товар в корзину, оформили заказ
        "conversion_cart_to_order_percent": calculate_conversion(orders_count, cart_adds),
        # Конверсия из перехода в заказ: сколько процентов тех, кто открыл
        # карточку, в итоге заказали. Это вся воронка целиком
        "conversion_to_order_percent": calculate_conversion(orders_count, card_views),
    }


def calculate_conversion(result: float, base: float) -> float:
    """
    Считает конверсию в процентах: сколько из base дошли до result.

    Если база нулевая, возвращаем 0, чтобы не делить на ноль.
    """
    if not base:
        return 0.0
    return round(result / base * 100, 1)


def calculate_drr(ad_spend_rub: float, orders_rub: float) -> float:
    """
    Считает ДРР (долю рекламных расходов) в процентах.

    Формула: расходы на рекламу делим на сумму заказов и умножаем на 100.
    Если заказов не было, возвращаем 0, чтобы не делить на ноль.
    """
    if not orders_rub:
        return 0.0
    return round(ad_spend_rub / orders_rub * 100, 1)
