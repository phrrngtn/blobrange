"""blobrange — Uniform relation addressing for Excel objects in DuckDB."""

from blobrange.connection import get_connection
from blobrange.resolver import ExcelResolver

__all__ = ["get_connection", "ExcelResolver"]
