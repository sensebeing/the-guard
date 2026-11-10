"""One environment compatibility boundary; public GUARD keys take precedence."""
import os


def review_environment(env=None):
    result = dict(os.environ if env is None else env)
    for key, value in list(result.items()):
        if key.startswith("GUARD_ARCH_REVIEW_"):
            result["ALTER_ARCH_REVIEW_" + key[len("GUARD_ARCH_REVIEW_"):]] = value
    for key, value in list(result.items()):
        if key.startswith("THE_GUARD_REVIEW_"):
            result["ALTER_ARCH_REVIEW_" + key[len("THE_GUARD_REVIEW_"):]] = value
    return result
