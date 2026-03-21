"""Threads for FSM execution and data collection from the Bpod hardware."""

import logging
import select
import struct
import threading
import time
from collections.abc import Callable
from datetime import datetime
from enum import IntEnum, auto
from queue import Queue

from bpod_core.bpod.structs import RawEvent, TimeReferences
from bpod_core.com import ExtendedSerial
from bpod_core.constants import STRUCT_UINT32_LE

logger = logging.getLogger(__name__)

_TIMING_VIOLATION_THRESHOLD_US = 1_000
"""Threshold for logging timing violations in microseconds."""


class EventID(IntEnum):
    """Enum for event codes."""

    STOP_SENTINEL = -100
    """Sentinel value for stopping threads."""
    EXIT = 255
    """Exit code for the Bpod."""
    START_FSM = auto()
    START_STATE = auto()
    END_FSM_CYCLES = auto()
    END_FSM_MICROS = auto()


class ReadThread(threading.Thread):
    """A thread for managing the execution of a finite state machine on the Bpod."""

    _struct_exit = struct.Struct('<IQ')

    def __init__(
        self,
        *,
        serial: ExtendedSerial,
        fsm_index: int,
        confirm_fsm: bool,
        cycle_period_us: int,
        queue_events: Queue[RawEvent],
        queue_softcodes: Queue[int],
    ) -> None:
        """
        Initialize the EventThread.

        Parameters
        ----------
        serial : ExtendedSerial
            The serial connection to the Bpod device.
        fsm_index : int
            The index of the FSM being managed.
        confirm_fsm : bool
            Whether to confirm the FSM with the Bpod device.
        cycle_period_us : int
            The cycle period of the Bpod device in microseconds.
        queue_events : Queue[RawEvent]
            Queue for storing events.
        queue_softcodes : Queue[int]
            Queue for storing softcodes.
        """
        super().__init__(name='CommunicationThread', daemon=True)
        self.serial = serial
        self._stop_event = threading.Event()
        self._index = fsm_index
        self._confirm_fsm = confirm_fsm
        self._cycle_period_us = cycle_period_us
        self._queue_events = queue_events
        self._queue_softcodes = queue_softcodes

    def stop(self) -> None:
        """Signal the FSM thread to stop after the current state cycle."""
        self._stop_event.set()

    def run(self) -> None:
        """Execute the CommunicationThread."""
        # confirm the state machine
        if self._confirm_fsm and not self.serial.read_bool():
            raise RuntimeError(f'State machine #{self._index} not confirmed by Bpod')

        # read the starting timestamps of the state machine
        # we do this early to get an accurate timestamp for the system clock
        start_micros_us = self.serial.read_uint64()
        perf_count_ns = time.perf_counter_ns()

        # assign members to local variables to avoid repeated attribute lookups
        serial = self.serial
        cycle_period_us = self._cycle_period_us
        q_events = self._queue_events
        q_softcodes = self._queue_softcodes

        # create buffers / memoryview for repeated serial reads
        opcode_buf = bytearray(2)  # buffer for opcodes
        event_data_buf = bytearray(259)  # max 255 events + 4 bytes for n_cycles
        event_data_view = memoryview(event_data_buf)

        # handle events: start of state machine, start of state
        q_events.put(RawEvent(perf_count_ns, start_micros_us, EventID.START_FSM))
        q_events.put(RawEvent(perf_count_ns, start_micros_us, EventID.START_STATE))

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
            perf_count_ns = time.perf_counter_ns()
            opcode, param = opcode_buf

            if opcode == 1:  # handle events
                # read `param` event bytes + 4 bytes for n_cycles (uInt32)
                serial.readinto(event_data_view[: param + 4])

                # unpack the cycle count and derive the event's microsecond timestamp
                (n_cycles,) = STRUCT_UINT32_LE.unpack_from(event_data_view, param)
                derived_micros_us = start_micros_us + n_cycles * cycle_period_us

                # hand events over to the EventThread
                for event in event_data_view[:param]:
                    if event == EventID.EXIT:
                        self.stop()
                        break
                    q_events.put(RawEvent(perf_count_ns, derived_micros_us, event))

            elif opcode == 2:  # handle softcodes
                softcode = param - 1  # subtract 1 for zero-based indexing
                q_softcodes.put(softcode)
                # q_events.put(RawEvent(perf_count_ns, None, 10000 + softcode))

            else:
                raise RuntimeError(f'Received unknown opcode from Bpod: {opcode}')

        # the Bpod sends two values after exiting the state machine trial:
        # 1) n_cycles - the number of hardware timer callbacks executed (uInt32)
        # 2) bpod_micros - the microsecond count at the end of the trial (uInt64)
        serial.readinto(event_data_view[:12])
        perf_count_ns = time.perf_counter_ns()
        n_cycles, end_micros_us = self._struct_exit.unpack_from(event_data_view)

        # we can use these values to verify the Bpod's hardware timer reliability. If
        # timer callbacks take longer than the cycle period, the actual trial duration
        # diverges from expected. A warning is logged if the absolute discrepancy of the
        # trial duration exceeds a threshold.
        duration_micros_us = end_micros_us - start_micros_us
        duration_cycles_us = n_cycles * cycle_period_us
        duration_discrepancy_us = abs(duration_cycles_us - duration_micros_us)
        if duration_discrepancy_us > _TIMING_VIOLATION_THRESHOLD_US:
            average_cycle_period_us = duration_micros_us / n_cycles
            logger.warning(
                'Violation of hardware timing guarantees. Trial was %d µs %s than '
                'expected. Average cycle period: %0.3f µs (expected: %0.3f µs).',
                duration_discrepancy_us,
                'longer' if duration_micros_us > duration_cycles_us else 'shorter',
                average_cycle_period_us,
                cycle_period_us,
            )

        # enqueue end-of-trial event
        derived_micros = start_micros_us + duration_cycles_us
        q_events.put(RawEvent(perf_count_ns, derived_micros, EventID.END_FSM_CYCLES))
        q_events.put(RawEvent(perf_count_ns, end_micros_us, EventID.END_FSM_MICROS))

        # stop all threads
        q_events.put(RawEvent(0, 0, EventID.STOP_SENTINEL))
        q_softcodes.put(EventID.STOP_SENTINEL)
        logger.debug('Stopping read thread')


class EventThread(threading.Thread):
    """A thread for handling incoming events during a state-machine run."""

    def __init__(
        self,
        *,
        event_queue: Queue[RawEvent],
        # fsm_index: int,
        # confirm_fsm: bool,
        # cycle_period: int,
        # state_transitions: npt.NDArray[np.uint8],
        # use_back_op: bool,
        # event_names: list[str],
        time_reference: TimeReferences,
    ) -> None:
        """
        Initialize the FSMThread.

        Parameters
        ----------
        event_queue : Queue[RawEvent]
            Queue for storing events.
        fsm_index : int
            The index of the FSM being managed.
        confirm_fsm : bool
            Whether to confirm the FSM with the Bpod device.
        cycle_period : int
            The cycle period of the Bpod device in microseconds.
        state_transitions : np.ndarray
            The state transition matrix.
        use_back_op : bool
            Whether the state machine makes use of the ``>back`` operator.
        event_names : list of str
            Names of all events the FSM can receive, used for logging.
        time_reference : TimeReferences
            Reference values for performance counters.
        """
        super().__init__(name='EventThread', daemon=True)
        self._event_queue = event_queue
        self._time_reference = time_reference

    def stop(self) -> None:
        """Signal the FSM thread to stop."""
        self._event_queue.put(RawEvent(0, 0, EventID.STOP_SENTINEL))

    def run(self) -> None:
        """Execute the EventThread."""
        event_queue = self._event_queue

        base_time_bpod_us = self._time_reference.reset_system_time_ns // 1000
        base_time_system_ns = (
            self._time_reference.reset_system_time_ns
            - self._time_reference.init_perf_counter_ns
        )

        while True:
            # get the next event
            perf_count_ns, bpod_count_us, event_index = event_queue.get()

            # check if we need to stop the thread
            if event_index == EventID.STOP_SENTINEL:
                event_queue.task_done()
                break

            # convert relative timestamps to absolute timestamps
            time_system_ns = base_time_system_ns + perf_count_ns
            time_bpod_us = base_time_bpod_us + bpod_count_us

            if logger.isEnabledFor(logging.DEBUG):
                bpod_time_s = time_bpod_us / 10**6
                time_a = datetime.fromtimestamp(bpod_time_s).strftime('%H:%M:%S.%f')
                logger.debug('%s: #%d', time_a, event_index)

            # logger.debug(
            #     '%s, %i, %s, EVENT %d',
            #     (system_time_ns // 1000 - bpod_time_us) if bpod_time_us else None,
            #     system_time_ns // 1000,
            #     bpod_time_us,
            #     event_index,
            # )

            # target_state = state_transitions[current_state][event_index]
            # if target_state == current_state:  # no transition
            #     continue
            # if target_state == target_exit:  # virtual exit state
            #     # TODO: handle end of state
            #     break
            # if target_state == target_back and use_back_op:  # back
            #     target_state = previous_state
            # # TODO: handle end of state
            # previous_state = current_state
            # current_state = target_state
            # # TODO: handle start of state
            # logger.debug('%d µs: State %d', micros, current_state)
            # break  # only handle the first state transition

            event_queue.task_done()

        logger.debug('Stopping event thread')

    def get_data(self) -> None:
        """Get the data from the event queue."""
        # TODO: implement
        return


class SoftcodeThread(threading.Thread):
    """A thread for managing the execution of softcodes."""

    def __init__(
        self,
        *,
        softcode_queue: Queue[int],
        softcode_handler: Callable[[int], None] | None,
    ) -> None:
        super().__init__(name='SoftcodeThread', daemon=True)
        self._softcode_queue = softcode_queue
        self._softcode_handler = softcode_handler

    def stop(self) -> None:
        """Signal the FSM thread to stop."""
        self._softcode_queue.put(EventID.STOP_SENTINEL)

    def run(self) -> None:
        """Execute the SoftcodeThread."""
        # assign members to local variables to avoid repeated attribute lookups
        queue = self._softcode_queue
        softcode_handler = self._softcode_handler
        handler_name = getattr(softcode_handler, '__name__', 'unknown')

        # enter the reading loop
        while True:
            softcode = queue.get()
            if softcode == EventID.STOP_SENTINEL:
                break
            if softcode_handler is not None:
                logger.debug("Calling '%s(%d)'", handler_name, softcode)
                try:
                    # TODO: get perf_count_ns, add to event queue?
                    softcode_handler(softcode)
                    # TODO: get perf_count_ns, add to event queue?
                except Exception as e:
                    logger.exception(
                        "Error in user-provided handler '%s' for softcode %d",
                        handler_name,
                        softcode,
                        exc_info=e,
                        stack_info=True,
                    )
            else:
                logger.warning(
                    'Received softcode %d from Bpod but no handler is defined', softcode
                )
            queue.task_done()
        logger.debug('Stopping softcode thread')
