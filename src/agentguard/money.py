"""Money is integer minor units (cents) everywhere internally."""

from decimal import Decimal, InvalidOperation


def parse_money(value: object) -> int:
    """Parse '12.99', '$12.99' or 12.99 into cents. Rejects negatives and sub-cent precision."""
    text = str(value).strip().removeprefix("$").replace(",", "")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not a money amount: {value!r}") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"money amount must be a non-negative number: {value!r}")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"money amount has more than two decimal places: {value!r}")
    return int(cents)


def format_cents(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"
