"""Compare dotted numeric versions component by component."""


def newer(candidate, current):
    return tuple(candidate.split(".")) > tuple(current.split("."))
