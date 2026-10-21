"""Page numbers are one-based; pages beyond the end contain no items."""


def page(items, number, size):
    if number < 1 or size < 1:
        raise ValueError("number and size must be positive")
    start = (number - 1) * size
    if start >= len(items):
        start = max(0, len(items) - size)
    return items[start:start + size]
