"""A grant covers its dotted namespace and descendants."""


def is_allowed(granted, requested):
    return requested == granted or requested.startswith(granted + ".")
