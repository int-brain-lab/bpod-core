import ctypes
import logging
import struct
import threading
from collections.abc import Iterable, Sequence
from typing import Any, overload

import numpy as np
import serial
from serial.serialutil import to_bytes as serial_to_bytes  # type: ignore[attr-defined]
from serial.tools import list_ports

logger = logging.getLogger(__name__)


class SerialSingletonException(serial.SerialException):
    pass


class ExtendedSerial(serial.Serial):
    def write_struct(self, data: Sequence[Any], format_string: str) -> int | None:
        """
        Write structured data to the serial port.

        This method packs the provided data into a binary format according to the
        specified format string and writes it to the serial port.

        Parameters
        ----------
        data : Sequence[Any]
            A sequence of data to be packed and written. The length of this sequence
            must match the number of format specifiers in `format_string`.

        format_string : str
            A format string that specifies the layout of the data. It should be
            compatible with the `struct` module's format specifications.
            See https://docs.python.org/3/library/struct.html#format-characters

        Returns
        -------
        int | None
            The number of bytes written to the serial port, or None if the write
            operation fails.
        """
        size = struct.calcsize(format_string)
        buff = ctypes.create_string_buffer(size)
        struct.pack_into(format_string, buff, 0, *data)
        return super().write(buff)

    def read_struct(self, format_string: str) -> tuple[Any, ...]:
        """
        Read structured data from the serial port.

        This method reads a specified number of bytes from the serial port and
        unpacks it into a tuple according to the provided format string.

        Parameters
        ----------
        format_string : str
            A format string that specifies the layout of the data to be read. It should
            be compatible with the `struct` module's format specifications.
            See https://docs.python.org/3/library/struct.html#format-characters

        Returns
        -------
        tuple[Any, ...]
            A tuple containing the unpacked data read from the serial port. The
            structure of the tuple corresponds to the format specified in
            `format_string`.
        """
        n_bytes = struct.calcsize(format_string)
        return struct.unpack(format_string, super().read(n_bytes))

    def write(self, data: tuple[Sequence[Any], str] | Any) -> int | None:
        """
        Write data to the Bpod.

        Parameters
        ----------
        data : any
            Data to be written to the Bpod.
            See https://docs.python.org/3/library/struct.html#format-characters

        Returns
        -------
        int or None
            Number of bytes written to the Bpod.
        """
        if isinstance(data, tuple()):
            return self.write_struct(data=data[0], format_string=data[1])
        else:
            return super().write(to_bytes(data))

    @overload
    def read(self, data_specifier: int = 1) -> bytes: ...

    @overload
    def read(self, data_specifier: str) -> tuple[Any, ...]: ...

    def read(self, data_specifier=1):
        r"""
        Read data from the Bpod.

        Parameters
        ----------
        data_specifier : int or str, default: 1
            The number of bytes to receive from the serial device, or a format string
            for unpacking.

            When providing an integer, the specified number of bytes will be returned
            as a bytestring. When providing a `format string`_, the data will be
            unpacked into a tuple accordingly. Format strings follow the conventions of
            the :mod:`struct` module.

            .. _format string:
                https://docs.python.org/3/library/struct.html#format-characters

        Returns
        -------
        bytes or tuple[Any]
            Data returned by the serial device. By default, data is formatted as a
            bytestring. Alternatively, when provided with a format string, data will
            be unpacked into a tuple according to the specified format string.
        """
        if isinstance(data_specifier, str):
            return self.read_struct(format_string=data_specifier)
        else:
            return super().read(size=data_specifier)

    @overload
    def query(self, query: bytes | Sequence[Any], data_specifier: int = 1) -> bytes: ...

    @overload
    def query(
        self, query: bytes | Sequence[Any], data_specifier: str
    ) -> tuple[Any, ...]: ...

    def query(self, query, data_specifier=1):
        r"""
        Query data from the Bpod.

        This method is a combination of :py:meth:`write` and :py:meth:`read`.

        Parameters
        ----------
        query : any
            Query to be sent to the Bpod.
        data_specifier : int or str, default: 1
            The number of bytes to receive from the Bpod, or a format string for
            unpacking.

            When providing an integer, the specified number of bytes will be returned
            as a bytestring. When providing a `format string`_, the data will be
            unpacked into a tuple accordingly. Format strings follow the conventions of
            the :py:mod:`struct` module.

            .. _format string:
                https://docs.python.org/3/library/struct.html#format-characters

        Returns
        -------
        bytes or tuple[Any]
            Data returned by the Bpod. By default, data is formatted as a bytestring.
            Alternatively, when provided with a format string, data will be unpacked
            into a tuple according to the specified format string.


        Examples
        --------
        Query 4 bytes of data from a Bpod device - first interpreted as a bytestring,
        then as a tuple of two unsigned short integers:

        .. code-block:: python
            :emphasize-lines: 2

            my_bpod.query(b"F", 4)
            b'\\x16\\x00\\x03\\x00'
            my_bpod.query(b"F", '2H')
            (22, 3)
        """
        self.write(query)
        return self.read(data_specifier)


class SerialSingleton(ExtendedSerial):
    _instances: dict[str | None, serial.Serial] = dict()
    _initialized = False
    _lock = threading.Lock()

    def __new__(
        cls,
        port: str | None = None,
        serial_number: str | None = None,
        *args,
        **kwargs,
    ):
        # identify the device by its serial number
        if port is None and serial_number is not None:
            port = get_port_from_serial_number(serial_number) or port

        # implement singleton
        with cls._lock:
            instance = SerialSingleton._instances.get(port, None)
            if instance is None:
                logger.debug(f'Creating new {cls.__name__} instance on {port}')
                instance = super().__new__(cls)
                SerialSingleton._instances[port] = instance
            else:
                instance_name = type(instance).__name__
                if instance_name != cls.__name__:
                    raise SerialSingletonException(
                        f'{port} is already in use by an instance of {instance_name}'
                    )
                logger.debug(f'Using existing {instance_name} instance on {port}')
            return instance

    def __init__(self, port: str | None = None, connect: bool = True, **kwargs) -> None:
        if self._initialized:
            return

        super().__init__(**kwargs)

        serial.Serial.port.fset(self, port)  # type: ignore[attr-defined]
        if port is not None and connect is True:
            self.open()

        self.port_info = next(
            (p for p in list_ports.comports() if p.device == self.port), None
        )

        self._initialized = True

    def __del__(self) -> None:
        self.close()
        with self._lock:
            if self.port in SerialSingleton._instances:
                logger.debug(f'Deleting {type(self).__name__} instance on {self.port}')
                SerialSingleton._instances.pop(self.port)

    def open(self) -> None:
        super().open()
        logger.debug(f'Serial connection to {self.port} opened')

    def close(self) -> None:
        super().close()
        logger.debug(f'Serial connection to {self.port} closed')

    @property
    def port(self) -> str | None:
        """
        Get the serial device's communication port.

        Returns
        -------
        str
            The serial port (e.g., 'COM3', '/dev/ttyUSB0') used by the serial device.
        """
        return super().port

    @port.setter
    def port(self, port: str | None):
        """
        Set the serial device's communication port.

        This setter allows changing the communication port before the object is
        instantiated. Once the object is instantiated, attempting to change the port
        will raise a SerialSingletonException.

        Parameters
        ----------
        port : str
            The new communication port to be set (e.g., 'COM3', '/dev/ttyUSB0').

        Raises
        ------
        SerialSingletonException
            If an attempt is made to change the port after the object has been
            instantiated.
        """
        if self._initialized:
            raise SerialSingletonException(
                'Port cannot be changed after instantiation.'
            )
        if port is not None:
            serial.Serial.port.fset(self, port)  # type: ignore[attr-defined]


def to_bytes(data: Any) -> bytes:
    """
    Convert data to bytestring.

    This method extends :meth:`serial.to_bytes` with support for NumPy types,
    unsigned 8-bit integers, strings (interpreted as utf-8) and iterables.

    Parameters
    ----------
    data : any
        Data to be converted to bytestring.

    Returns
    -------
    bytes
        Data converted to bytestring.
    """
    match data:
        case bytes():
            return data
        case int():
            return bytes([data])
        case np.ndarray() | np.generic():
            return data.tobytes()
        case str():
            return data.encode('utf-8')
        case _ if isinstance(data, Iterable):
            return b''.join(to_bytes(item) for item in data)
        case _:
            return serial_to_bytes(data)  # type: ignore[no-any-return]


def get_port_from_serial_number(serial_number: str) -> str | None:
    """
    Retrieve the com port of a USB serial device identified by its serial number.

    Parameters
    ----------
    serial_number : str
       The serial number of the USB device that you want to obtain the communication
       port of.

    Returns
    -------
    str or None
       The communication port of the USB serial device that matches the serial number
       provided by the user. The function will return None if no such device was found.
    """
    port_info = list_ports.comports()
    port_match = next((p for p in port_info if p.serial_number == serial_number), None)
    return port_match.name if port_match else None


def get_serial_number_from_port(port: str | None) -> str | None:
    """
    Retrieve the serial number of a USB serial device identified by its com port.

    Parameters
    ----------
    port : str
        The communication port of the USB serial device for which you want to retrieve
        the serial number.

    Returns
    -------
    str or None
        The serial number of the USB serial device corresponding to the provided
        communication port. Returns None if no device matches the port.
    """
    port_info = list_ports.comports()
    port_match = next((p for p in port_info if p.name == port), None)
    return port_match.serial_number if port_match else None
