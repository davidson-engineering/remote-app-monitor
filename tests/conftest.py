import contextlib
import os
import sys

import pytest


class FakeSerialDevice:
    """A pseudo-terminal pair: the monitor opens ``port``, the test writes to
    the other end as if it were the device."""

    def __init__(self) -> None:
        import tty

        self._controller, self._device = os.openpty()
        tty.setraw(self._controller)
        tty.setraw(self._device)
        self.port = os.ttyname(self._device)

    def write(self, data: bytes) -> None:
        os.write(self._controller, data)

    def close(self) -> None:
        for fd in (self._controller, self._device):
            with contextlib.suppress(OSError):  # already closed
                os.close(fd)


@pytest.fixture
def fake_device():
    if sys.platform == "win32":
        pytest.skip("pseudo-terminals are POSIX only")
    device = FakeSerialDevice()
    yield device
    device.close()
