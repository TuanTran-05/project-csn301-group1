"""Opt-in draft history; no database, SSH, or application startup side effects."""

from .history_manager import HistoryManager

__all__ = ["HistoryManager"]
