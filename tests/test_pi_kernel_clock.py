"""pi/pi_sender.py: the kernel clock-state read (adjtimex, read-only) that replaces the once-a-minute
`timedatectl` run, its struct layout, the `timedatectl` reference, and the start/end cross-check line.

The verdict mirrors systemd's ntp_synced() (src/timedate/timedated.c, read at v257), which is what
`timedatectl` prints as "System clock synchronized": adjtimex() >= 0 and maxerror < 16000000 us, with
STA_UNSYNC and the TIME_ERROR return value deliberately NOT consulted."""
from __future__ import annotations

import contextlib
import ctypes
import io
import sys
import unittest
from unittest import mock

from tests.test_pi_sender import ps

TIME_ERROR = 5
STA_UNSYNC = 0x0040
LINUX = sys.platform.startswith("linux")
LINUX_LP64 = LINUX and ctypes.sizeof(ctypes.c_long) == 8

# Offsets of struct timex on Linux LP64 (aarch64 and x86_64), as measured with gcc and glibc's
# <sys/timex.h> on x86_64 (sizeof 208); the man page adjtimex(2) gives the same field order.
LP64_OFFSETS = {
    "modes": 0, "offset": 8, "freq": 16, "maxerror": 24, "esterror": 32, "status": 40, "constant": 48,
    "precision": 56, "tolerance": 64, "time": 72, "tick": 88, "ppsfreq": 96, "jitter": 104, "shift": 112,
    "stabil": 120, "jitcnt": 128, "calcnt": 136, "errcnt": 144, "stbcnt": 152, "tai": 160,
}


def fake_adjtimex(rc=0, status=0, maxerror=3856, seen=None):
    """Stands in for the libc call: fills the struct it is given and returns the adjtimex() return value."""

    def call(tx):
        if seen is not None:
            seen.append({"modes": tx.modes, "status": tx.status, "maxerror": tx.maxerror})
        tx.status = status
        tx.maxerror = maxerror
        return rc

    return call


def raising(exc):
    def call(tx):
        raise exc

    return call


class VerdictTests(unittest.TestCase):
    def test_a_typical_synchronized_reading_is_true(self):
        self.assertIs(ps.read_kernel_sync(fake_adjtimex(rc=0, status=0, maxerror=3856)), True)

    def test_maxerror_just_under_16_s_is_synchronized_and_16_s_is_not(self):
        self.assertIs(ps.read_kernel_sync(fake_adjtimex(maxerror=15_999_999)), True)
        self.assertIs(ps.read_kernel_sync(fake_adjtimex(maxerror=16_000_000)), False)
        self.assertIs(ps.read_kernel_sync(fake_adjtimex(maxerror=2_000_000_000)), False)  # fits a 32-bit C long too

    def test_time_error_with_unsync_set_but_a_small_maxerror_is_still_synchronized(self):
        # This mirrors systemd's ntp_synced(), which ignores STA_UNSYNC on purpose ("it may have been set to
        # prevent the kernel from touching the RTC") and never compares the return value with TIME_ERROR.
        # `timedatectl` says "yes" in this state, and the flag is defined as what timedatectl reported.
        self.assertIs(ps.read_kernel_sync(fake_adjtimex(rc=TIME_ERROR, status=STA_UNSYNC, maxerror=1000)), True)

    def test_a_clean_status_does_not_rescue_a_huge_maxerror(self):
        self.assertIs(ps.read_kernel_sync(fake_adjtimex(rc=0, status=0, maxerror=16_000_000)), False)

    def test_the_leap_second_return_values_do_not_matter(self):
        for rc in (0, 1, 2, 3, 4):
            with self.subTest(rc=rc):
                self.assertIs(ps.read_kernel_sync(fake_adjtimex(rc=rc, maxerror=500)), True)

    def test_a_negative_return_value_is_unknown_not_false(self):
        # systemd maps an adjtimex failure to "not synchronized"; this flag deliberately says "unknown" instead
        self.assertIsNone(ps.read_kernel_sync(fake_adjtimex(rc=-1, maxerror=0)))

    def test_failures_of_the_libc_call_are_unknown_and_never_raise(self):
        for exc in (AttributeError("this libc has no adjtimex"), OSError(1, "EPERM"), TypeError("x"),
                    ctypes.ArgumentError("bad argument"), ValueError("v"), RuntimeError("r")):
            with self.subTest(exc=type(exc).__name__):
                self.assertIsNone(ps.read_kernel_sync(raising(exc)))
                self.assertIsNone(ps.read_adjtimex(raising(exc)))

    def test_the_call_gets_a_zeroed_struct_with_modes_zero_which_is_read_only(self):
        seen = []
        ps.read_kernel_sync(fake_adjtimex(seen=seen))
        self.assertEqual(seen, [{"modes": 0, "status": 0, "maxerror": 0}])

    def test_the_internal_read_returns_rc_status_and_maxerror(self):
        clock = ps.read_adjtimex(fake_adjtimex(rc=1, status=0x2001, maxerror=77))
        self.assertEqual(clock, ps.KernelClock(1, 0x2001, 77))
        self.assertEqual((clock.rc, clock.status, clock.maxerror_us), (1, 0x2001, 77))


class UnavailableTests(unittest.TestCase):
    def test_a_non_linux_platform_is_unknown_and_never_touches_libc(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(ps, "_libc_adjtimex") as libc:
            self.assertIsNone(ps.read_kernel_sync())
            self.assertIsNone(ps.read_adjtimex())
        libc.assert_not_called()

    def test_a_python_without_ctypes_is_unknown(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(ps, "ctypes", None), \
                mock.patch.object(ps, "_libc_adjtimex") as libc:
            self.assertIsNone(ps.read_kernel_sync())
            self.assertIsNone(ps.read_adjtimex())
        libc.assert_not_called()

    def test_no_subprocess_is_ever_started_by_the_kernel_read(self):
        with mock.patch.object(ps.subprocess, "run") as run, mock.patch.object(ps.subprocess, "Popen") as popen:
            ps.read_kernel_sync(fake_adjtimex())
            ps.read_kernel_sync()  # the real default path: libc on Linux, unknown elsewhere
        run.assert_not_called()
        popen.assert_not_called()

    @unittest.skipUnless(LINUX, "needs the Linux adjtimex system call")
    def test_the_real_libc_call_returns_a_verdict_or_unknown_without_raising(self):
        clock = ps.read_adjtimex()
        if clock is not None:  # None only where a sandbox blocks the call
            self.assertTrue(0 <= clock.rc <= TIME_ERROR, clock)
            self.assertGreaterEqual(clock.maxerror_us, 0)
        self.assertIn(ps.read_kernel_sync(), (True, False, None))


class LayoutTests(unittest.TestCase):
    def test_the_struct_with_an_8_byte_long_has_the_c_abi_size_and_offsets_on_every_platform(self):
        timex = ps.make_timex(ctypes.c_int64)  # a stand-in for the LP64 C `long`
        self.assertEqual(ctypes.sizeof(timex), 208)
        for name, offset in LP64_OFFSETS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(timex, name).offset, offset)

    @unittest.skipUnless(LINUX_LP64, "needs Linux LP64: the real struct is built from the C `long`, which is 4 bytes on Windows and 32-bit Linux")
    def test_the_real_struct_is_208_bytes_with_maxerror_at_24_and_status_at_40(self):
        self.assertEqual(ctypes.sizeof(ps._Timex), 208)
        self.assertEqual(ps._Timex.maxerror.offset, 24)
        self.assertEqual(ps._Timex.status.offset, 40)
        for name, offset in LP64_OFFSETS.items():
            self.assertEqual(getattr(ps._Timex, name).offset, offset, name)


class LibcCallTests(unittest.TestCase):
    def test_a_failing_libc_call_raises_oserror_with_the_errno(self):
        with mock.patch.object(ps, "_adjtimex_fn", lambda ref: -1), mock.patch.object(ps.ctypes, "get_errno", return_value=1):
            with self.assertRaises(OSError) as ctx:
                ps._libc_adjtimex(ps._Timex())
        self.assertEqual(ctx.exception.errno, 1)

    def test_and_the_public_read_turns_that_into_unknown_not_false(self):
        with mock.patch.object(ps, "_adjtimex_fn", lambda ref: -1), mock.patch.object(ps.ctypes, "get_errno", return_value=1):
            self.assertIsNone(ps.read_adjtimex(ps._libc_adjtimex))
            self.assertIsNone(ps.read_kernel_sync(ps._libc_adjtimex))


class TimedatectlReferenceTests(unittest.TestCase):
    def test_parses_the_synchronized_line(self):
        self.assertIs(ps.read_timedatectl(runner=lambda: "System clock synchronized: yes\n"), True)
        self.assertIs(ps.read_timedatectl(runner=lambda: "System clock synchronized: no\n"), False)

    def test_unparsable_or_failing_is_unknown(self):
        self.assertIsNone(ps.read_timedatectl(runner=lambda: "nonsense"))
        self.assertIsNone(ps.read_timedatectl(runner=raising(OSError("no timedatectl"))))


class CrossCheckLineTests(unittest.TestCase):
    def make(self, clock, reference):
        lines = []
        return ps.NtpCrossCheck(reference=reference, kernel=lambda: clock, log=lines.append), lines

    def test_the_start_line_shows_all_three_adjtimex_values_next_to_the_timedatectl_answer(self):
        check, lines = self.make(ps.KernelClock(0, 0x0000, 3856), lambda: True)
        check.report("start")
        self.assertEqual(lines, ["ntp check (start): adjtimex rc=0 status=0x0000 maxerror_us=3856 -> True; timedatectl -> True"])

    def test_a_divergence_is_diagnosable_from_one_line(self):
        check, lines = self.make(ps.KernelClock(5, 0x0040, 16_000_000), lambda: True)
        check.report("end")
        self.assertEqual(lines, ["ntp check (end): adjtimex rc=5 status=0x0040 maxerror_us=16000000 -> False; timedatectl -> True"])

    def test_a_negative_return_value_is_printed_as_unknown(self):
        check, lines = self.make(ps.KernelClock(-1, 0, 0), lambda: False)
        check.report("start")
        self.assertEqual(lines, ["ntp check (start): adjtimex rc=-1 status=0x0000 maxerror_us=0 -> None; timedatectl -> False"])

    def test_a_broken_log_never_escapes_report(self):
        def broken(line):
            raise BrokenPipeError("stderr closed")

        check = ps.NtpCrossCheck(reference=lambda: True, kernel=lambda: ps.KernelClock(0, 0, 1), log=broken)
        check.report("start")  # must not raise: a closed stderr must not kill the sender or hide its exit code

    def test_info_stderr_writes_to_stderr_only(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            ps.info_stderr("hello")
        self.assertEqual((out.getvalue(), err.getvalue()), ("", "hello\n"))

    def test_the_defaults_main_uses_are_the_real_readers_and_stderr(self):
        check = ps.NtpCrossCheck()
        self.assertIs(check._reference, ps.read_timedatectl)
        self.assertIs(check._kernel, ps.read_adjtimex)
        self.assertIs(check._log, ps.info_stderr)

    def test_unavailable_adjtimex_or_a_failing_timedatectl_print_as_none_and_never_raise(self):
        lines = []
        check = ps.NtpCrossCheck(reference=raising(OSError("x")), kernel=raising(OSError("y")), log=lines.append)
        check.report("end")
        self.assertEqual(lines, ["ntp check (end): adjtimex unavailable -> None; timedatectl -> None"])
        check, lines = self.make(None, lambda: None)
        check.report("start")
        self.assertEqual(lines, ["ntp check (start): adjtimex unavailable -> None; timedatectl -> None"])


if __name__ == "__main__":
    unittest.main()
