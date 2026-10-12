"""
Helpers for building PyQt-based GUIs around bpod-core.

While bpod-core does neither require PyQt nor provide an actual GUI, this module
provides some tools to make it easier to integrate bpod-core into existing Qt-based
GUIs. This module builds on the `qtpy <https://github.com/spyder-ide/qtpy>`_ abstraction
layer to make it compatible with the various Qt bindings available in the Python
ecosystem: `PyQt5 <https://www.riverbankcomputing.com/static/Docs/PyQt5/>`_,
`PySide2 <https://doc.qt.io/archives/qtforpython-5>`_,
`PyQt6 <https://www.riverbankcomputing.com/static/Docs/PyQt6/>`_ and
`PySide6 <https://doc.qt.io/qtforpython-6>`_.
"""

import logging
import sys
from os import PathLike
from pathlib import Path

from qtpy.QtCore import (
    Property,
    QCoreApplication,
    QObject,
    QProcess,
    QProcessEnvironment,
    Signal,
    Slot,
)
from serial.tools.list_ports import comports

from bpod_core.bpod import (
    Bpod,
    BpodError,
    RemoteBpod,
)
from bpod_core.bpod.structs import (
    BpodEventUnion,
    EventInput,
    EventOutput,
    EventStateEnd,
    EventStateStart,
    EventTrialEnd,
    EventTrialEndControl,
    EventTrialStart,
)
from bpod_core.com import find_ports

logger = logging.getLogger(__name__)


class QBpod(QObject):
    """Qt adapter exposing a Bpod and its live trial events as signals/properties."""

    serialNumberChanged = Signal(str, arguments=['serial_number'])
    """
    Qt :class:`~PySide6.QtCore.Signal` carrying the Bpod's serial number.

    Attributes
    ----------
    serial_number : str
        The Bpod's serial number.
    """

    portChanged = Signal(str, arguments=['port'])
    """
    Qt :class:`~PySide6.QtCore.Signal` carrying the Bpod's serial port.

    Attributes
    ----------
    port : str
        The Bpod's serial port.
    """

    controlAddressChanged = Signal(str, arguments=['control_address'])
    """
    Qt :class:`~PySide6.QtCore.Signal` carrying the Bpod's control channel address.

    Attributes
    ----------
    control_address : str
        The Bpod's control channel address.
    """

    eventsAddressChanged = Signal(str, arguments=['events_address'])
    """
    Qt :class:`~PySide6.QtCore.Signal` carrying the Bpod's events channel address.

    Attributes
    ----------
    events_address : str
        The Bpod's events channel address.
    """

    trialStart = Signal(
        'qulonglong',  # type: ignore[arg-type]
        int,
        str,
        arguments=['time_us', 'trial_number', 'fsm_hash'],
    )
    """
    Qt :class:`~PySide6.QtCore.Signal` emitted at the start of a trial.

    Attributes
    ----------
    time_us : int
        Bpod timestamp (microseconds since epoch, UTC).
    trial_number : int
        The trial number.
    fsm_hash : str
        The hexadecimal string representation of the state machine's
        :attr:`~bpod_core.fsm.StateMachine.hash`.
    """

    trialEnd = Signal(
        'qulonglong',  # type: ignore[arg-type]
        int,
        arguments=['time_us', 'trial'],
    )
    """
    Qt :class:`~PySide6.QtCore.Signal` emitted when a trial ends.

    Attributes
    ----------
    time_us : int
        Bpod timestamp (microseconds since epoch, UTC).
    trial : int
        The trial number.
    """

    trialEndControl = Signal(
        'qulonglong',  # type: ignore[arg-type]
        int,
        int,
        arguments=['time_us', 'trial', 'n_events'],
    )
    """
    Qt :class:`~PySide6.QtCore.Signal` emitted when a trial ends (control).

    Attributes
    ----------
    time_us : int
        Bpod timestamp (microseconds since epoch, UTC). This timestamp has a higher
        accuracy compared to the one of :attr:`trialEnd` and can be used for verifying
        the timing accuracy of the state machine run.
    trial : int
        The trial number.
    n_events : int
        Then number of events published for the trial. This can be used to verify if
        all events published on the best-effort PUB/SUB channel were received.
    """

    stateStart = Signal(
        'qulonglong',  # type: ignore[arg-type]
        str,
        arguments=['time_us', 'state_name'],
    )
    """
    Qt :class:`~PySide6.QtCore.Signal` emitted when a state starts.

    Attributes
    ----------
    time_us : int
        Bpod timestamp (microseconds since epoch, UTC).
    state_name : str
        The name of the state.
    """

    stateEnd = Signal(
        'qulonglong',  # type: ignore[arg-type]
        str,
        arguments=['time_us', 'state_name'],
    )
    """
    Qt :class:`~PySide6.QtCore.Signal` emitted when a state ends.

    Attributes
    ----------
    time_us : int
        Bpod timestamp (microseconds since epoch, UTC).
    state_name : str
        The name of the state.
    """

    inputEvent = Signal(
        'qulonglong',  # type: ignore[arg-type]
        str,
        str,
        int,
        arguments=['time_us', 'event', 'channel', 'value'],
    )
    """
    Qt :class:`~PySide6.QtCore.Signal` emitted when an input event occurs.

    Attributes
    ----------
    time_us : int
        Bpod timestamp (microseconds since epoch, UTC).
    event : str
        Name of the input event.
    channel : str
        Name of the input channel. Empty string if not applicable.
    value : int
        The event's value. -1 if not applicable.
    """

    outputAction = Signal(
        'qulonglong',  # type: ignore[arg-type]
        str,
        int,
        arguments=['time_us', 'channel', 'value'],
    )
    """
    Qt :class:`~PySide6.QtCore.Signal` emitted when an output action was performed.

    Attributes
    ----------
    time_us : int
        Bpod timestamp (microseconds since epoch, UTC).
    channel : str
        Name of the output channel.
    value : int
        The action's value.
    """

    _is_connected: bool = None
    _local_bpod: Bpod | None = None
    _remote_bpod: RemoteBpod

    _serial_number: str | None = None
    _port: str | None = None
    _control_address: str
    _events_address: str

    process: QProcess
    """Used to launch python scripts."""

    def __init__(self, serial_number: str | None = None) -> None:
        super().__init__()

        # First, try to connect to the Bpod directly via USB
        try:
            self._local_bpod = Bpod(serial_number=serial_number)
        except BpodError:
            self._local_bpod = None
        else:
            self._serial_number = self._local_bpod.serial_number
            self.serialNumberChanged.emit(self._serial_number)
            self._remote_bpod = RemoteBpod(
                serial_number=self._serial_number, event_callback=self._on_bpod_event
            )
            self._local_bpod._zmq.close()  # noqa: SLF001

        # discover_remote_bpod(serial_number=serial_number, remote=False)
        #
        # self._bpod = Bpod(serial_number=serial_number)
        # self._serial_number = self._bpod.serial_number
        # self._remote_bpod = RemoteBpod(serial_number=serial_number)
        # self._bpod._zmq.close()

        # Prepare the QProcess instance
        env = QProcessEnvironment.systemEnvironment()
        env.insert('BPOD_OVERRIDE_SERIAL_NUMBER', self._serial_number)
        env.insert('BPOD_OVERRIDE_REMOTE', '1')
        self.process = QProcess(self)
        self.process.setProcessChannelMode(
            QProcess.ProcessChannelMode.ForwardedChannels
        )
        self.process.setProcessEnvironment(env)

        # connect signals related to the QProcess instance
        self.process.finished.connect(self._on_subprocess_finished)

        # make sure the QProcess instance is destroyed when the QBpod instance is closed
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.close)

    def open(self, identifier: str) -> None:
        """
        Open a Bpod with a given serial port, serial number or control address.

        Parameters
        ----------
        identifier : str
            port, serial number, or control address of the Bpod device to open.
        """
        # first check if the identifier corresponds to a device connected via USB
        port_info = next(
            iter(p for p in comports() if identifier in (p.device, p.serial_number)),
            None,
        )

        # if so, try to connect to it directly
        if port_info is not None:
            self._serial_number = port_info.serial_number
            self._port = port_info.device
            self.close()
            try:
                local_bpod = Bpod(port=port_info.device)
            except BpodError:
                self._local_bpod = None
            else:
                self._local_bpod = local_bpod
                control_address = local_bpod.address_control
                events_address = local_bpod.address_events

        self._control_address = control_address
        self._events_address = events_address

        # connect to the remote Bpod
        self._remote_bpod = RemoteBpod(
            address=self._control_address, event_callback=self._on_bpod_event
        )

        self.serialNumberChanged.emit(self._serial_number)
        self.portChanged.emit(self._port)
        self.controlAddressChanged.emit(self._remote_bpod.address_control)
        self.eventsAddressChanged.emit(self._remote_bpod.address_events)

    def close(self) -> None:
        """Disconnect from the Bpod and close the QProcess instance."""
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self._remote_bpod.stop_state_machine()
            self._remote_bpod.close()
            self.process.finished.disconnect(self._on_subprocess_finished)
            self.process.terminate()
            self.process.waitForFinished(1000)
            self.process.close()
        if self._local_bpod is not None:
            self._local_bpod.stop_state_machine()
            self._local_bpod.close()
            self._local_bpod = None

    @Slot(str)
    def launchProcess(self, python_script: PathLike | str) -> None:
        """
        Launch the provided Python script in a separate process.

        This method will hand over control of a local Bpod to a python script launched
        in the :class:`~PySide6.QtCore.QProcess` :attr:`process`.

        Parameters
        ----------
        python_script : PathLike or str
            The path to the Python script to be executed.

        Raises
        ------
        RuntimeError
            If :attr:`process` is in a running state already or :class:`QBpod` does not
            own a local :class:`~bpod_core.bpod.Bpod` instance.
        OSError
            If the Python script does not exist or a symlink loop is encountered.
        ValueError
            If the provided path is not a Python script.

        Notes
        -----
            Python scripts can only be executed if :class:`QBpod` owns a local
            :class:`~bpod_core.bpod.Bpod` instance with direct serial access to the
            device. Trying to launch a Python script with a remote-controlled Bpod will
            result in a :class:`RuntimeError`.
        """
        if self._local_bpod is None:
            raise RuntimeError(
                'This function can only be called from a local Bpod instance.'
            )
        if self.process.state() != QProcess.ProcessState.NotRunning:
            raise RuntimeError('Cannot launch a new process while the Bpod is running.')
        script_path = Path(python_script).resolve(strict=True)
        if script_path.suffix != '.py':
            raise ValueError(f'Invalid file type: {script_path}')

        self._local_bpod.close()
        self._local_bpod._zmq.close()  # noqa: SLF001
        self.process.start(sys.executable, [str(script_path)])
        if not self.process.waitForStarted():
            raise RuntimeError(self.process.errorString())

    @Slot(str)
    def runStateMachine(self, state_machine_file: PathLike | str) -> None:
        pass

    def _on_subprocess_finished(
        self, _exit_code: int, _exit_status: QProcess.ExitStatus
    ) -> None:
        if self._local_bpod is not None:
            self._local_bpod.open()

    def _on_bpod_event(self, message: BpodEventUnion) -> None:
        """Translate a live trial-event message into the corresponding Qt signal.

        Runs on the SUB event-loop thread of the underlying :class:`RemoteBpod`.
        Cross-thread signal emission is safe - Qt queues it into the receivers'
        event loops - but nothing else is, so this method must only emit and
        return.
        """
        match message:
            case EventInput(time_us=t_us, event=evt, channel=ch, value=val):
                self.inputEvent.emit(t_us, evt, ch or '', -1 if val is None else val)
            case EventOutput(time_us=t_us, channel=ch, value=val):
                self.outputAction.emit(t_us, ch, val)
            case EventStateStart(time_us=t_us, state=state):
                self.stateStart.emit(t_us, state)
            case EventStateEnd(time_us=t_us, state=state):
                self.stateEnd.emit(t_us, state)
            case EventTrialStart(time_us=t_us, trial=trial, fsm_hash=fsm_hash):
                self.trialStart.emit(t_us, trial, fsm_hash)
            case EventTrialEnd(time_us=t_us, trial=trial):
                self.trialEnd.emit(t_us, trial)
            case EventTrialEndControl(time_us=t_us, trial=trial, n_events=n_events):
                self.trialEndControl.emit(t_us, trial, n_events)

    @Property(str, notify=serialNumberChanged)
    def serialNumber(self) -> str:
        """The serial number of the Bpod."""
        return self._serial_number

    @Property(str, notify=portChanged)
    def port(self) -> str:
        """The serial number of the Bpod."""
        return self._port

    @Property(str, notify=controlAddressChanged)
    def controlAddress(self) -> str:
        """The address of the control channel."""
        return self._remote_bpod.address_control

    @Property(str, notify=eventsAddressChanged)
    def eventsAddress(self) -> str:
        """The address of the events channel."""
        return self._remote_bpod.address_events
