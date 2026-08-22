from decimal import Decimal

from src.pricing import money, money_sum


def whole_coin_reward(value: Decimal) -> Decimal:
    if not value.is_finite() or value < 0:
        raise ValueError('Награда должна быть целым неотрицательным числом')
    integral = value.to_integral_value()
    if value != integral:
        raise ValueError('Награда должна быть целым неотрицательным числом')
    return integral


def manual_coin_adjustment(value: Decimal) -> Decimal:
    if not value.is_finite() or not value:
        raise ValueError('Сумма должна быть ненулевым числом')
    normalized = money(value)
    if normalized != value:
        raise ValueError('Используйте не более двух знаков после запятой')
    return normalized


def adjusted_coin_balance(
    balance: Decimal,
    adjustment: Decimal,
) -> tuple[Decimal, Decimal]:
    adjustment = manual_coin_adjustment(adjustment)
    balance_after = money_sum(balance, adjustment)
    if balance_after < 0:
        raise ValueError('Баланс пользователя не может стать отрицательным')
    return adjustment, balance_after
