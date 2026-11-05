"""Exceptions raised by the bpod module."""

import contextlib
from collections.abc import Iterator

from bpod_core.com import ExtendedSerial, PortBusyError


class BpodError(Exception):
    """Raised for errors specific to Bpod device operations."""


class BpodKeyError(BpodError, KeyError):
    """Exception class for Bpod-related key errors."""


@contextlib.contextmanager
def owning_replies(
    serial: ExtendedSerial, action: str, hint: str = ''
) -> Iterator[None]:
    """Own `serial`'s replies for a conversation, or refuse `action` with a BpodError.

    See :meth:`~bpod_core.com.ExtendedSerial.exclusive_reads`: a running trial's
    reader or another thread's command may own them; the refusal says which, followed
    by `hint`.
    """
    try:
        with serial.exclusive_reads():
            yield
    except PortBusyError as e:
        holder = (
            'a state machine is running'
            if serial._read_owner is None  # noqa: SLF001 - the port's protocol
            else 'another thread is using the Bpod'
        )
        raise BpodError(f'Cannot {action} while {holder}.{hint}') from e
