"""Tests for bpod_core.misc utilities and helpers."""

import errno

import pytest
from pydantic import ValidationError

from bpod_core import misc
from bpod_core.misc import ValidatedDict


class TestSanitizeString:
    """Tests for sanitize_string utility."""

    def test_basic_substitution(self):
        """Replaces spaces and hyphens with underscores."""
        assert misc.sanitize_string(' foo bar-123 ') == '_foo_bar_123_'

    def test_custom_substitute(self):
        """Uses a custom substitute character."""
        assert misc.sanitize_string('foo bar!', substitute='-') == 'foo-bar-'

    def test_invalid_types(self):
        """Raises TypeError on invalid argument types."""
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
    """Converts various input styles to snake_case."""
    assert misc.convert_to_snake_case(text) == expected


class TestSuggestSimilar:
    """Tests for suggest_similar helper."""

    @pytest.fixture
    def fruits(self):
        """Fixture providing sample fruit names."""
        return ['apple', 'banana', 'grape']

    def test_close_match(self, fruits):
        """Returns formatted suggestion for close match."""
        result = misc.suggest_similar('appl', fruits, cutoff=0.6)
        assert result == " - did you mean 'apple'?"

    def test_no_close_match(self, fruits):
        """Returns empty string when no similar items found."""
        result = misc.suggest_similar('xyz', fruits, cutoff=0.6)
        assert result == ''

    def test_custom_format_string(self, fruits):
        """Supports a custom result format string."""
        result = misc.suggest_similar('banan', fruits, format_string='{}?', cutoff=0.6)
        assert result == 'banana?'

    def test_empty_valid_strings(self):
        """Returns empty string if valid_strings is empty."""
        result = misc.suggest_similar('apple', [], cutoff=0.6)
        assert result == ''

    def test_invalid_string_is_valid(self, fruits):
        """Returns suggestion even if input equals a valid string."""
        result = misc.suggest_similar('banana', fruits, cutoff=0.6)
        assert result == " - did you mean 'banana'?"


class TestSetNested:
    """Tests for set_nested utility."""

    def test_basic_case(self):
        """Sets a nested value creating dicts as needed."""
        d = {}
        misc.set_nested(d, ['a', 'b', 'c'], 42)
        assert d == {'a': {'b': {'c': 42}}}

    def test_intermediate_dictionaries_exist(self):
        """Uses existing intermediate dictionaries without overwriting."""
        d = {'a': {'b': {}}}
        misc.set_nested(d, ['a', 'b', 'c'], 42)
        assert d == {'a': {'b': {'c': 42}}}

    def test_overwriting_existing_value(self):
        """Overwrites an existing nested value."""
        d = {'a': {'b': {'c': 10}}}
        misc.set_nested(d, ['a', 'b', 'c'], 42)
        assert d == {'a': {'b': {'c': 42}}}

    def test_setting_value_at_top_level(self):
        """Sets a value at the top level with a single key."""
        d = {}
        misc.set_nested(d, ['a'], 42)
        assert d == {'a': 42}

    def test_deeply_nested_value(self):
        """Handles multiple levels of nesting."""
        d = {}
        misc.set_nested(d, ['a', 'b', 'c', 'd'], 42)
        assert d == {'a': {'b': {'c': {'d': 42}}}}

    def test_empty_dict_empty_key_list(self):
        """No change when key path is empty."""
        d = {}
        misc.set_nested(d, [], 42)  # Should do nothing
        assert d == {}

    def test_empty_dict_one_key(self):
        """Sets single key in an empty dict."""
        d = {}
        misc.set_nested(d, ['a'], 42)  # Should set the key "a" to 42
        assert d == {'a': 42}


class TestGetNested:
    """Tests for get_nested utility."""

    def test_existing_value(self):
        """Returns the value when all keys exist."""
        d = {'a': {'b': {'c': 42}}}
        result = misc.get_nested(d, ['a', 'b', 'c'])
        assert result == 42

    def test_missing_key(self):
        """Returns None by default if a key is missing."""
        d = {'a': {'b': {}}}
        result = misc.get_nested(d, ['a', 'b', 'c'])
        assert result is None  # Default is None

    def test_missing_key_with_default(self):
        """Returns provided default if a key is missing."""
        d = {'a': {'b': {}}}
        result = misc.get_nested(d, ['a', 'b', 'c'], default=99)
        assert result == 99  # Should return the default value

    def test_empty_dict(self):
        """Returns None when the root dict is empty."""
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


class TestSettingsDict:
    @pytest.fixture
    def temp_settings(self, tmp_path, mocker):
        """Fixture to create a SettingsDict pointing to a temporary directory."""
        mocker.patch('bpod_core.misc.user_config_dir', return_value=str(tmp_path))
        settings = misc.SettingsDict('test_app', 'test_author', 'test_settings.json')
        yield settings

    def test_set_and_get(self, temp_settings):
        """Test setting and retrieving a key-value pair."""
        temp_settings['key'] = 'value'
        assert temp_settings['key'] == 'value'

    def test_update_existing_key(self, temp_settings):
        """Test updating an existing key."""
        temp_settings['key'] = 'old'
        temp_settings['key'] = 'new'
        assert temp_settings['key'] == 'new'

    def test_delete_key(self, temp_settings):
        """Ensure keys can be deleted."""
        temp_settings['key'] = 'value'
        del temp_settings['key']
        assert 'key' not in temp_settings

    def test_delete_nonexistent_key(self, temp_settings):
        """Ensure deleting a nonexistent key raises a KeyError."""
        with pytest.raises(KeyError):
            del temp_settings['missing_key']

    def test_length_of_dict(self, temp_settings):
        """Test the length of the set keys."""
        assert len(temp_settings) == 0
        temp_settings['key1'] = 'value1'
        temp_settings['key2'] = 'value2'
        assert len(temp_settings) == 2

    def test_iterating_keys(self, temp_settings):
        """Ensure keys can be iterated over."""
        temp_settings['key1'] = 'value1'
        temp_settings['key2'] = 'value2'
        assert set(iter(temp_settings)) == {'key1', 'key2'}

    def test_default_on_missing_key(self, temp_settings):
        """Test retrieving a default value for a missing key."""
        assert temp_settings.get('missing_key', 'default') == 'default'

    def test_clear_all_keys(self, temp_settings):
        """Test clearing the entire dictionary by deleting all keys."""
        temp_settings['key1'] = 'value1'
        temp_settings['key2'] = 'value2'
        # Delete all keys from the SettingsDict instance
        for k in list(iter(temp_settings)):
            del temp_settings[k]
        assert len(temp_settings) == 0

    def test_corrupted_file(self, tmp_path, mocker):
        """Test behavior with a corrupted JSON file."""
        corrupted_file = tmp_path / 'test_settings.json'
        corrupted_file.write_text('corrupted json')
        mocker.patch('bpod_core.misc.user_config_dir', return_value=str(tmp_path))
        settings = misc.SettingsDict('test_app', 'test_author', 'test_settings.json')
        assert len(settings) == 0  # Should recover with an empty dict

    def test_repr(self, temp_settings):
        """Test the repr of the SettingsDict."""
        temp_settings['key1'] = 'value1'
        temp_settings['key2'] = 'value2'
        assert repr(temp_settings) == (repr(temp_settings._state))

    def test_persistence_across_instances(self, tmp_path, mocker):
        """Values should persist to disk and be readable by a new instance."""
        mocker.patch('bpod_core.misc.user_config_dir', return_value=str(tmp_path))
        s1 = misc.SettingsDict('test_app', 'test_author', 'persist.json')
        s1['a'] = 1
        s1.set_nested(['nested', 'x'], 42)
        # New instance should read previous state
        s2 = misc.SettingsDict('test_app', 'test_author', 'persist.json')
        assert s2['a'] == 1
        assert s2.get_nested(['nested', 'x']) == 42

    def test_set_and_get_nested_methods(self, temp_settings):
        """Use set_nested and get_nested on the SettingsDict wrapper."""
        temp_settings.set_nested(['level1', 'level2'], 'val')
        assert temp_settings.get_nested(['level1', 'level2']) == 'val'
        # Overwrite nested value
        temp_settings.set_nested(['level1', 'level2'], 'new')
        assert temp_settings.get_nested(['level1', 'level2']) == 'new'

    def test_missing_file_initialization_and_creation_on_write(self, tmp_path, mocker):
        """Dict starts empty and file is created upon first write."""
        mocker.patch('bpod_core.misc.user_config_dir', return_value=str(tmp_path))
        file_name = 'new_settings.json'
        s = misc.SettingsDict('test_app', 'test_author', file_name)
        assert len(s) == 0
        path = tmp_path / file_name
        assert not path.exists()
        s['created'] = True
        assert path.exists()

    def test_contains_operator(self, temp_settings):
        """Test the 'in' operator."""
        temp_settings['present'] = 123
        assert 'present' in temp_settings
        assert 'absent' not in temp_settings


class TestValidatedDict:
    @pytest.fixture
    def validated_dict(self):
        return ValidatedDict[str, int]({})

    def test_set_get_contains(self, validated_dict):
        """Test setting and retrieving a key-value pair."""
        validated_dict['a'] = 1
        assert validated_dict['a'] == 1
        assert 'a' in validated_dict

    def test_len(self, validated_dict):
        """Test the length is assessed correctly."""
        assert len(validated_dict) == 0
        validated_dict['a'] = 1
        assert len(validated_dict) == 1

    def test_iter(self, validated_dict):
        """Ensure keys can be iterated over."""
        assert set(iter(validated_dict)) == set(iter({}))
        validated_dict['a'] = 1
        assert set(iter(validated_dict)) == set(iter({'a': 1}))

    def test_delete(self, validated_dict):
        """Ensure keys can be deleted."""
        validated_dict['a'] = 1
        assert 'a' in validated_dict
        assert len(validated_dict) == 1
        del validated_dict['a']
        assert 'a' not in validated_dict
        assert len(validated_dict) == 0

    def test_repr(self, validated_dict):
        """Test the repr of the ValidatedDict."""
        assert repr(validated_dict) == repr({})
        validated_dict['a'] = 1
        assert repr(validated_dict) == repr({'a': 1})

    def test_equality(self, validated_dict):
        """Test equality operator."""
        assert dict(validated_dict) == {}
        validated_dict['a'] = 1
        assert dict(validated_dict) == {'a': 1}

    def test_runtime_validate_key(self, validated_dict):
        """Test runtime validation of keys."""
        with pytest.raises(ValidationError):
            validated_dict[2] = 1  # type: ignore[assignment]

    def test_runtime_validate_value(self, validated_dict):
        """Test runtime validation of values."""
        with pytest.raises(ValidationError):
            validated_dict['a'] = 'b'  # type: ignore[assignment]

    def test_validation_on_overwrite(self, validated_dict):
        """Test validation on overwrite."""
        validated_dict['k'] = 1
        with pytest.raises(ValidationError):
            validated_dict['k'] = 'a'  # type: ignore[assignment]

    def test_missing_key(self, validated_dict):
        """Test if KeyError is raised when accessing a missing key."""
        with pytest.raises(KeyError):
            _ = validated_dict['missing']
        with pytest.raises(KeyError):
            del validated_dict['missing']
