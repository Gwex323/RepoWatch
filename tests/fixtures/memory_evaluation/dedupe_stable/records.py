"""Keep one record for each ID in arrival order."""


def unique_records(rows):
    chosen = {}
    for row in rows:
        chosen.setdefault(row["id"], row)
    return list(chosen.values())
