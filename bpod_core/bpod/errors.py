"""Exceptions raised by the bpod module."""


class BpodError(Exception):
    """Raised for errors specific to Bpod device operations."""


class BpodKeyError(BpodError, KeyError):
    """Exception class for Bpod-related key errors."""
