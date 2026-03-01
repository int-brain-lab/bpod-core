"""Miscellaneous tools that don't fit the other categories."""

import difflib
import errno
import json
import logging
import re
import socket
import struct
from collections.abc import Iterable, Iterator, Mapping, MutableMapping, Sequence
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

import msgspec
from filelock import FileLock
from pydantic import Field, RootModel

logger = logging.getLogger(__name__)

K = TypeVar('K')
V = TypeVar('V')

RE_NON_ALPHANUMERIC = re.compile(r'[^a-zA-Z0-9_]')
"""Match non-alphanumeric characters except underscores."""
RE_ACRONYM = re.compile(r'([A-Z]+)([A-Z][a-z])')
"""Match acronym boundaries."""
RE_CASE_TRANSITION = re.compile(r'(?<=[a-z])(?=[A-Z])|(?<=\D)(?=\d)|(?<=\d)(?=\D)')
"""Match case and digit transitions."""
RE_MULTIPLE_UNDERSCORES = re.compile(r'_{2,}')
"""Match multiple consecutive underscores."""


class DocstringInheritanceMixin:
    """Mixin that automatically inherits docstrings from parent classes.

    When a subclass overrides a method or property without providing its own docstring,
    this mixin copies the docstring from the nearest parent class that defines one. This
    avoids having to duplicate docstrings across abstract methods and their concrete
    implementations.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name, attr in vars(cls).items():
            if (callable(attr) or isinstance(attr, property)) and not attr.__doc__:
                for base in cls.__mro__[1:]:
                    base_attr = vars(base).get(name)
                    if base_attr is not None and base_attr.__doc__:
                        attr.__doc__ = base_attr.__doc__
                        break


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
    """
    string = RE_NON_ALPHANUMERIC.sub('_', string)
    string = RE_ACRONYM.sub(r'\1_\2', string)
    string = RE_CASE_TRANSITION.sub('_', string)
    string = RE_MULTIPLE_UNDERSCORES.sub('_', string)
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
    valid_strings : Iterable[str]
        An iterable of valid strings to compare against.
    format_string : str, optional
        The format string for the suggestion. Defaults to " - did you mean '{}'?".
    cutoff : float, optional
        The similarity threshold for considering a match. Defaults to 0.6.

    Returns
    -------
    str
        A formatted suggestion string if a match is found, otherwise an empty string.
    """
    matches = difflib.get_close_matches(invalid_string, valid_strings, 1, cutoff)
    return format_string.format(matches[0]) if len(matches) > 0 else ''


def set_nested(d: MutableMapping, keys: Sequence[Any], value: Any) -> None:
    """
    Set a value in a nested dict, creating intermediate dicts as needed.

    Parameters
    ----------
    d : MutableMapping
        The dictionary in which to set the value.
    keys : Sequence
        A sequence of keys representing the nested path where the value should be set.
    value : Any
        The value to set at the specified path.
    """
    if not keys:
        return  # Do nothing if keys is empty

    current = d
    for key in keys[:-1]:
        current = current.setdefault(key, {})

    current[keys[-1]] = value


def get_nested(d: MutableMapping, keys: Sequence[Any], default: Any = None) -> Any:
    """
    Retrieve a value from a nested dict using a Sequence of keys.

    Parameters
    ----------
    d : MutableMapping
        The dictionary from which to get a value.
    keys : Sequence
        A sequence of keys representing the path to the desired value.
    default : Any, optional
        The value to return if the path does not exist. Defaults to None.

    Returns
    -------
    Any
        The value at the nested path, or default if any key in the path is missing.
    """
    for key in keys:
        if not isinstance(d, MutableMapping) or key not in d:
            return default
        d = d[key]
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
            if e.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL}:
                return '127.0.0.1'
            raise


class SettingsDict(MutableMapping):
    """
    A dictionary-like persistent settings storage backed by a JSON file.

    This class implements the MutableMapping interface, storing key-value pairs that are
    automatically persisted to a JSON file on every write. It supports standard
    dictionary operations (get, set, delete, iterate) as well as nested key access.

    File access is protected by a file lock for safe concurrent access from multiple
    processes.
    """

    def __init__(self, json_path: PathLike | str) -> None:
        """Initialize the SettingsDict instance.

        Parameters
        ----------
        json_path : PathLike or str
            Path to the JSON configuration file.
        """
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

    def __getitem__(self, key: Any) -> Any:
        return self._state[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        if self._state.get(key) == value:
            return
        self._state[key] = value
        self._save_to_file()

    def __contains__(self, key: Any) -> bool:
        return key in self._state

    def __delitem__(self, key: Any) -> None:
        del self._state[key]
        self._save_to_file()

    def __iter__(self) -> Iterator[Any]:
        return iter(list(self._state))

    def __len__(self) -> int:
        return len(self._state)

    def __repr__(self) -> str:
        return repr(self._state)

    def get_nested(self, keys: Sequence[Any], default: Any | None = None) -> Any:
        """Retrieve a nested value using a sequence of keys.

        Parameters
        ----------
        keys : Sequence
            A sequence of keys representing the nested path.
        default : Any, optional
            The value to return if the path does not exist. Defaults to None.

        Returns
        -------
        Any
            The value at the nested path, or default if any key in the path is missing.
        """
        return get_nested(d=self._state, keys=keys, default=default)

    def set_nested(self, keys: Sequence[Any], value: Any) -> None:
        """Set a nested value using a sequence of keys.

        Parameters
        ----------
        keys : Sequence
            A sequence of keys representing the nested path.
        value : Any
            The value to set at the nested path.
        """
        if get_nested(d=self._state, keys=keys) == value:
            return
        set_nested(d=self._state, keys=keys, value=value)
        self._save_to_file()


class ValidatedDict(RootModel[dict[K, V]], MutableMapping[K, V], Generic[K, V]):
    """A dict-like container with runtime validation for keys and values.

    This class wraps a standard :py:class:`dict` and integrates with Pydantic's
    :class:`RootModel` to validate keys and values upon mutation. It behaves like a
    mutable mapping for all common operations (get, set, delete, iterate, len) and
    compares equal to regular dicts with the same contents.

    Notes
    -----
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
    """

    root: dict[K, V] = Field(default_factory=dict)

    def __getitem__(self, key: K) -> V:
        return self.root[key]

    def __setitem__(self, key: K, value: V) -> None:
        validated = type(self).model_validate({key: value}).root
        self.root[key] = validated[key]

    def __delitem__(self, key: K) -> None:
        del self.root[key]

    def __iter__(self) -> Iterator[K]:  # type: ignore[override]
        return iter(self.root)

    def __len__(self) -> int:
        return len(self.root)

    def __repr__(self) -> str:
        return repr(self.root)

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
    values : Sequence[int]
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

    References
    ----------
    https://docs.python.org/3/library/struct.html#format-characters
    """
    if values:
        byte_array.extend(struct.pack(f'<{len(values)}{fmt}', *values))


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
    remove_root : bool, optional
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
