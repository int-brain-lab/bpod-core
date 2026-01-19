"""Miscellaneous tools that don't fit the other categories."""

import difflib
import errno
import json
import logging
import re
import socket
import struct
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

import msgspec
from filelock import FileLock
from pydantic import Field, RootModel

logger = logging.getLogger(__name__)

K = TypeVar('K')
V = TypeVar('V')

RE_SANITIZE = re.compile(r'[^a-zA-Z0-9_]')
RE_SNAKE_CASE = re.compile(r'(?<=[a-z])(?=[A-Z])|(?<=\D)(?=\d)|(?<=\d)(?=\D)')
RE_UNDERSCORES = re.compile(r'_{2,}')


def sanitize_string(string: str, substitute='_'):
    """
    Replace non-alphanumeric characters in a string with a given substitute.

    Parameters
    ----------
    string : str
        The input string to be sanitized.
    substitute : str, optional
        The character(s) to replace non-alphanumeric characters with.
        Defaults to '_'.

    Returns
    -------
    str
        A sanitized string where all non-alphanumeric characters have been replaced with
        the specified substitute.

    Raises
    ------
    TypeError
        If either `string` or `substitute` is not an instance of ``str``.
    """
    if not (isinstance(string, str) and isinstance(substitute, str)):
        raise TypeError('Both `string` and `substitute` must be strings.')
    return re.sub(RE_SANITIZE, substitute, string)


def convert_to_snake_case(string: str) -> str:
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
    string = sanitize_string(string)
    string = RE_SNAKE_CASE.sub('_', string)
    string = RE_UNDERSCORES.sub('_', string)
    string = string.strip('_')
    return string.lower()


def suggest_similar(
    invalid_string: str,
    valid_strings: list[str],
    format_string: str = " - did you mean '{}'?",
    cutoff: float = 0.6,
) -> str:
    """
    Suggest a similar valid string based on the given invalid string.

    This function uses a similarity matching algorithm to find the closest match from a
    list of valid strings. If a match is found above the specified cutoff, it returns a
    formatted suggestion string.

    Parameters
    ----------
    invalid_string : str
        The string that is invalid or misspelled.
    valid_strings : list[str]
        A list of valid strings to compare against.
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

    All values are packed using the same format character. For example, fmt='i' packs
    all values as 32-bit signed integers.

    Parameters
    ----------
    byte_array : bytearray
        The bytearray to extend in-place.
    values : typing.Sequence
        The values to pack and append.
    fmt : str
        Format character (e.g., 'i').

    Examples
    --------
    >>> buffer = bytearray()
    >>> extend_packed(buffer, [1, 2, 3], 'i')
    >>> len(buffer)
    12
    >>> buffer.hex()
    '010000000200000003000000'
    """
    format_string = f'<{len(values)}{fmt}'
    byte_array.extend(struct.pack(format_string, *values))
