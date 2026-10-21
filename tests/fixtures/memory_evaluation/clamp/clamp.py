"""Clamp a number to an inclusive, ordered interval."""


def clamp(value, lower, upper):
    if lower > upper:
        raise ValueError("lower must not exceed upper")
    return min(max(value, lower), upper)
