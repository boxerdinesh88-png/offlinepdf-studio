"""Undo/redo history semantics."""

from __future__ import annotations

from offlinepdf.core.history import Command, History


class Bag:
    items: list[str]


class Append(Command):
    description = "append"

    def __init__(self, text: str, bag: Bag) -> None:
        self.text = text
        self.bag = bag

    def redo(self, session) -> None:
        self.bag.items.append(self.text)

    def undo(self, session) -> None:
        self.bag.items.pop()


class Mergeable(Command):
    """Absorbs follow-up appends: one undo reverses the whole run."""

    description = "append-run"

    def __init__(self, text: str, bag: Bag) -> None:
        self.texts = [text]
        self.bag = bag

    def redo(self, session) -> None:
        for text in self.texts:
            self.bag.items.append(text)

    def undo(self, session) -> None:
        for _ in self.texts:
            self.bag.items.pop()

    def merge_with(self, other: Command) -> bool:
        if isinstance(other, Mergeable):
            self.texts.extend(other.texts)
            return True
        return False


def _bag() -> Bag:
    bag = Bag()
    bag.items = []
    return bag


def test_push_executes_immediately():
    bag = _bag()
    h = History(session=None)
    h.push(Append("a", bag))
    assert bag.items == ["a"]
    assert h.can_undo and not h.can_redo


def test_undo_redo_cycle():
    bag = _bag()
    h = History(session=None)
    h.push(Append("a", bag))
    h.push(Append("b", bag))
    assert bag.items == ["a", "b"]
    assert h.undo_description == "append"

    assert h.undo() is True
    assert bag.items == ["a"]
    assert h.redo_description == "append"
    assert h.redo() is True
    assert bag.items == ["a", "b"]
    assert h.undo() and h.undo()
    assert bag.items == []
    assert h.undo() is False
    assert h.redo() and h.redo()
    assert bag.items == ["a", "b"]
    assert h.redo() is False


def test_push_clears_redo_stack():
    bag = _bag()
    h = History(session=None)
    h.push(Append("a", bag))
    h.undo()
    assert h.can_redo
    h.push(Append("z", bag))
    assert not h.can_redo
    assert bag.items == ["z"]


def test_mergeable_absorbs_follow_up():
    bag = _bag()
    h = History(session=None)
    h.push(Mergeable("a", bag))
    h.push(Mergeable("b", bag))
    assert bag.items == ["a", "b"]
    assert h.can_undo
    h.undo()  # single undo reverses both
    assert bag.items == []


def test_listeners_and_clear():
    bag = _bag()
    h = History(session=None)
    calls: list[int] = []
    h.add_listener(lambda _s: calls.append(1))
    h.push(Append("a", bag))
    h.undo()
    h.clear()
    assert len(calls) >= 3
    assert not h.can_undo and not h.can_redo


def test_listener_error_does_not_break_history():
    bag = _bag()
    h = History(session=None)
    h.add_listener(lambda _s: 1 / 0)
    h.push(Append("a", bag))  # must not raise
    assert bag.items == ["a"]


def test_undo_description_empty():
    h = History(session=None)
    assert h.undo_description == ""
    assert h.redo_description == ""
