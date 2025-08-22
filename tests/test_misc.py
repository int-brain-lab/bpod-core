import errno
import socket

import pytest

from bpod_core import misc


class TestSanitizeString:
    def test_basic_substitution(self):
        assert misc.sanitize_string(' hello world-123 ') == '_hello_world_123_'

    def test_custom_substitute(self):
        assert misc.sanitize_string('hello world!', substitute='-') == 'hello-world-'
        assert misc.sanitize_string('a+b=c', substitute='X') == 'aXbXc'

    def test_invalid_types(self):
        with pytest.raises(TypeError):
            misc.sanitize_string('test', substitute=1)  # type: ignore
        with pytest.raises(TypeError):
            misc.sanitize_string(1)  # type: ignore


def test_convert_to_snake_case():
    assert misc.convert_to_snake_case('Hello World') == 'hello_world'
    assert misc.convert_to_snake_case(' Hello World ') == 'hello_world'
    assert misc.convert_to_snake_case('HelloWorld') == 'hello_world'
    assert misc.convert_to_snake_case('Hello_World') == 'hello_world'
    assert misc.convert_to_snake_case('Hello__World') == 'hello_world'
    assert misc.convert_to_snake_case('_Hello_World_') == 'hello_world'
    assert misc.convert_to_snake_case('123Test') == '123_test'
    assert misc.convert_to_snake_case('Test123') == 'test_123'


def test_suggest_similar():
    # Test when there's a close match
    result = misc.suggest_similar('appl', ['apple', 'banana', 'grape'], cutoff=0.6)
    assert result == " - did you mean 'apple'?"

    # Test when there's no close match
    result = misc.suggest_similar('xyz', ['apple', 'banana', 'grape'], cutoff=0.6)
    assert result == ''

    # Test with custom format string
    result = misc.suggest_similar(
        'banan',
        ['apple', 'banana', 'grape'],
        format_string="Did you mean '{}'?",
        cutoff=0.6,
    )
    assert result == "Did you mean 'banana'?"

    # Test with low cutoff (no match above cutoff)
    result = misc.suggest_similar('banana', ['apple', 'grape'], cutoff=0.9)
    assert result == ''

    # Test with an empty valid_strings list
    result = misc.suggest_similar('apple', [], cutoff=0.6)
    assert result == ''

    # Test when the invalid_string is exactly one of the valid strings
    result = misc.suggest_similar('banana', ['banana', 'apple', 'grape'], cutoff=0.6)
    assert result == ''


class TestSetNested:
    def test_basic_case(self):
        d = {}
        misc.set_nested(d, ['a', 'b', 'c'], 42)
        assert d == {'a': {'b': {'c': 42}}}

    def test_intermediate_dictionaries_exist(self):
        d = {'a': {'b': {}}}
        misc.set_nested(d, ['a', 'b', 'c'], 42)
        assert d == {'a': {'b': {'c': 42}}}

    def test_overwriting_existing_value(self):
        d = {'a': {'b': {'c': 10}}}
        misc.set_nested(d, ['a', 'b', 'c'], 42)
        assert d == {'a': {'b': {'c': 42}}}

    def test_setting_value_at_top_level(self):
        d = {}
        misc.set_nested(d, ['a'], 42)
        assert d == {'a': 42}

    def test_deeply_nested_value(self):
        d = {}
        misc.set_nested(d, ['a', 'b', 'c', 'd'], 42)
        assert d == {'a': {'b': {'c': {'d': 42}}}}

    def test_empty_dict_empty_key_list(self):
        d = {}
        misc.set_nested(d, [], 42)  # Should do nothing
        assert d == {}

    def test_empty_dict_one_key(self):
        d = {}
        misc.set_nested(d, ['a'], 42)  # Should set the key "a" to 42
        assert d == {'a': 42}


class TestGetLocalIPv4:
    def test_returns_valid_ipv4(self):
        ip = misc.get_local_ipv4()
        parts = ip.split('.')
        assert len(parts) == 4
        assert all(0 <= int(p) < 256 for p in parts)

    def test_fallback_to_loopback_on_unreachable(self, monkeypatch):
        class DummySocket:
            def connect(self, addr):
                raise OSError(errno.ENETUNREACH, 'Network unreachable')

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(socket, 'socket', lambda *a, **k: DummySocket())
        ip = misc.get_local_ipv4()
        assert ip == '127.0.0.1'
