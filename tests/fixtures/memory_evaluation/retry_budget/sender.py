"""Call the operation at most budget times, stopping on first success."""


def send_with_budget(send, budget):
    if budget < 1:
        raise ValueError("budget must be positive")
    last_error = None
    for _ in range(budget + 1):
        try:
            return send()
        except RuntimeError as exc:
            last_error = exc
    raise last_error
