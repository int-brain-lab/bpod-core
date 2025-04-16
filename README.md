bpod-core
=========

**bpod-core** is a Python package for communicating with the *Bpod* device from [*Sanworks*](https://sanworks.io/).

This project is maintained by the software development team at the
[*International Brain Lab*](https://internationalbrainlab.org/).

Installation for use
--------------------

``` bash
git clone https://github.com/int-brain-lab/bpod-core.git
cd bpod
python3.10 -m venv ./venv
source ./venv/bin/activate
pip install --upgrade pip
pip install -e .
```

Currently, only Python v3.10 on Ubuntu 22.04, Fedora 38 and Windows 10 is being tested.

Installation for developers
---------------------------

``` bash
git clone https://github.com/int-brain-lab/bpod-core.git
cd bpod
python3.10 -m venv ./venv
source ./venv/bin/activate
pip install --upgrade pip
pip install -e .[DEV]
```

This repository is adhering to the following conventions:

* [semantic versioning](https://semver.org/) for consistent version numbering logic
* [ruff](https://docs.astral.sh/ruff) for linting and formatting
