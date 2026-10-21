"""Spend quota only when a request can be accepted."""


def accept(remaining, requested):
    remaining -= requested
    if remaining < 0:
        return False, remaining
    return True, remaining
