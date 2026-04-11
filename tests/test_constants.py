import numpy as np
import pytest

from bpod_core import constants


@pytest.mark.parametrize(
    ('np_dtype', 'int_min'),
    [
        pytest.param(np.int8, constants.INT8_MIN, id='int8'),
        pytest.param(np.int16, constants.INT16_MIN, id='int16'),
        pytest.param(np.int32, constants.INT32_MIN, id='int32'),
        pytest.param(np.int64, constants.INT64_MIN, id='int64'),
    ],
)
def test_int_min(np_dtype, int_min):
    """Test minimum integer values."""
    assert np.iinfo(np_dtype).min == int_min


@pytest.mark.parametrize(
    ('np_dtype', 'int_max'),
    [
        pytest.param(np.int8, constants.INT8_MAX, id='int8'),
        pytest.param(np.int16, constants.INT16_MAX, id='int16'),
        pytest.param(np.int32, constants.INT32_MAX, id='int32'),
        pytest.param(np.int64, constants.INT64_MAX, id='int64'),
        pytest.param(np.uint8, constants.UINT8_MAX, id='uint8'),
        pytest.param(np.uint16, constants.UINT16_MAX, id='uint16'),
        pytest.param(np.uint32, constants.UINT32_MAX, id='uint32'),
        pytest.param(np.uint64, constants.UINT64_MAX, id='uint64'),
    ],
)
def test_int_max(np_dtype, int_max):
    """Test maximum integer values."""
    assert np.iinfo(np_dtype).max == int_max
