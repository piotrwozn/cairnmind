"""The machine identity behind an installation's fingerprint (#70, #234): stable on Linux and on macOS.

No Mac runs these tests here: the Darwin path is selected by setting ``sys.platform`` and by stubbing the
``ioreg`` command with output shaped like the one of an Apple silicon Mac. The Linux path also runs for real.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from cairnmind import store_writer

UUID = "1A2B3C4D-5E6F-4A1B-8C2D-3E4F5A6B7C8D"

IOREG = f"""\
+-o J516cAP  <class IOPlatformExpertDevice, id 0x100000213, registered, matched, active, busy 0 (2010 ms), retain 39>
    {{
      "IOPolledInterface" = "AppleARMWatchdogTimerHibernateHandler is not serializable"
      "#address-cells" = <02000000>
      "AAPL,phandle" = <01000000>
      "IOBusyInterest" = "IOCommand is not serializable"
      "target-type" = <"J516c">
      "name" = <"device-tree">
      "manufacturer" = <"Apple Inc.">
      "compatible" = <"J516cAP","Mac15,8","AppleARM">
      "IOPlatformSerialNumber" = "XXXXXXXXXX"
      "model" = <"Mac15,8">
      "device_type" = <"bootrom">
      "#size-cells" = <02000000>
      "IOPlatformUUID" = "{UUID}"
    }}
"""

MACHINE_ID_FILES = ("/etc/machine-id", "/var/lib/dbus/machine-id")


class Machine:
    """A machine as the lookup sees it: its machine id files, its host name and its ioreg command."""

    def __init__(self, monkeypatch, *, platform: str, files: dict[str, str] | None = None,
                 host: str = "studio.local", ioreg=None):
        self.files, self.host, self.ioreg, self.calls = files or {}, host, ioreg, []
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(store_writer.socket, "gethostname", lambda: self.host)
        read_text = Path.read_text

        def fake_read(path, *args, **kwargs):
            name = str(path)
            if name in MACHINE_ID_FILES:
                if name not in self.files:
                    raise FileNotFoundError(name)
                return self.files[name]
            return read_text(path, *args, **kwargs)

        def fake_run(argv, *args, **kwargs):
            self.calls.append((list(argv), kwargs))
            if self.ioreg is None:
                raise AssertionError("ioreg must not run here")
            if isinstance(self.ioreg, BaseException):
                raise self.ioreg
            code, out = self.ioreg
            return subprocess.CompletedProcess(argv, code, out, "")

        monkeypatch.setattr(Path, "read_text", fake_read)
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(store_writer, "_known_platform_uuid", None, raising=False)  # this process read none
        monkeypatch.setattr(store_writer, "_ioreg_failed_at", float("-inf"), raising=False)


def test_a_mac_keeps_its_identity_when_the_host_name_changes(monkeypatch, tmp_path):
    mac = Machine(monkeypatch, platform="darwin", host="studio.local", ioreg=(0, IOREG))
    before = store_writer.machine_identity()
    first = store_writer.installation(tmp_path / "data")
    mac.host = "Piotrs-MacBook-Pro.wifi.example"  # another network: macOS renames the host
    assert store_writer.machine_identity() == before
    again = store_writer.installation(tmp_path / "data")
    assert again.id == first.id and again.copied_from is None


def test_a_mac_reads_the_platform_uuid_with_the_absolute_ioreg_and_a_time_limit(monkeypatch):
    mac = Machine(monkeypatch, platform="darwin", ioreg=(0, IOREG))
    assert store_writer.machine_identity() == "platform-uuid:" + UUID.lower()
    argv, kwargs = mac.calls[0]
    assert argv == ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]
    assert kwargs.get("timeout") and kwargs["timeout"] <= 10
    assert kwargs.get("check") is not True


def test_a_mac_does_not_read_machine_id_files(monkeypatch):
    # A file a person or a tool left at /etc/machine-id on a Mac does not replace the platform's own identity.
    Machine(monkeypatch, platform="darwin", files={"/etc/machine-id": "f" * 32 + "\n"}, ioreg=(0, IOREG))
    assert store_writer.machine_identity() == "platform-uuid:" + UUID.lower()


@pytest.mark.parametrize("failure", [
    FileNotFoundError("/usr/sbin/ioreg"),
    PermissionError("/usr/sbin/ioreg"),
    subprocess.TimeoutExpired(["/usr/sbin/ioreg"], 5),
    (1, ""),
    (0, ""),
    (0, "ioreg: no such class\n"),
    (1, IOREG),  # a failed run is not trusted, whatever it printed
])
def test_a_mac_whose_ioreg_fails_falls_back_to_the_host_name(monkeypatch, failure):
    Machine(monkeypatch, platform="darwin", host="studio.local", ioreg=failure)
    assert store_writer.machine_identity() == "host:studio.local"


def test_a_failed_lookup_is_tried_again_and_a_good_one_is_kept(monkeypatch):
    mac = Machine(monkeypatch, platform="darwin", ioreg=subprocess.TimeoutExpired(["ioreg"], 5))
    assert store_writer.machine_identity().startswith("host:")
    mac.ioreg = (0, IOREG)
    assert store_writer.machine_identity().startswith("host:")  # asked again only after IOREG_RETRY_SECONDS (#371)
    monkeypatch.setattr(store_writer, "_ioreg_failed_at", float("-inf"))
    assert store_writer.machine_identity() == "platform-uuid:" + UUID.lower()
    mac.ioreg = (1, "")  # once read, the identity of this process does not depend on another run
    assert store_writer.machine_identity() == "platform-uuid:" + UUID.lower()


def test_linux_order_is_unchanged_and_never_runs_ioreg(monkeypatch):
    linux = Machine(monkeypatch, platform="linux", files={"/etc/machine-id": "a" * 32 + "\n",
                                                          "/var/lib/dbus/machine-id": "b" * 32 + "\n"})
    assert store_writer.machine_identity() == "machine-id:" + "a" * 32
    del linux.files["/etc/machine-id"]
    assert store_writer.machine_identity() == "machine-id:" + "b" * 32
    linux.files["/var/lib/dbus/machine-id"] = "  \n"
    assert store_writer.machine_identity() == "host:studio.local"
    assert linux.calls == []


def test_linux_machine_identity_for_real():
    if not sys.platform.startswith("linux") or not any(Path(path).is_file() for path in MACHINE_ID_FILES):
        pytest.skip("needs a Linux machine with a machine id")
    assert store_writer.machine_identity().startswith("machine-id:")


def test_the_parser_reads_real_shaped_ioreg_output():
    assert store_writer.platform_uuid(IOREG) == UUID.lower()
    assert store_writer.platform_uuid(f'"IOPlatformUUID" = "{UUID.lower()}"\n') == UUID.lower()
    # An ioreg tree drawn with the branch characters of a deeper listing.
    assert store_writer.platform_uuid(f'  | |   "IOPlatformUUID" = "{UUID}"\n') == UUID.lower()


@pytest.mark.parametrize("text", [
    "",
    "garbage\x00\xff",
    '"IOPlatformSerialNumber" = "XXXXXXXXXX"\n',
    '"IOPlatformUUID" = "not-a-uuid"\n',
    '"IOPlatformUUID" = "1A2B3C4D5E6F4A1B8C2D3E4F5A6B7C8D"\n',  # no hyphens: not the form ioreg prints
    '"IOPlatformUUID" = "{1A2B3C4D-5E6F-4A1B-8C2D-3E4F5A6B7C8D}"\n',
    '"IOPlatformUUID" = "00000000-0000-0000-0000-000000000000"\n',  # the nil UUID names no machine
    f'"IOPlatformUUID" = "{UUID}" trailing\n',
    f'"IOPlatformUUIDX" = "{UUID}"\n',
    f'"IOPlatformUUID" = <"{UUID}">\n',
    f'"other" = "\\"IOPlatformUUID\\" = \\"{UUID}\\""\n',  # the key inside another value
    f'"IOPlatformUUID" = "{UUID}"\n"IOPlatformUUID" = "2A2B3C4D-5E6F-4A1B-8C2D-3E4F5A6B7C8D"\n',
    "x" * 100_000,
])
def test_the_parser_refuses_junk(text):
    assert store_writer.platform_uuid(text) is None


def test_the_parser_accepts_the_same_uuid_printed_twice():
    assert store_writer.platform_uuid(IOREG + IOREG) == UUID.lower()
