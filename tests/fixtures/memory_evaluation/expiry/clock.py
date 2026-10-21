"""A lease expires as soon as its deadline is reached."""


def expired(created_at, now, lifetime):
    if lifetime < 0:
        raise ValueError("lifetime must be non-negative")
    deadline = created_at + lifetime
    return now > deadline
