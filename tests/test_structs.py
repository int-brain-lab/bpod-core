"""Tests for the data structures in bpod_core.bpod.structs."""

from collections.abc import Iterator
from typing import get_args

from bpod_core.bpod import structs


def _all_subclasses(cls: type) -> Iterator[type]:
    """Yield all direct and indirect subclasses of a class."""
    for subclass in cls.__subclasses__():
        yield subclass
        yield from _all_subclasses(subclass)


def test_bpod_message_union_is_complete():
    """Union TypeAliases cover every concrete BpodMessage subclass."""
    assert {
        *get_args(structs.BpodRequestUnion),
        *get_args(structs.BpodReplyUnion),
        *get_args(structs.BpodEventUnion),
    } == set(_all_subclasses(structs.BpodMessage))


def test_bpod_event_tag():
    """Event tags should encode to a single byte MessagePack fixint."""
    for tag in structs._EventTag:
        assert tag.value >= -32, f"Value of tag '{tag.name}' is too small for a fixint"
        assert tag.value <= 127, f"Value of '{tag.name}' is too large for a fixint"
