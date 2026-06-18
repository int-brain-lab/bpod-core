"""Abstract base classes used by the bpod module."""

from abc import abstractmethod
from contextlib import AbstractContextManager
from typing import Literal, overload

import polars as pl

from bpod_core.bpod.structs import HardwareConfiguration, VersionInfo
from bpod_core.fsm import StateMachine


class AbstractBpod(AbstractContextManager):
    """Abstract base class for Bpod objects."""

    _version: VersionInfo
    _hardware: HardwareConfiguration
    _serial_number: str

    @property
    @abstractmethod
    def name(self) -> str | None:
        """The Bpod's user-defined name, or :obj:`None` if not set."""

    @property
    @abstractmethod
    def location(self) -> str | None:
        """The Bpod's user-defined location, or :obj:`None` if not set."""

    @property
    def version(self) -> VersionInfo:
        """Version information of the Bpod's firmware and hardware."""
        return self._version

    @property
    def serial_number(self) -> str:
        """The Bpod's unique serial number."""
        return self._serial_number

    @overload
    def get_data(
        self, *, concat: bool = ..., rechunk: bool = ..., lazy: Literal[False] = False
    ) -> pl.DataFrame: ...

    @overload
    def get_data(
        self, *, concat: bool = ..., rechunk: bool = ..., lazy: Literal[True]
    ) -> pl.LazyFrame: ...

    @abstractmethod
    def get_data(self, *, concat=True, rechunk=False, lazy=False):
        """Return trial data from the data queue.

        Parameters
        ----------
        concat : bool, default: True
            If ``True``, pop and concatenate all DataFrames currently in the queue into
            a single DataFrame, blocking until at least one is available.
            If ``False``, pop and return one DataFrame, blocking until one is
            available.
        rechunk : bool, default: False
            If ``True``, make sure that the result data is in contiguous memory. Only
            applies when ``concat=True``.
        lazy : bool, default: False
            If ``True``, return a :class:`polars.LazyFrame`.
            If ``False``, return a :class:`polars.DataFrame`.

        Returns
        -------
        DataFrame or LazyFrame
            One trial's data, or all available trials concatenated when ``concat=True``.

            Columns:

            - **time** (:class:`~polars.datatypes.Datetime`) – absolute Bpod timestamp.
            - **trial** (:class:`~polars.datatypes.UInt16`) – zero-based trial index.
            - **state machine** (:class:`~polars.datatypes.Categorical`) – state machine
              hash, see :meth:`~bpod_core.fsm.StateMachine.hash`.
            - **state** (:class:`~polars.datatypes.Categorical`) – state name.
            - **type** (:class:`~polars.datatypes.Enum`) – event type.
            - **event** (:class:`~polars.datatypes.Categorical`) – input event name.
            - **channel** (:class:`~polars.datatypes.Categorical`) – channel name.
            - **value** (:class:`~polars.datatypes.UInt8`) – channel value.

        Raises
        ------
        BpodError
            If the queue is empty and no state machine is currently running.
        """
        ...

    @abstractmethod
    def reset_session_clock(self) -> bool:
        """Reset the Bpod session clock.

        Returns
        -------
        bool
            True if the Bpod acknowledged the command.

        Raises
        ------
        BpodError
            When the method is called while a state machine is running.
        """

    @abstractmethod
    def run(
        self,
        state_machine: StateMachine | None = None,
        *,
        trial_number: int | None = None,
        validate: bool = True,
    ) -> None:
        """
        Run a state machine on the Bpod.

        Validates, compiles, sends, and queues a state machine for immediate execution.
        If the Bpod is currently running a state machine, the new one is queued to run
        as soon as the current one finishes, with no inter-trial gap.

        If called without an argument, the previously sent state machine is re-sent
        from the compilation cache and queued for immediate back-to-back execution.

        Parameters
        ----------
        state_machine : StateMachine, optional
            The state machine to run. If not provided, the previously sent state machine
            is repeated.
        trial_number : int, optional
            The trial number to assign to the state machine. If not provided, the trial
            number is automatically incremented with each run.
        validate : bool, default: True
            If False, the state machine will not be validated prior to compilation. This
            will speed up the process, but may result in errors or unexpected behavior
            if the state machine is invalid. Use with caution.

        Raises
        ------
        RuntimeError
            If called without an argument and no state machine has been run yet.
        ValueError
            If the state machine is invalid or exceeds hardware limitations.
        ValidationError
            If function arguments don't match type hints.

        Notes
        -----
        This method returns once the state machine has been queued on the Bpod. The Bpod
        will then begin executing the state machine as soon as possible — immediately
        if the device is idle or right after the current state machine finishes.
        Subsequent calls of this method will result in continuous acquisition and zero
        inter-trial downtime (as long as the Bpod's run queue stays filled).
        """

    @abstractmethod
    def set_status_led(self, enable: bool) -> bool:  # noqa: FBT001
        """
        Enable or disable the Bpod's status LED.

        Parameters
        ----------
        enable : bool
            True to enable the status LED, False to disable.

        Returns
        -------
        bool
            True if the operation was successful, False otherwise.
        """

    @abstractmethod
    def stop_state_machine(self) -> None:
        """Stop the currently running state machine."""

    @abstractmethod
    def update_modules(self) -> None:
        """Update the list of connected modules and their configurations."""
