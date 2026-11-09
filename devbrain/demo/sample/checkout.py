"""A deliberately small checkout, with all amounts expressed in cents."""


def total_cents(prices: list[int]) -> int:
    if any(type(price) is not int or price < 0 for price in prices):
        raise ValueError("Prices must be nonnegative integer cents")
    return sum(prices)
