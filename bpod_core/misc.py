"""Miscellaneous tools that don't fit the other categories."""

import difflib
import re
from typing import Any

RE_SANITIZE = re.compile(r'[^a-zA-Z0-9_]')
RE_SNAKE_CASE = re.compile(r'(?<=[a-z])(?=[A-Z\d])')
RE_UNDERSCORES = re.compile(r'_{2,}|_$|^_')


def convert_to_snake_case(input_str: str) -> str:
    """
    Convert a given string to snake_case.

    This function replaces spaces with underscores and inserts underscores
    between lowercase and uppercase letters to convert a string to snake_case.

    Parameters
    ----------
    input_str : str
        The input string to be converted.

    Returns
    -------
    str
        The converted snake_case string.
    """
    input_str = input_str.replace(' ', '_')
    snake_case_str = RE_SANITIZE.sub('', input_str)
    snake_case_str = RE_SNAKE_CASE.sub('_', snake_case_str)
    snake_case_str = RE_UNDERSCORES.sub('_', snake_case_str)
    return snake_case_str.lower()


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
