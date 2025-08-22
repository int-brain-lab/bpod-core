import errno

import pytest

from bpod_core import misc


class TestSanitizeString:
    def test_basic_substitution(self):
        assert misc.sanitize_string(' foo bar-123 ') == '_foo_bar_123_'

    def test_custom_substitute(self):
        assert misc.sanitize_string('foo bar!', substitute='-') == 'foo-bar-'

    def test_invalid_types(self):
        with pytest.raises(TypeError):
            misc.sanitize_string('foo', substitute=1)  # type: ignore
        with pytest.raises(TypeError):
            misc.sanitize_string(1)  # type: ignore


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('Foo Bar', 'foo_bar'),
        (' Foo Bar ', 'foo_bar'),
        ('FooBar', 'foo_bar'),
        ('Foo_Bar', 'foo_bar'),
        ('Foo__Bar', 'foo_bar'),
        ('_Foo_Bar_', 'foo_bar'),
        ('123Bar', '123_bar'),
        ('Foo123', 'foo_123'),
    ],
)
def test_convert_to_snake_case(text, expected):
    assert misc.convert_to_snake_case(text) == expected


class TestSuggestSimilar:
    @pytest.fixture
    def fruits(self):
        return ['apple', 'banana', 'grape']

    def test_close_match(self, fruits):
        result = misc.suggest_similar('appl', fruits, cutoff=0.6)
        assert result == " - did you mean 'apple'?"

    def test_no_close_match(self, fruits):
        result = misc.suggest_similar('xyz', fruits, cutoff=0.6)
        assert result == ''

    def test_custom_format_string(self, fruits):
        result = misc.suggest_similar('banan', fruits, format_string='{}?', cutoff=0.6)
        assert result == 'banana?'

    def test_empty_valid_strings(self):
        result = misc.suggest_similar('apple', [], cutoff=0.6)
        assert result == ''

    def test_invalid_string_is_valid(self, fruits):
        result = misc.suggest_similar('banana', fruits, cutoff=0.6)
        assert result == " - did you mean 'banana'?"


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


class TestGetNested:
    def test_existing_value(self):
        d = {'a': {'b': {'c': 42}}}
        result = misc.get_nested(d, ['a', 'b', 'c'])
        assert result == 42

    def test_missing_key(self):
        d = {'a': {'b': {}}}
        result = misc.get_nested(d, ['a', 'b', 'c'])
        assert result is None  # Default is None

    def test_missing_key_with_default(self):
        d = {'a': {'b': {}}}
        result = misc.get_nested(d, ['a', 'b', 'c'], default=99)
        assert result == 99  # Should return the default value

    def test_empty_dict(self):
        d = {}
        result = misc.get_nested(d, ['a', 'b', 'c'])
        assert result is None  # Default is None

    def test_empty_dict_with_default(self):
        d = {}
        result = misc.get_nested(d, ['a', 'b', 'c'], default=99)
        assert result == 99  # Should return the default value

    def test_top_level_key(self):
        d = {'a': 42}
        result = misc.get_nested(d, ['a'])
        assert result == 42

    def test_non_dict_value(self):
        d = {'a': 42}
        result = misc.get_nested(d, ['a', 'b', 'c'])
        assert result is None  # Default is None

    def test_nested_with_non_dict(self):
        d = {'a': {'b': 42}}
        result = misc.get_nested(d, ['a', 'b', 'c'])
        assert result is None  # Default is None


class TestGetLocalIPv4:
    @pytest.fixture
    def mock_socket(self, mocker):
        return mocker.patch('socket.socket')

    def test_successful_ip_retrieval(self, mock_socket):
        mock_instance = mock_socket.return_value.__enter__.return_value
        mock_instance.getsockname.return_value = ('192.168.1.10', 0)

        result = misc.get_local_ipv4()
        assert result == '192.168.1.10'

    def test_network_unreachable(self, mock_socket):
        # Mock the socket to raise an OSError for network unreachable
        mock_instance = mock_socket.return_value.__enter__.return_value
        mock_instance.connect.side_effect = OSError(
            errno.ENETUNREACH, 'Network is unreachable'
        )

        result = misc.get_local_ipv4()
        assert result == '127.0.0.1'

    def test_host_unreachable(self, mock_socket):
        # Mock the socket to raise an OSError for host unreachable
        mock_instance = mock_socket.return_value.__enter__.return_value
        mock_instance.connect.side_effect = OSError(
            errno.EHOSTUNREACH, 'Host is unreachable'
        )

        result = misc.get_local_ipv4()
        assert result == '127.0.0.1'

    def test_address_not_available(self, mock_socket):
        # Mock the socket to raise an OSError for address not available
        mock_instance = mock_socket.return_value.__enter__.return_value
        mock_instance.connect.side_effect = OSError(
            errno.EADDRNOTAVAIL, 'Address not available'
        )

        result = misc.get_local_ipv4()
        assert result == '127.0.0.1'

    def test_unexpected_os_error(self, mock_socket):
        # Mock the socket to raise an unexpected OSError
        mock_instance = mock_socket.return_value.__enter__.return_value
        mock_instance.connect.side_effect = OSError(errno.EACCES, 'Permission denied')

        with pytest.raises(OSError):
            misc.get_local_ipv4()
