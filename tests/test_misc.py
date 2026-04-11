"""Tests for bpod_core.misc utilities and helpers."""

import errno
import struct

import pytest
from pydantic import ValidationError

from bpod_core import misc
from bpod_core.constants import FMT_UINT8
from bpod_core.misc import ByteEnum, LRUCache, ValidatedDict


class TestByteEnum:
    """Tests for ByteEnum."""

    @pytest.fixture
    def op(self):
        """A minimal ByteEnum subclass for testing."""

        class Op(ByteEnum):
            READ = 1
            WRITE = 2
            EXEC = ord('X')

        return Op

    def test_value(self, op):
        """Member has the correct integer value."""
        assert op.READ == 1
        assert op.WRITE == 2

    def test_as_bytes(self, op):
        """as_bytes returns a single-byte bytes object matching the value."""
        assert op.READ.as_bytes == b'\x01'
        assert op.WRITE.as_bytes == b'\x02'

    def test_as_bytes_length(self, op):
        """as_bytes is always exactly one byte."""
        for member in op:
            assert len(member.as_bytes) == 1

    def test_ascii_value(self, op):
        """ord() values are correctly stored and round-trip via as_bytes."""
        assert ord('X') == op.EXEC
        assert op.EXEC.as_bytes == b'X'

    def test_is_int(self, op):
        """Members are integers (IntEnum behaviour preserved)."""
        assert isinstance(op.READ, int)
        assert op.READ + op.WRITE == 3

    def test_iteration(self, op):
        """Iterating over the enum yields all members."""
        assert list(op) == [op.READ, op.WRITE, op.EXEC]

    def test_invalid_negative(self):
        """Negative values raise ValueError."""
        with pytest.raises(ValueError, match='one byte'):

            class Bad(ByteEnum):
                NEG = -1

    def test_invalid_too_large(self):
        """Values above 255 raise ValueError."""
        with pytest.raises(ValueError, match='one byte'):

            class Bad(ByteEnum):
                BIG = 256


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
        ('HTTPSConnection', 'https_connection'),
    ],
)
def test_to_snakecase(text, expected):
    """Converts various input styles to snake_case."""
    assert misc.to_snake_case(text) == expected


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
    def temp_settings(self, tmp_path):
        """Fixture to create a SettingsDict pointing to a temporary directory."""
        return misc.SettingsDict(tmp_path / 'settings.json')

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

    def test_corrupted_file(self, tmp_path):
        """Test behavior with a corrupted JSON file."""
        corrupted_file = tmp_path / 'test_settings.json'
        corrupted_file.write_text('corrupted json')
        settings = misc.SettingsDict(corrupted_file)
        assert len(settings) == 0  # Should recover with an empty dict

    def test_repr(self, temp_settings):
        """Test the repr of the SettingsDict."""
        temp_settings['key1'] = 'value1'
        temp_settings['key2'] = 'value2'
        assert repr(temp_settings) == (repr(temp_settings._state))

    def test_persistence_across_instances(self, tmp_path):
        """Values should persist to disk and be readable by a new instance."""
        s1 = misc.SettingsDict(tmp_path / 'persist.json')
        s1['a'] = 1
        s1.set_nested(['nested', 'x'], 42)
        # New instance should read previous state
        s2 = misc.SettingsDict(tmp_path / 'persist.json')
        assert s2['a'] == 1
        assert s2.get_nested(['nested', 'x']) == 42

    def test_set_and_get_nested_methods(self, temp_settings):
        """Use set_nested and get_nested on the SettingsDict wrapper."""
        temp_settings.set_nested(['level1', 'level2'], 'val')
        assert temp_settings.get_nested(['level1', 'level2']) == 'val'
        # Overwrite nested value
        temp_settings.set_nested(['level1', 'level2'], 'new')
        assert temp_settings.get_nested(['level1', 'level2']) == 'new'

    def test_missing_file_initialization_and_creation_on_write(self, tmp_path):
        """Dict starts empty and file is created upon first write."""
        path = tmp_path / 'new_settings.json'
        s = misc.SettingsDict(path)
        assert len(s) == 0
        assert not path.exists()
        s['created'] = True
        assert path.exists()

    def test_contains_operator(self, temp_settings):
        """Test the 'in' operator."""
        temp_settings['present'] = 123
        assert 'present' in temp_settings
        assert 'absent' not in temp_settings

    def test_setitem_skips_write_when_value_unchanged(self, temp_settings, mocker):
        """Setting the same value should not trigger a file write."""
        temp_settings['key'] = 'value'
        spy = mocker.spy(temp_settings, '_save_to_file')
        temp_settings['key'] = 'value'  # same value
        spy.assert_not_called()

    def test_setitem_writes_when_value_changed(self, temp_settings, mocker):
        """Setting a different value should trigger a file write."""
        temp_settings['key'] = 'value'
        spy = mocker.spy(temp_settings, '_save_to_file')
        temp_settings['key'] = 'new_value'
        spy.assert_called_once()

    def test_set_nested_skips_write_when_value_unchanged(self, temp_settings, mocker):
        """set_nested with the same value should not trigger a file write."""
        temp_settings.set_nested(['a', 'b'], 42)
        spy = mocker.spy(temp_settings, '_save_to_file')
        temp_settings.set_nested(['a', 'b'], 42)  # same value
        spy.assert_not_called()

    def test_set_nested_writes_when_value_changed(self, temp_settings, mocker):
        """set_nested with a different value should trigger a file write."""
        temp_settings.set_nested(['a', 'b'], 42)
        spy = mocker.spy(temp_settings, '_save_to_file')
        temp_settings.set_nested(['a', 'b'], 99)
        spy.assert_called_once()

    def test_setitem_writes_for_new_key(self, temp_settings, mocker):
        """Setting a new key should always trigger a file write."""
        spy = mocker.spy(temp_settings, '_save_to_file')
        temp_settings['new_key'] = 'value'
        spy.assert_called_once()

    def test_set_nested_writes_for_new_path(self, temp_settings, mocker):
        """set_nested with a new path should always trigger a file write."""
        spy = mocker.spy(temp_settings, '_save_to_file')
        temp_settings.set_nested(['new', 'path'], 'value')
        spy.assert_called_once()


class TestExtendPacked:
    """Tests for misc.extend_packed()."""

    def test_pack_unsigned_bytes(self):
        """Packs a list of unsigned bytes."""
        buf = bytearray()
        misc.extend_packed(buf, [1, 2, 255], FMT_UINT8)
        assert buf == b'\x01\x02\xff'

    def test_pack_unsigned_shorts(self):
        """Packs a list of unsigned 16-bit integers (little-endian)."""
        buf = bytearray()
        misc.extend_packed(buf, [1, 256, 65535], 'H')
        assert buf == b'\x01\x00\x00\x01\xff\xff'

    def test_pack_unsigned_ints(self):
        """Packs a list of unsigned 32-bit integers (little-endian)."""
        buf = bytearray()
        misc.extend_packed(buf, [1, 0x01020304], 'I')
        assert buf == b'\x01\x00\x00\x00\x04\x03\x02\x01'

    def test_empty_values(self, mocker):
        """Empty list results in no bytes added."""
        pack = mocker.spy(struct, 'pack')
        buf = bytearray()
        misc.extend_packed(buf, [], 'I')
        assert buf == b''
        assert not pack.called

    def test_extends_existing_buffer(self):
        """Appends to an existing bytearray without overwriting."""
        buf = bytearray(b'\xaa\xbb')
        misc.extend_packed(buf, [1, 2], FMT_UINT8)
        assert buf == b'\xaa\xbb\x01\x02'

    def test_invalid_format_raises_struct_error(self):
        """Raises struct.error for an invalid format character."""
        buf = bytearray()
        with pytest.raises(struct.error):
            misc.extend_packed(buf, [1], 'Z')

    def test_value_out_of_range_raises_struct_error(self):
        """Raises struct.error when value exceeds format range."""
        buf = bytearray()
        with pytest.raises(struct.error):
            misc.extend_packed(buf, [256], FMT_UINT8)  # max for uInt8 is 255

    def test_negative_for_unsigned_raises_struct_error(self):
        """Raises struct.error for negative value with unsigned format."""
        buf = bytearray()
        with pytest.raises(struct.error):
            misc.extend_packed(buf, [-1], FMT_UINT8)


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


class TestPruneEmptyParentDirectories:
    """Tests for prune_empty_parent_directories utility."""

    def test_removes_single_empty_directory(self, tmp_path):
        """Removes a single empty target directory."""
        root = tmp_path / 'root'
        target = root / 'empty'
        root.mkdir()
        target.mkdir()

        misc.prune_empty_parent_directories(target, root)

        assert not target.exists()
        assert root.exists()

    def test_removes_nested_empty_directories(self, tmp_path):
        """Recursively removes empty parent directories up to root."""
        root = tmp_path / 'root'
        nested = root / 'a' / 'b' / 'c'
        nested.mkdir(parents=True)

        misc.prune_empty_parent_directories(nested, root)

        assert not (root / 'a').exists()
        assert root.exists()

    def test_stops_at_non_empty_parent(self, tmp_path):
        """Stops removing when a parent directory contains other files."""
        root = tmp_path / 'root'
        branch_a = root / 'parent' / 'empty'
        branch_b = root / 'parent' / 'sibling.txt'
        branch_a.mkdir(parents=True)
        branch_b.write_text('content')

        misc.prune_empty_parent_directories(branch_a, root)

        assert not branch_a.exists()
        assert (root / 'parent').exists()  # parent not removed (has sibling)
        assert branch_b.exists()

    def test_does_not_remove_non_empty_target(self, tmp_path):
        """Target directory is not removed if it contains files."""
        root = tmp_path / 'root'
        target = root / 'not_empty'
        target.mkdir(parents=True)
        (target / 'file.txt').write_text('content')

        misc.prune_empty_parent_directories(target, root)

        assert target.exists()
        assert (target / 'file.txt').exists()

    def test_raises_for_missing_target(self, tmp_path):
        """Raises FileNotFoundError if target directory does not exist."""
        root = tmp_path / 'root'
        root.mkdir()
        missing = root / 'missing'

        with pytest.raises(FileNotFoundError, match='does not exist'):
            misc.prune_empty_parent_directories(missing, root)

    def test_raises_for_missing_root(self, tmp_path):
        """Raises FileNotFoundError if root directory does not exist."""
        target = tmp_path / 'target'
        target.mkdir()
        missing_root = tmp_path / 'missing_root'

        with pytest.raises(FileNotFoundError, match='does not exist'):
            misc.prune_empty_parent_directories(target, missing_root)

    def test_raises_for_file_target(self, tmp_path):
        """Raises NotADirectoryError if target is a file."""
        root = tmp_path / 'root'
        root.mkdir()
        file_target = root / 'file.txt'
        file_target.write_text('content')

        with pytest.raises(NotADirectoryError, match='is not a directory'):
            misc.prune_empty_parent_directories(file_target, root)

    def test_raises_for_file_root(self, tmp_path):
        """Raises NotADirectoryError if root is a file."""
        file_root = tmp_path / 'file.txt'
        file_root.write_text('content')
        target = tmp_path / 'target'
        target.mkdir()

        with pytest.raises(NotADirectoryError, match='is not a directory'):
            misc.prune_empty_parent_directories(target, file_root)

    def test_raises_for_non_subpath(self, tmp_path):
        """Raises ValueError if target is not a subpath of root."""
        root = tmp_path / 'root'
        other = tmp_path / 'other'
        root.mkdir()
        other.mkdir()

        with pytest.raises(ValueError, match='is not a sub-directory'):
            misc.prune_empty_parent_directories(other, root)

    def test_accepts_string_paths(self, tmp_path):
        """Works with string paths, not just Path objects."""
        root = tmp_path / 'root'
        target = root / 'empty'
        root.mkdir()
        target.mkdir()

        misc.prune_empty_parent_directories(str(target), str(root))

        assert not target.exists()
        assert root.exists()

    @pytest.mark.parametrize('error_class', ([PermissionError, FileNotFoundError]))
    def test_handles_errors_gracefully(self, tmp_path, mocker, error_class):
        """Returns silently when rmdir raises OSError."""
        root = tmp_path / 'root'
        target = root / 'problematic'
        root.mkdir()
        target.mkdir()

        mocker.patch.object(target.__class__, 'rmdir', side_effect=error_class())

        misc.prune_empty_parent_directories(target, root)
        assert target.exists()

    def test_remove_root_true_removes_empty_root(self, tmp_path):
        """With remove_root=True, empty root directory is removed."""
        root = tmp_path / 'root'
        nested = root / 'a' / 'b' / 'c'
        nested.mkdir(parents=True)

        misc.prune_empty_parent_directories(nested, root, remove_root=True)

        assert not root.exists()

    def test_remove_root_true_preserves_non_empty_root(self, tmp_path):
        """With remove_root=True, non-empty root is still preserved."""
        root = tmp_path / 'root'
        target = root / 'empty'
        sibling = root / 'sibling.txt'
        target.mkdir(parents=True)
        sibling.write_text('content')

        misc.prune_empty_parent_directories(target, root, remove_root=True)

        assert not target.exists()
        assert root.exists()
        assert sibling.exists()

    @pytest.mark.parametrize('error_class', ([PermissionError, FileNotFoundError]))
    def test_remove_root_handles_errors_gracefully(self, tmp_path, mocker, error_class):
        """With remove_root=True, OSError on root removal is handled gracefully."""
        root = tmp_path / 'root'
        root.mkdir()

        mocker.patch.object(root.__class__, 'rmdir', side_effect=error_class())

        misc.prune_empty_parent_directories(root, root, remove_root=True)
        assert root.exists()

    def test_handles_race_condition(self, tmp_path, mocker):
        """Returns silently if parent is deleted between rmdir and recursion."""
        root = tmp_path / 'root'
        parent = root / 'parent'
        target = parent / 'child'
        target.mkdir(parents=True)

        original_is_dir = type(target).is_dir

        def simulate_race(path):
            # Simulate race: parent deleted by another process after target removed
            if path == parent and not target.exists():
                return False
            return original_is_dir(path)

        mocker.patch.object(type(target), 'is_dir', simulate_race)

        misc.prune_empty_parent_directories(target, root)
        assert not target.exists()


class TestLRUCache:
    """Tests for LRUCache."""

    def test_set_and_get(self) -> None:
        """Stores and retrieves a value by key."""
        cache: LRUCache[str, int] = LRUCache(maxsize=2)
        cache['a'] = 1
        assert cache['a'] == 1

    def test_evicts_least_recently_used(self) -> None:
        """Evicts the oldest entry when maxsize is exceeded."""
        cache: LRUCache[str, int] = LRUCache(maxsize=2)
        cache['a'] = 1
        cache['b'] = 2
        cache['c'] = 3  # 'a' should be evicted
        assert 'a' not in cache
        assert 'b' in cache
        assert 'c' in cache

    def test_access_updates_recency(self) -> None:
        """Reading a key promotes it to most recently used."""
        cache: LRUCache[str, int] = LRUCache(maxsize=2)
        cache['a'] = 1
        cache['b'] = 2
        _ = cache['a']  # 'a' is now most recently used; 'b' is LRU
        cache['c'] = 3  # 'b' should be evicted, not 'a'
        assert 'a' in cache
        assert 'b' not in cache
        assert 'c' in cache

    def test_get_updates_recency(self) -> None:
        """Reading a key through the get method promotes it to most recently used."""
        cache: LRUCache[str, int] = LRUCache(maxsize=2)
        cache['a'] = 1
        cache['b'] = 2
        cache.get('a')  # 'a' is now most recently used, 'c' is LRU
        cache['c'] = 4  # 'b' should be evicted, not 'a'
        assert 'a' in cache
        assert 'b' not in cache
        assert 'c' in cache

    def test_update_existing_key_updates_recency(self) -> None:
        """Overwriting an existing key promotes it to most recently used."""
        cache: LRUCache[str, int] = LRUCache(maxsize=2)
        cache['a'] = 1
        cache['b'] = 2
        cache['a'] = 99  # 'a' updated; 'b' becomes LRU
        cache['c'] = 3  # 'b' should be evicted
        assert cache['a'] == 99
        assert 'b' not in cache
        assert 'c' in cache

    def test_len_does_not_exceed_maxsize(self) -> None:
        """Length never exceeds maxsize after many insertions."""
        cache: LRUCache[int, int] = LRUCache(maxsize=3)
        for i in range(10):
            cache[i] = i
        assert len(cache) == 3

    def test_default_maxsize(self) -> None:
        """Default maxsize is 128."""
        assert LRUCache().maxsize == 128

    def test_maxsize_property(self) -> None:
        """maxsize property reflects the value passed at construction."""
        assert LRUCache(maxsize=64).maxsize == 64

    def test_negative_maxsize_raises(self) -> None:
        """Raises ValueError for a negative maxsize."""
        with pytest.raises(ValueError, match='maxsize'):
            LRUCache(maxsize=-1)

    def test_get_returns_value(self) -> None:
        """get returns the value for an existing key."""
        cache: LRUCache[str, int] = LRUCache(maxsize=2)
        cache['a'] = 1
        assert cache.get('a') == 1

    def test_get_returns_none_for_missing_key(self) -> None:
        """get returns None by default for a missing key."""
        cache: LRUCache[str, int] = LRUCache(maxsize=2)
        assert cache.get('missing') is None

    def test_get_returns_default_for_missing_key(self) -> None:
        """get returns the provided default for a missing key."""
        cache: LRUCache[str, int] = LRUCache(maxsize=2)
        assert cache.get('missing', 99) == 99

    def test_setdefault_returns_existing_value(self) -> None:
        """setdefault returns the existing value without overwriting it."""
        cache: LRUCache[str, int] = LRUCache(maxsize=2)
        cache['a'] = 1
        assert cache.setdefault('a', 99) == 1
        assert cache['a'] == 1

    def test_setdefault_inserts_and_returns_default(self) -> None:
        """setdefault inserts and returns the default for a missing key."""
        cache: LRUCache[str, int] = LRUCache(maxsize=2)
        assert cache.setdefault('a', 99) == 99
        assert cache['a'] == 99


class TestDocstringInheritanceMixin:
    """Tests for DocstringInheritanceMixin."""

    def test_inherits_method_docstring(self):
        """Subclass method without docstring inherits from parent."""

        class Base(misc.DocstringInheritanceMixin):
            def foo(self):
                """Base docstring."""

        class Child(Base):
            def foo(self):
                pass

        assert Child.foo.__doc__ == 'Base docstring.'

    def test_does_not_overwrite_existing_docstring(self):
        """Subclass method with its own docstring is not overwritten."""

        class Base(misc.DocstringInheritanceMixin):
            def foo(self):
                """Base docstring."""

        class Child(Base):
            def foo(self):
                """Child docstring."""

        assert Child.foo.__doc__ == 'Child docstring.'

    def test_inherits_property_docstring(self):
        """Subclass property without docstring inherits from parent."""

        class Base(misc.DocstringInheritanceMixin):
            @property
            def bar(self):
                """Base property docstring."""
                return 1

        class Child(Base):
            @property
            def bar(self):
                return 2

        assert Child.bar.__doc__ == 'Base property docstring.'

    def test_inherits_from_grandparent(self):
        """Docstring is inherited across multiple levels of inheritance."""

        class Base(misc.DocstringInheritanceMixin):
            def foo(self):
                """Base docstring."""

        class Middle(Base):
            pass

        class Child(Middle):
            def foo(self):
                pass

        assert Child.foo.__doc__ == 'Base docstring.'
