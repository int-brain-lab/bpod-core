import errno
import socket

from bpod_core import misc


class TestGetLocalIPv4:
    def test_returns_valid_ipv4(self):
        ip = misc.get_local_ipv4()
        parts = ip.split('.')
        assert len(parts) == 4
        assert all(0 <= int(p) < 256 for p in parts)

    def test_fallback_to_loopback_on_unreachable(self, monkeypatch):
        class DummySocket:
            def connect(self, addr):
                raise OSError(errno.ENETUNREACH, 'Network unreachable')

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(socket, 'socket', lambda *a, **k: DummySocket())
        ip = misc.get_local_ipv4()
        assert ip == '127.0.0.1'
