"""Miscellaneous tools that don't fit the other categories."""

import difflib
import errno
import json
import logging
import re
import socket
import struct
from collections.abc import (
    Hashable,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextlib import contextmanager
from enum import IntEnum
from os import PathLike, strerror
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

import msgspec
from filelock import FileLock
from pydantic import Field, RootModel
from typing_extensions import Self, override

logger = logging.getLogger(__name__)

K = TypeVar('K', bound=Hashable)
"""Key type variable for generic mappings such as :class:`ValidatedDict`."""
V = TypeVar('V')
"""Value type variable for generic mappings such as :class:`ValidatedDict`."""

_MISSING = object()
"""Sentinel for distinguishing a missing key from a key set to None."""

_RE_NON_ALPHANUMERIC = re.compile(r'[^a-zA-Z0-9_]')
"""Match non-alphanumeric characters except underscores."""
_RE_ACRONYM = re.compile(r'([A-Z]+)([A-Z][a-z])')
"""Match acronym boundaries."""
_RE_CASE_TRANSITION = re.compile(r'(?<=[a-z])(?=[A-Z])|(?<=\D)(?=\d)|(?<=\d)(?=\D)')
"""Match case and digit transitions."""
_RE_MULTIPLE_UNDERSCORES = re.compile(r'_{2,}')
"""Match multiple consecutive underscores."""


class ByteEnum(IntEnum):
    r"""An :class:`~enum.IntEnum` restricted to single-byte values (0–255).

    Subclass this to define enums whose integer values fit in one unsigned byte. Each
    member additionally exposes its value as a :class:`bytes` object via
    :attr:`byte_value` for zero-allocation wire encoding.

    Raises
    ------
    OverflowError
        If a member's value is outside the range of a single, unsigned byte.

    Examples
    --------
    Subclass :class:`ByteEnum` to create a custom enum type:

    >>> class Color(ByteEnum):
    ...     RED = 1
    ...     GREEN = 2

    It can be used like a regular :class:`~enum.IntEnum`:

    >>> Color.RED.value
    1

    Additionally, you can access it's values as :class:`bytes` objects:

    >>> Color.RED.byte_value
    b'\x01'
    """

    _as_bytes: bytes

    def __new__(cls, value: int) -> 'Self':
        """Create a new ByteEnum member."""
        obj: Self = int.__new__(cls, value)
        obj._value_ = value
        try:
            obj._as_bytes = value.to_bytes(1, 'little', signed=False)
        except OverflowError:
            msg = f'Value must fit in a single, unsigned byte - got {value!r}'
            raise OverflowError(msg) from None
        return obj

    @property
    def byte_value(self) -> bytes:
        """The member's :attr:`~enum.Enum.value` as a :class:`bytes` object."""
        return self._as_bytes


def to_snake_case(string: str) -> str:
    """
    Convert a given string to snake_case.

    Parameters
    ----------
    string : str
        The input string to be converted.

    Returns
    -------
    str
        The converted snake_case string.

    Examples
    --------
    >>> to_snake_case('CamelCase')
    'camel_case'
    >>> to_snake_case('HTMLParser')
    'html_parser'
    >>> to_snake_case('version2dot0')
    'version_2_dot_0'
    """
    string = _RE_NON_ALPHANUMERIC.sub('_', string)
    string = _RE_ACRONYM.sub(r'\1_\2', string)
    string = _RE_CASE_TRANSITION.sub('_', string)
    string = _RE_MULTIPLE_UNDERSCORES.sub('_', string)
    string = string.strip('_')
    return string.lower()


def suggest_similar(
    invalid_string: str,
    valid_strings: Iterable[str],
    format_string: str = " - did you mean '{}'?",
    cutoff: float = 0.6,
) -> str:
    """
    Suggest a similar valid string based on the given invalid string.

    This function uses a similarity matching algorithm to find the closest match from an
    iterable of valid strings. If a match is found above the specified cutoff, it
    returns a formatted suggestion string.

    Parameters
    ----------
    invalid_string : str
        The string that is invalid or misspelled.
    valid_strings : Iterable of str
        An iterable of valid strings to compare against.
    format_string : str, default: " - did you mean '{}'?"
        The format string for the suggestion.
    cutoff : float, default: 0.6
        The similarity threshold for considering a match.

    Returns
    -------
    str
        A formatted suggestion string if a match is found, otherwise an empty string.

    Examples
    --------
    >>> 'no such port' + suggest_similar('Prot1', ['Port1', 'Port2'])
    "no such port - did you mean 'Port1'?"
    >>> 'no such port' + suggest_similar('xyz', ['Port1', 'Port2'])
    'no such port'
    """
    matches = difflib.get_close_matches(invalid_string, valid_strings, 1, cutoff)
    return format_string.format(matches[0]) if len(matches) > 0 else ''


class SuggestionDict(dict[str, V]):
    """A dictionary that suggests similar keys on failed lookup.

    On :class:`KeyError`, raises ``error_class`` with a message that includes the
    closest match from the existing keys via :func:`suggest_similar`, making typos and
    near-misses easier to diagnose.

    Parameters
    ----------
    dictionary : MutableMapping
        Initial key-value pairs.
    name : str, default: 'key'
        Human-readable label for the key type used in the error message.
    error_class : type of Exception, default: KeyError
        Exception class to raise on failed lookup. Must accept a single string argument.

    Examples
    --------
    >>> d = SuggestionDict({'Port1': 1, 'Port2': 2}, name='channel')
    >>> d['Port1']
    1
    >>> d['Prot1']
    Traceback (most recent call last):
        ...
    KeyError: "No such channel: 'Prot1' - did you mean 'Port1'?"
    """

    def __init__(
        self,
        dictionary: MutableMapping[str, V],
        *,
        name: str | None = None,
        error_class: type[Exception] = KeyError,
    ) -> None:
        super().__init__(dictionary)
        self._name = name or 'key'
        self._error_class = error_class

    @override
    def __getitem__(self, key: str) -> V:
        try:
            return super().__getitem__(key)
        except KeyError as e:
            raise self._error_class(
                f"No such {self._name}: '{key}'" + suggest_similar(key, self.keys())
            ) from e


def set_nested(d: MutableMapping, keys: Sequence[Hashable], value: Any) -> None:
    """
    Set a value in a nested dict, creating intermediate dicts as needed.

    Parameters
    ----------
    d : MutableMapping
        The dictionary in which to set the value.
    keys : Sequence of Hashable
        A sequence of keys representing the nested path where the value should be set.
    value : Any
        The value to set at the specified path.

    Examples
    --------
    >>> from bpod_core.misc import set_nested
    >>> dictionary = {}
    >>> set_nested(dictionary, ['a', 'b', 'c'], 42)
    >>> dictionary
    {'a': {'b': {'c': 42}}}

    >>> set_nested(dictionary, ['a', 'b', 'x'], 99)
    >>> dictionary
    {'a': {'b': {'c': 42, 'x': 99}}}
    """
    if not keys:
        return  # Do nothing if keys is empty

    current = d
    for key in keys[:-1]:
        current = current.setdefault(key, {})

    current[keys[-1]] = value


def get_nested(d: MutableMapping, keys: Sequence[Hashable], default: Any = None) -> Any:
    """
    Retrieve a value from a nested dict using a Sequence of keys.

    Parameters
    ----------
    d : MutableMapping
        The dictionary from which to get a value.
    keys : Sequence of Hashable
        A sequence of keys representing the path to the desired value.
    default : Any, default: None
        The value to return if the path does not exist.

    Returns
    -------
    Any
        The value at the nested path, or default if any key in the path is missing.

    Examples
    --------
    >>> from bpod_core.misc import get_nested
    >>> dictionary = {'a': {'b': {'c': 42}}}
    >>> get_nested(dictionary, ['a', 'b', 'c'])
    42

    >>> get_nested(dictionary, ['a', 'x'], default='missing')
    'missing'
    """
    for key in keys:
        try:
            d = d[key]
        except (KeyError, TypeError):  # noqa: PERF203
            return default
    return d


def get_local_ipv4() -> str:
    """
    Determine the primary local IPv4 address of the machine.

    This function attempts to determine the IPv4 address of the local machine
    that would be used for an outbound connection to the internet. It does this
    by creating a UDP socket and connecting to a known public IP address
    (Google DNS at 8.8.8.8). No data is sent, but the OS uses the routing table
    to select the appropriate local interface.

    Returns
    -------
    str
        The local IPv4 address as a string. If the network is unreachable or
        unavailable, returns the loopback address `127.0.0.1`.

    Raises
    ------
    OSError
        If an unexpected socket error occurs during interface detection.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(('8.8.8.8', 80))  # Doesn't have to be reachable
            return str(s.getsockname()[0])
        except OSError as e:
            if e.errno in {
                errno.ENETUNREACH,  # network is unreachable
                errno.EHOSTUNREACH,  # no route to host
                errno.EADDRNOTAVAIL,  # cannot assign requested address
                errno.ENETDOWN,  # network is down
            }:
                logger.warning('%s - using loopback', strerror(e.errno))
                return '127.0.0.1'
            raise


class SettingsDict(MutableMapping[str, Any]):
    """
    A dictionary-like persistent settings storage backed by a JSON file.

    This class implements the MutableMapping interface, storing key-value pairs that are
    automatically persisted to a JSON file on every write. It supports standard
    dictionary operations (get, set, delete, iterate) as well as nested key access.

    File access is protected by a file lock for safe concurrent access from multiple
    processes.

    Parameters
    ----------
    json_path : PathLike or str
        Path to the JSON configuration file.
    """

    def __init__(self, json_path: PathLike | str) -> None:
        self._json_path = Path(json_path).resolve()
        self._lock_path = self._json_path.with_suffix('.lock')
        self._file_lock = FileLock(self._lock_path)
        self._state = self._load_from_file()

    def _load_from_file(self) -> dict:
        if not self._json_path.exists():
            return {}
        with self._file_lock, self._json_path.open('r') as f:
            data = f.read()
        try:
            return cast('dict', msgspec.json.decode(data))
        except msgspec.DecodeError:
            return {}

    def _save_to_file(self) -> None:
        dictionary = msgspec.to_builtins(self._state)
        self._json_path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock, self._json_path.open('w') as f:
            json.dump(dictionary, f, indent=2)

    @override
    def __getitem__(self, key: str) -> Any:
        return self._state[key]

    @override
    def __setitem__(self, key: str, value: Any) -> None:
        if self._state.get(key) == value:
            return
        old_value = self._state.get(key, _MISSING)
        self._state[key] = value
        try:
            self._save_to_file()
        except Exception:
            if old_value is _MISSING:
                del self._state[key]
            else:
                self._state[key] = old_value
            raise

    @override
    def __contains__(self, key: object) -> bool:
        return key in self._state

    @override
    def __delitem__(self, key: str) -> None:
        old_value = self._state.pop(key)
        try:
            self._save_to_file()
        except Exception:
            self._state[key] = old_value
            raise

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(list(self._state))

    @override
    def __len__(self) -> int:
        return len(self._state)

    @override
    def __repr__(self) -> str:
        return repr(self._state)

    def get_nested(self, keys: Sequence[str], default: Any | None = None) -> Any:
        """Retrieve a nested value using a sequence of keys.

        Parameters
        ----------
        keys : Sequence of str
            A sequence of keys representing the nested path.
        default : Any, default: None
            The value to return if the path does not exist.

        Returns
        -------
        Any
            The value at the nested path, or default if any key in the path is missing.
        """
        return get_nested(d=self._state, keys=keys, default=default)

    def set_nested(self, keys: Sequence[str], value: Any) -> None:
        """Set a nested value using a sequence of keys.

        Parameters
        ----------
        keys : Sequence of str
            A sequence of keys representing the nested path.
        value : Any
            The value to set at the nested path.
        """
        old_value = get_nested(d=self._state, keys=keys, default=_MISSING)
        if old_value == value:
            return
        set_nested(d=self._state, keys=keys, value=value)
        try:
            self._save_to_file()
        except Exception:
            if old_value is _MISSING:
                set_nested(d=self._state, keys=keys[:-1], value={})
            else:
                set_nested(d=self._state, keys=keys, value=old_value)
            raise


class ValidatedDict(RootModel[dict[K, V]], MutableMapping[K, V]):
    """A dict-like container with runtime validation for keys and values.

    This class wraps a standard :py:class:`dict` and integrates with Pydantic's
    :class:`~pydantic.RootModel` to validate keys and values upon mutation. It behaves
    like a mutable mapping for all common operations (get, set, delete, iterate, len)
    and compares equal to regular dicts with the same contents.

    Parameters
    ----------
    root : Mapping, optional
        Initial key-value pairs. Defaults to an empty :class:`dict`.

    Examples
    --------
    Subclass :class:`ValidatedDict` to create a custom type with validation:

    >>> class TestDict(ValidatedDict[str, int]):
    ...     pass

    You can then instantiate your class ``TestDict`` like a regular dict:

    >>> test_dict = TestDict()
    >>> test_dict['foo'] = 1
    >>> test_dict[42] = 2
    Traceback (most recent call last):
       ...
    pydantic_core._pydantic_core.ValidationError: 1 validation error for TestDict
    42.[key]
      Input should be a valid string [type=string_type, input_value=42, input_type=int]
      ...

    Alternatively, you can also instantiate a ValidatedDict directly:

    >>> my_validated_dict = ValidatedDict[str, int]({'foo': 1, 'bar': 2})
    >>> my_validated_dict['foo'] = 'bar'
    Traceback (most recent call last):
       ...
    pydantic_core._pydantic_core.ValidationError: 1 validation error ...
    foo
      Input should be a valid integer, unable to parse string as an integer ...
      ...
    """

    root: dict[K, V] = Field(default_factory=dict)
    """
    Underlying :class:`dict`; mutating it directly bypasses validation.

    :meta private:
    """

    @override
    def __getitem__(self, key: K) -> V:
        return self.root[key]

    @override
    def __setitem__(self, key: K, value: V) -> None:
        validated = type(self).model_validate({key: value}).root
        self.root[key] = validated[key]

    @override
    def __delitem__(self, key: K) -> None:
        del self.root[key]

    @override
    def __iter__(self) -> Iterator[K]:  # type: ignore[override]
        return iter(self.root)

    @override
    def __len__(self) -> int:
        return len(self.root)

    @override
    def __repr__(self) -> str:
        return repr(self.root)

    @override
    def __eq__(self, other: object) -> bool:
        return self.root == other

    if TYPE_CHECKING:

        def __init__(self, root: Mapping[K, V] | None = ...) -> None: ...

        def __hash__(self) -> int: ...
    else:
        __hash__ = None


def extend_packed(
    byte_array: bytearray,
    values: Sequence[int],
    fmt: str,
) -> None:
    """Extend a bytearray with packed binary values.

    This function takes a sequence of integers, converts each one to bytes using the
    specified struct format character, and appends the result to the given bytearray.
    All values use the same format character and are packed in little-endian byte order.

    Parameters
    ----------
    byte_array : bytearray
        The bytearray that will be modified in-place by appending the packed binary
        data.
    values : Sequence of int
        Integer values to convert to binary. The number of values determines how many
        times the format character is repeated.
    fmt : str
        A single struct format character that defines how each value is encoded.
        Common options: 'b' (int8), 'h' (int16), 'i' (int32), 'q' (int64), 'B' (uint8),
        'H' (uint16), 'I' (uint32), 'Q' (uint64).

    Examples
    --------
    >>> from bpod_core.misc import extend_packed
    >>> buffer = bytearray()
    >>> extend_packed(buffer, [1, 2, 3], 'i')
    >>> len(buffer)
    12
    >>> buffer.hex()
    '010000000200000003000000'

    See Also
    --------
    `Format characters
    <https://docs.python.org/3/library/struct.html#format-characters>`__ used by the
    :mod:`struct` module.
    """
    if values:
        byte_array.extend(struct.pack(f'<{len(values)}{fmt.lstrip("<>")}', *values))


def prune_empty_parent_directories(
    target_directory: PathLike | str,
    root_directory: PathLike | str,
    *,
    remove_root: bool = False,
) -> None:
    """Remove empty parent directories recursively up to root directory.

    Recursively removes the given directory if empty, then checks and removes parent
    directories up to (and optionally including) the root directory. Stops at the first
    non-empty directory encountered.

    Parameters
    ----------
    target_directory : PathLike or str
        Directory to check and remove if empty.
    root_directory : PathLike or str
        Root directory to stop at. Must be a parent directory of target_directory.
    remove_root : bool, default: False
        If True, also remove root_directory if it becomes empty.

    Raises
    ------
    ValueError
        If target_directory is not a subpath of root_directory.
    FileNotFoundError
        If target_directory or root_directory does not exist.
    NotADirectoryError
        If target_directory or root_directory is not a directory.
    """
    target_directory = Path(target_directory).absolute()
    root_directory = Path(root_directory).absolute()

    for path in (target_directory, root_directory):
        if not path.exists():
            raise FileNotFoundError(f"'{path}' does not exist")
        if not path.is_dir():
            raise NotADirectoryError(f"'{path}' is not a directory")

    if not target_directory.is_relative_to(root_directory):
        raise ValueError(
            f"'{target_directory}' is not a sub-directory of '{root_directory}'"
        )

    if target_directory == root_directory:
        if remove_root:
            try:
                root_directory.rmdir()
            except OSError:
                return
        return

    try:
        target_directory.rmdir()
    except OSError:
        return

    parent = target_directory.parent
    if parent.is_dir():  # Guard against race condition
        prune_empty_parent_directories(
            target_directory=parent,
            root_directory=root_directory,
            remove_root=remove_root,
        )


@contextmanager
def suppress_logging(level: int = logging.CRITICAL) -> Iterator[None]:
    """
    Temporarily suppress logging up to and including the specified level.

    The previous global logging disable level is restored when exiting the
    context, even if an exception is raised.

    Parameters
    ----------
    level : int, default=logging.CRITICAL
        Logging level to suppress. Messages at this level and below will be
        ignored while the context is active. The default suppresses all
        standard logging messages.

    Yields
    ------
    None
    """
    previous = logging.root.manager.disable
    logging.disable(level)
    try:
        yield
    finally:
        logging.disable(previous)
