"""Threads for FSM execution and data collection from the Bpod hardware."""

import logging
import struct
import threading
import time
from collections.abc import Callable
from datetime import datetime

import numpy as np
import numpy.typing as npt

from bpod_core.bpod.structs import TimeReferences
from bpod_core.com import ExtendedSerial
from bpod_core.constants import STRUCT_UINT32_LE

logger = logging.getLogger(__name__)


class FSMThread(threading.Thread):
    """A thread for managing the execution of a finite state machine on the Bpod."""

    _struct_exit = struct.Struct('<IQ')

    def __init__(
        self,
        *,
        serial: ExtendedSerial,
        fsm_index: int,
        confirm_fsm: bool,
        cycle_period: int,
        softcode_handler: Callable,
        state_transitions: npt.NDArray[np.uint8],
        use_back_op: bool,
        event_names: list[str],
        time_reference: TimeReferences,
    ) -> None:
        """
        Initialize the FSMThread.

        Parameters
        ----------
        serial : ExtendedSerial
            The serial connection to the Bpod device.
        fsm_index : int
            The index of the FSM being managed.
        confirm_fsm : bool
            Whether to confirm the FSM with the Bpod device.
        cycle_period : int
            The cycle period of the Bpod device in microseconds.
        softcode_handler : Callable
            A handler function for processing softcodes.
        state_transitions : np.ndarray
            The state transition matrix.
        use_back_op : bool
            Whether the state machine makes use of the ``>back`` operator.
        event_names : list of str
            Names of all events the FSM can receive, used for logging.
        time_reference : TimeReferences
            Reference values for performance counters.
        """
        super().__init__(daemon=True)
        self.serial = serial
        self._stop_event = threading.Event()
        self._index = fsm_index
        self._confirm_fsm = confirm_fsm
        self._cycle_period = cycle_period
        self._softcode_handler = softcode_handler
        self._state_transitions = state_transitions
        self._use_back_op = use_back_op
        self._event_names = event_names
        self._time_reference = time_reference

    def stop(self) -> None:
        """Signal the FSM thread to stop after the current state cycle."""
        self._stop_event.set()

    def run(self) -> None:
        """Execute the FSMThread."""
        # confirm the state machine
        if self._confirm_fsm and not self.serial.read_bool():
            raise RuntimeError(f'State machine #{self._index} not confirmed by Bpod')

        # read the starting timestamps of the state machine
        # we do this early to get an accurate timestamp for the system clock
        bpod_count_us = self.serial.read_uint64()
        perf_count_ns = time.perf_counter_ns()

        # assign members to local variables to avoid repeated attribute lookups
        serial = self.serial
        index = self._index
        cycle_period = self._cycle_period
        softcode_handler = self._softcode_handler
        state_transitions = self._state_transitions
        previous_state = np.uint8(0)
        current_state = np.uint8(0)
        target_exit = np.uint8(state_transitions.shape[0])
        target_back = np.uint8(255)
        use_back_op = self._use_back_op
        event_names = self._event_names
        reference_time_ns, reference_count_ns, reset_time_ns = self._time_reference
        reset_time_us = reset_time_ns // 1000

        # create buffers / memoryview for repeated serial reads
        opcode_buf = bytearray(2)  # buffer for opcodes
        event_data_buf = bytearray(259)  # max 255 events + 4 bytes for n_cycles
        event_data_view = memoryview(event_data_buf)

        # convert counters to absolute time
        bpod_time_us = reset_time_us + bpod_count_us
        system_time_ns = reference_time_ns + (reference_count_ns - perf_count_ns)
        # TODO: handle start of state machine
        # TODO: handle start of state

        if logger.isEnabledFor(logging.DEBUG):
            bpod_time_s = bpod_time_us / 10**6
            system_time_s = system_time_ns / 10**9
            time_a = datetime.fromtimestamp(bpod_time_s).strftime('%H:%M:%S.%f')
            time_b = datetime.fromtimestamp(system_time_s).strftime('%H:%M:%S.%f')
            logger.debug('%s/%s: Starting state machine #%d', time_a, time_b, index)
            logger.debug('%s/%s: State %d', time_a, time_b, current_state)

        # enter the reading loop
        while not self._stop_event.is_set():
            # TODO: thread is blocked by readinto()
            #
            # Options:
            # a) use serial timeout,
            # b) while serial.in_waiting() < 2:
            #        if self._stop_event.wait(timeout=0.01):
            #            break
            # c) thread entirely controlled by bpod (no _stop_event required)
            #
            # readinto returns number of bytes read, so we can use it to check for
            # timeout

            # read the next two opcodes
            serial.readinto(opcode_buf)

            # read the system time
            perf_count_ns = time.perf_counter_ns()
            system_time_ns = reference_time_ns + (perf_count_ns - reference_count_ns)

            opcode, param = opcode_buf
            if opcode == 1:  # handle events
                # read `param` event bytes + 4 bytes for n_cycles (uInt32)
                serial.readinto(event_data_view[: param + 4])

                # unpack the number of cycles, calculate the event's timestamp
                (n_cycles,) = STRUCT_UINT32_LE.unpack_from(event_data_view, param)
                micros = reset_time_us + n_cycles * cycle_period

                # handle each event
                events = event_data_view[:param]
                for event in events:
                    if event != 255:
                        logger.debug(
                            '%d µs: Event %d - %s)', micros, event, event_names[event]
                        )
                    # TODO: handle event

                # handle state transitions / exit event
                for event in events:
                    if event == 255:  # exit event
                        self.stop()
                        break
                    target_state = state_transitions[current_state][event]
                    if target_state == current_state:  # no transition
                        continue
                    if target_state == target_exit:  # virtual exit state
                        # TODO: handle end of state
                        break
                    if target_state == target_back and use_back_op:  # back
                        target_state = previous_state
                    # TODO: handle end of state
                    previous_state = current_state
                    current_state = target_state
                    # TODO: handle start of state
                    logger.debug('%d µs: State %d', micros, current_state)
                    break  # only handle the first state transition

            elif opcode == 2:  # handle softcodes
                param -= 1
                logger.debug('Softcode %d', param)
                softcode_handler(param)

            else:
                raise RuntimeError(f'Unknown opcode: {opcode}')

        # exit state machine
        # read 12 bytes: cycles (uInt32) and micros (uInt64)
        cycles, micros = self._struct_exit.unpack(serial.read(12))
        logger.debug(
            '%d µs: Ending state machine #%d (%d cycles)', micros, index, cycles
        )
        # TODO: handle end of state machine
