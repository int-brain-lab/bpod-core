"""Miscellaneous tools that don't fit the other categories."""

import difflib
import errno
import json
import re
import socket
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any, cast

import msgspec
from appdirs import user_config_dir

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


def set_nested(d: dict[str, Any], keys: list[str], value: Any) -> None:
    """
    Set a value in a nested dict, creating intermediate dicts as needed.

    Parameters
    ----------
    d : dict
        The dictionary in which to set the value.
    keys : list of str
        A list of keys representing the nested path where the value should be set.
    value : Any
        The value to set at the specified path.
    """
    if not keys:
        return  # Do nothing if keys is empty
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def get_nested(d: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    """
    Retrieve a value from a nested dict using a list of keys.

    Parameters
    ----------
    d : dict
        The dictionary from which to get a value.
    keys : list of str
        A list of keys representing the path to the desired value.
    default : Any, optional
        The value to return if the path does not exist. Defaults to None.

    Returns
    -------
    Any
        The value at the nested path, or default if any key in the path is missing.
    """
    for key in keys:
        if not isinstance(d, dict) or key not in d:
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
    A dictionary-like class for managing application settings with persistence.

    This class provides a MutableMapping interface for storing, retrieving,
    and manipulating key-value pairs. Settings are saved to a specified
    file on disk and persist across executions. This allows applications
    to easily manage user settings or configuration data in a structured
    and convenient way.

    Parameters
    ----------
    app_name : str
        The name of the application.
    app_author : str, optional
        The author of the application.
    filename : str, optional
        The name of the configuration file to use, default is 'settings.json'.
    """

    def __init__(
        self,
        app_name: str,
        app_author: str | None = None,
        filename: str = 'settings.json',
    ) -> None:
        config_path = Path(user_config_dir(app_name, app_author))
        config_path.mkdir(parents=True, exist_ok=True)
        self._path = config_path / filename
        self._path.touch(exist_ok=True)
        self._state = self._load_from_file()

    def _load_from_file(self) -> dict:
        with self._path.open('r') as f:
            data = f.read()
        try:
            return cast('dict', msgspec.json.decode(data))
        except msgspec.DecodeError:
            return {}

    def _save_to_file(self) -> None:
        dictionary = msgspec.to_builtins(self._state)
        with self._path.open('w') as f:
            json.dump(dictionary, f, indent=2)

    def __getitem__(self, key: Any) -> Any:
        if key in self._state:
            return self._state.get(key)
        else:
            raise KeyError(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        self._state[key] = value
        self._save_to_file()

    def __contains__(self, key: Any) -> bool:
        return key in self._state

    def __delitem__(self, key: Any) -> None:
        if key in self._state:
            del self._state[key]
            self._save_to_file()
        else:
            raise KeyError(key)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._state)

    def __len__(self) -> int:
        return len(self._state)

    def __repr__(self) -> str:
        return repr(self._state)
