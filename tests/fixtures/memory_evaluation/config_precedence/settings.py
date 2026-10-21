"""Combine default settings with settings requested by an operator."""


def merge_settings(defaults, requested):
    return {**requested, **defaults}
