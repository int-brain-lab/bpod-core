"""Abstract base classes used by the bpod module."""

from abc import abstractmethod
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from functools import cached_property
from typing import Literal, overload

import msgspec
import polars as pl
from pydantic import validate_call
from typing_extensions import override

from bpod_core.bpod.constants import (
    CHANNEL_TYPES_OUTPUT,
    FlexIOChannelType,
    FlexIOThresholdMode,
    FlexIOThresholdPolarity,
    FlexIOThresholdVoltage,
)
from bpod_core.bpod.structs import (
    HardwareConfiguration,
    HardwareState,
    VersionInfo,
    _FlexIOState,
)
from bpod_core.fsm import StateMachine
from bpod_core.misc import suggest_similar


class AbstractBpod(AbstractContextManager):
    """Abstract base class for Bpod objects."""

    _version: VersionInfo
    _hardware: HardwareConfiguration
    _state: HardwareState
    _serial_number: str
    _flex_io: 'AbstractFlexIO | None' = None

    @property
    def flex_io(self) -> 'AbstractFlexIO':
        """The FlexIO subsystem."""
        if self._flex_io is None:
            if self._hardware.n_flexio == 0:
                raise RuntimeError(
                    f'Bpod {self._version.machine_str} does not have FlexIO channels'
                )
            raise RuntimeError('FlexIO subsystem not available')
        return self._flex_io

    @property
    @abstractmethod
    def name(self) -> str | None:
        """The Bpod's user-defined name, or :obj:`None` if not set."""

    @property
    @abstractmethod
    def location(self) -> str | None:
        """The Bpod's user-defined location, or :obj:`None` if not set."""

    @property
    @abstractmethod
    def address_control(self) -> str:
        """The ZeroMQ address of the control channel."""

    @property
    @abstractmethod
    def address_events(self) -> str:
        """The ZeroMQ address of the events channel."""

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


@dataclass(slots=True)
class FlexIOThreshold:
    """Class representing a FlexIO analog threshold."""

    _state_getter: Callable[[], _FlexIOState] = field(repr=False)
    _state_setter: Callable[[_FlexIOState], None] = field(repr=False)
    _channel_index: int = field(repr=False)
    _threshold_index: int = field(repr=False)

    @property
    def voltage(self) -> FlexIOThresholdVoltage:
        """The voltage of the threshold."""
        state = self._state_getter()
        return state.threshold_voltages[self._channel_index][self._threshold_index]

    @voltage.setter
    @validate_call
    def voltage(self, value: FlexIOThresholdVoltage) -> None:
        state = self._state_getter()
        voltages = [list(v) for v in state.threshold_voltages]
        voltages[self._channel_index][self._threshold_index] = value
        new_state = msgspec.structs.replace(state, threshold_voltages=voltages)
        self._state_setter(new_state)

    @property
    def polarity(self) -> FlexIOThresholdPolarity:
        """The polarity of the threshold."""
        state = self._state_getter()
        return state.threshold_polarities[self._channel_index][self._threshold_index]

    @polarity.setter
    @validate_call
    def polarity(self, value: FlexIOThresholdPolarity) -> None:
        state = self._state_getter()
        polarities = [list(p) for p in state.threshold_polarities]
        polarities[self._channel_index][self._threshold_index] = value
        new_state = msgspec.structs.replace(state, threshold_polarities=polarities)
        self._state_setter(new_state)

    @property
    def enabled(self) -> bool:
        """Whether the threshold is enabled."""
        state = self._state_getter()
        return state.threshold_enabled[self._channel_index][self._threshold_index]

    @enabled.setter
    @validate_call
    def enabled(self, value: bool) -> None:
        state = self._state_getter()
        enabled = [list(e) for e in state.threshold_enabled]
        enabled[self._channel_index][self._threshold_index] = value
        new_state = msgspec.structs.replace(state, threshold_enabled=enabled)
        self._state_setter(new_state)


@dataclass()
class FlexIOChannel:
    """Class representing a single FlexIO channel."""

    _state_getter: Callable[[], _FlexIOState] = field(repr=False)
    _state_setter: Callable[[_FlexIOState], None] = field(repr=False)
    _index: int = field(repr=False)
    _thresholds: tuple[FlexIOThreshold, FlexIOThreshold] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._thresholds = (
            FlexIOThreshold(self._state_getter, self._state_setter, self._index, 0),
            FlexIOThreshold(self._state_getter, self._state_setter, self._index, 1),
        )

    @cached_property
    def name(self) -> str:
        """Name of the FlexIO channel."""
        return f'{CHANNEL_TYPES_OUTPUT[b"F"]}{self._index + 1}'

    @property
    def channel_type(self) -> FlexIOChannelType:
        """The type of the FlexIO channel."""
        return self._state_getter().channel_types[self._index]

    @channel_type.setter
    @validate_call
    def channel_type(self, value: FlexIOChannelType) -> None:
        state = self._state_getter()
        new_value = list(state.channel_types)
        new_value[self._index] = value
        new_state = msgspec.structs.replace(state, channel_types=new_value)
        self._state_setter(new_state)

    @property
    def threshold_mode(self) -> FlexIOThresholdMode:
        """The analog threshold mode of the FlexIO channel."""
        return self._state_getter().threshold_modes[self._index]

    @threshold_mode.setter
    @validate_call
    def threshold_mode(self, value: FlexIOThresholdMode) -> None:
        state = self._state_getter()
        new_value = list(state.threshold_modes)
        new_value[self._index] = value
        new_state = msgspec.structs.replace(state, threshold_modes=new_value)
        self._state_setter(new_state)

    @property
    def thresholds(self) -> tuple[FlexIOThreshold, FlexIOThreshold]:
        """The analog thresholds of the FlexIO channel."""
        return self._thresholds


class AbstractFlexIO(Mapping[str, FlexIOChannel]):
    """Abstract base for FlexIO subsystems."""

    __slots__ = ('_on_channel_types_changed', '_state', '_view')

    _view: dict[str, FlexIOChannel]
    _state: _FlexIOState

    def __init__(
        self,
        n: int,
        *,
        on_channel_types_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()

        self._state = _FlexIOState.create_default(n_channels=n)
        self._on_channel_types_changed = on_channel_types_changed
        self._view = {}
        for i in range(n):
            channel = FlexIOChannel(self._get_state, self._apply_settings, i)
            self._view[channel.name] = channel

    def _get_state(self) -> _FlexIOState:
        return self._state

    @override
    def __getitem__(self, item: str) -> FlexIOChannel:
        try:
            return self._view[item]
        except KeyError as e:
            raise KeyError(
                f"No such FlexIO channel: '{item}'"
                + suggest_similar(item, self._view.keys())
            ) from e

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(self._view)

    @override
    def __len__(self) -> int:
        return len(self._view)

    @abstractmethod
    def _apply_settings(self, state: _FlexIOState) -> None: ...

    @abstractmethod
    def reset(self) -> None:
        """Reset the FlexIO subsystem to its default settings."""
        ...
