"""Shared production-scope exclusions for topology checks and inventory."""

from pathlib import PurePosixPath


def is_excluded_topology_path(path: str) -> bool:
    parts = set(PurePosixPath(path).parts)
    return bool(
        parts
        & {
            "tests",
            "test",
            "migrations",
            "alembic",
            ".venv",
            "__pycache__",
            "scripts",
            "Tests",
            "UITests",
            "Snapshots",
            "__Snapshots__",
        }
        or PurePosixPath(path).name.startswith("test_")
        or path.endswith(("_test.py", "Tests.swift", "Test.swift"))
    )
