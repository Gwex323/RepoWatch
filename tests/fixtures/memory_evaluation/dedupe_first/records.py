"""Keep one record for each ID in arrival order."""


def unique_records(rows):
    chosen = {}
    for row in rows:
        chosen[row["id"]] = row
    return list(chosen.values())
