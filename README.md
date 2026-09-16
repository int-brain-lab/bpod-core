# bpod-core

A modern Python interface for Bpod Finite State Machines.

> [!WARNING]
> bpod-core is under development and not yet ready for production use.

The Bpod Finite State Machine is an open-source platform by
[*Sanworks*](https://sanworks.io/) for controlling behavioral experiments: a
microcontroller-based finite state machine that runs a trial's logic in real time and
talks to sensors, valves, and other peripherals with millisecond precision. Running the
state machine on dedicated microcontroller hardware, rather than a general-purpose
computer, keeps trial timing deterministic and free of OS jitter. This makes Bpod
particularly well-suited to closed-loop experiments, where state transitions must be
triggered by sensor readings within a tight, predictable latency.

bpod-core is a Python library for driving Bpod hardware. It grew out of the the need for
a modern, well-tested way to work with Bpod devices from Python, and is designed to fit
naturally into everyday research workflows.

This project is maintained by the software development team at the
[*International Brain Lab*](https://internationalbrainlab.org/).

## Goals

* **Interface to Bpod Devices:** Provides an interface for interacting with Bpod
  devices.
* **State Machine Management:** Offers capabilities to define, validate, and run state
  machines.
* **Standalone or Library Use:** Can function independently or be integrated as a
  library within other projects.
* **Performance-Oriented:** Designed to be lean and fast.
* **Quality Assurance:** Typed, tested, and documented.

## Non-Goals

* **No GUI:** Does not include a graphical user interface.
* **Limited High-Level Functionality:** Does not provide features such as data
  management, configuration, or calibration.
* **No Specific Module Support:** Does not implement support for specific Bpod modules.

## Links

* [bpod-core documentation](https://int-brain-lab.github.io/bpod-core) – the official documentation of bpod-core.
* [Bpod Wiki](https://sanworks.github.io/Bpod_Wiki) – maintained by [
  *Sanworks*](https://sanworks.io/).
* [PyBpod](https://pybpod.readthedocs.io) – another Python project for Bpod devices maintained by the [*Champalimaud Foundation*](http://research.fchampalimaud.org/).

---
[![CI](https://github.com/int-brain-lab/bpod-core/actions/workflows/main.yaml/badge.svg)](https://github.com/int-brain-lab/bpod-core/actions/workflows/main.yaml)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![Coverage Status](https://coveralls.io/repos/github/int-brain-lab/bpod-core/badge.svg?branch=main)](https://coveralls.io/github/int-brain-lab/bpod-core?branch=main)
[![License](https://img.shields.io/github/license/int-brain-lab/bpod-core)](https://github.com/int-brain-lab/bpod-core/blob/main/LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/int-brain-lab/bpod-core?include_prereleases)](https://github.com/int-brain-lab/bpod-core/releases/latest)
[![PyPI](https://img.shields.io/pypi/v/bpod-core)](https://pypi.org/project/bpod-core/)
[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.21497456-blue.svg)](https://doi.org/10.5281/zenodo.21497456)
