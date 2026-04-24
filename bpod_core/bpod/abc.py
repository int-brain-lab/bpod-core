"""Abstract base classes used by the bpod module."""

from abc import ABC, abstractmethod

from bpod_core.bpod.structs import HardwareConfiguration, VersionInfo


class AbstractBpod(ABC):
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
