"""Unlimited undo/redo history using the command pattern.

Commands are the only way mutations reach a :class:`~offlinepdf.core.document.PdfSession`
(once editing lands in M3).  Each command stores enough state to undo itself;
history is unbounded but stores only compressed page-level before/after bytes.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

__all__ = ["Command", "History", "HistoryListener"]

logger = logging.getLogger(__name__)


class Command(ABC):
    """A reversible document mutation."""

    description: str = "edit"
    #: False for commands that only touch editor-side layers (e.g. inserted
    #: images); listeners can then skip re-rendering the PDF pages.
    mutates_document: bool = True

    @abstractmethod
    def redo(self, session: Any) -> None:
        """Apply the command."""

    @abstractmethod
    def undo(self, session: Any) -> None:
        """Revert the command."""

    def merge_with(self, other: Command) -> bool:
        """Return True when ``other`` can be absorbed into this command."""
        return False


HistoryListener = object  # callable(session) - kept loose to avoid Qt dependency


class History:
    """Undo/redo stacks with change notification."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._undo: list[Command] = []
        self._redo: list[Command] = []
        self._listeners: list[Any] = []
        self._last: Command | None = None

    def push(self, command: Command) -> None:
        """Execute ``command`` and record it, clearing the redo stack."""
        command.redo(self._session)
        if self._undo and self._undo[-1].merge_with(command):
            self._redo.clear()
        else:
            self._undo.append(command)
            self._redo.clear()
        logger.info("command: %s (undo depth %d)", command.description, len(self._undo))
        self._last = command
        self._notify()

    def record(self, command: Command) -> None:
        """Record ``command`` after the caller already applied it.

        Used when the mutation happens in several prepared steps (content
        stream rewrites) where running ``redo()`` up front would be wasteful.
        The command's ``redo`` still runs when the edit is repeated via redo().
        """
        if self._undo and self._undo[-1].merge_with(command):
            self._redo.clear()
        else:
            self._undo.append(command)
            self._redo.clear()
        logger.info("command: %s (undo depth %d)", command.description, len(self._undo))
        self._last = command
        self._notify()

    def undo(self) -> bool:
        """Undo the newest command. Returns False when nothing to undo."""
        if not self._undo:
            return False
        command = self._undo.pop()
        command.undo(self._session)
        self._redo.append(command)
        logger.info("undo: %s", command.description)
        self._last = command
        self._notify()
        return True

    def redo(self) -> bool:
        """Redo the newest undone command. Returns False when nothing to redo."""
        if not self._redo:
            return False
        command = self._redo.pop()
        command.redo(self._session)
        self._undo.append(command)
        logger.info("redo: %s", command.description)
        self._last = command
        self._notify()
        return True

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def last_command(self) -> Command | None:
        """The command most recently pushed, recorded, undone or redone."""
        return self._last

    @property
    def undo_description(self) -> str:
        return self._undo[-1].description if self._undo else ""

    @property
    def redo_description(self) -> str:
        return self._redo[-1].description if self._redo else ""

    def add_listener(self, listener: Any) -> None:
        self._listeners.append(listener)

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._last = None
        self._notify()

    def _notify(self) -> None:
        for listener in self._listeners:
            try:
                listener(self._session)
            except Exception:
                logger.exception("history listener failed")
