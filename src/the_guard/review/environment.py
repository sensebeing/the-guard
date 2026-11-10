"""Normalize GUARD_ARCH_REVIEW_* aliases into canonical THE_GUARD_REVIEW_* keys."""
import os


def review_environment(env=None):
    result = dict(os.environ if env is None else env)
    for key, value in list(result.items()):
        if key.startswith("GUARD_ARCH_REVIEW_"):
            suffix = key[len("GUARD_ARCH_REVIEW_"):]
            result.setdefault("THE_GUARD_REVIEW_" + suffix, value)
    return result
