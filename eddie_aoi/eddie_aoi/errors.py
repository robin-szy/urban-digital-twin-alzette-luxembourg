"""Errors raised when an AOI cannot safely be built.

``PreparationError`` is a byte-identical copy of the class in
``users/devika/src/landuse.py`` (commit fa5d58b), kept here so the vendored
routing in ``routing.py`` raises the same type it raises upstream.
"""


class PreparationError(ValueError):
    """An input cannot safely be prepared for static ingestion."""
