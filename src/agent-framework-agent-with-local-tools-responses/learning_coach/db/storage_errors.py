"""Translate SQLite adapter failures into the application-owned error contract."""

from functools import wraps
import sqlite3

from learning_coach.service.ports import StorageError


def storage_boundary(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except sqlite3.Error:
            # The underlying transaction has already rolled back. Neither SQL
            # nor database paths should cross into the application response.
            raise StorageError("storage_operation_failed") from None
    return wrapped
