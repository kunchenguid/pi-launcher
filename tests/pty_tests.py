#!/usr/bin/env python3
"""
pty_tests.py - real pseudo-terminal tests for pi-launcher.

Each test creates a fresh PTY and a "shell" session leader that starts the
launcher as a foreground job (own process group, terminal handed over),
exactly like an interactive shell. The harness then drives the master side
and asserts the terminal semantics the launcher must preserve: controlling
TTY, foreground process group, winsize/SIGWINCH, Ctrl-C, Ctrl-Z/fg job
control, Pi-style group suspend, and raw byte transparency.

Usage: pty_tests.py <path-to-launcher-executable>
"""

import errno
import fcntl
import json
import os
import select
import signal
import struct
import sys
import termios
import time

LAUNCHER = sys.argv[1] if len(sys.argv) > 1 else None
if not LAUNCHER or not os.path.isfile(LAUNCHER):
    print("usage: pty_tests.py <launcher-executable>", file=sys.stderr)
    sys.exit(2)

FAILURES = []
PASSES = []
TIMEOUT = 20.0


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


def shell_child(slave, result_w, cmd_r, launcher_argv, launcher_env):
    """Session leader acting as the interactive shell."""
    os.setsid()
    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    os.dup2(slave, 0)
    os.dup2(slave, 1)
    os.dup2(slave, 2)
    if slave > 2:
        os.close(slave)

    # tcsetpgrp from a background process group raises SIGTTOU; block it
    # like every real shell does.
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})

    def report(event):
        os.write(result_w, (json.dumps(event) + "\n").encode())

    lpid = os.fork()
    if lpid == 0:
        try:
            os.setpgid(0, 0)
            os.execve(launcher_argv[0], launcher_argv, launcher_env)
        except BaseException:
            os._exit(127)
    os.setpgid(lpid, lpid)
    os.tcsetpgrp(0, lpid)
    report({"event": "spawned", "pid": lpid, "sid": os.getsid(0)})

    for _ in range(64):
        _, status = os.waitpid(lpid, os.WUNTRACED)
        if os.WIFSTOPPED(status):
            report({"event": "stopped", "sig": os.WSTOPSIG(status)})
            # Wait for the harness to order a foreground resume.
            cmd = b""
            while not cmd.startswith(b"fg"):
                chunk = os.read(cmd_r, 2)
                if not chunk:
                    os._exit(3)
                cmd += chunk
            os.killpg(lpid, signal.SIGCONT)
            continue
        if os.WIFEXITED(status):
            report({"event": "exit", "code": os.WEXITSTATUS(status)})
            os._exit(0)
        if os.WIFSIGNALED(status):
            report({"event": "signaled", "sig": os.WTERMSIG(status)})
            os._exit(0)
    os._exit(4)


class PtySession:
    def __init__(self, env, rows=43, cols=132, args=None):
        self.env = env
        self.args = args or []
        self.master, slave = os.openpty()
        fcntl.ioctl(
            slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
        )
        self.result_r, result_w = os.pipe()
        cmd_r, self.cmd_w = os.pipe()
        self.shell_pid = os.fork()
        if self.shell_pid == 0:
            os.close(self.master)
            os.close(self.result_r)
            os.close(self.cmd_w)
            try:
                shell_child(
                    slave,
                    result_w,
                    cmd_r,
                    [LAUNCHER] + self.args,
                    self.env,
                )
            finally:
                os._exit(5)
        os.close(slave)
        os.close(result_w)
        os.close(cmd_r)
        self.output = b""
        self.events = []
        self._result_buf = b""
        self.deadline = time.monotonic() + TIMEOUT

    def _read_available(self):
        """Pull whatever is ready from master + result pipe."""
        fds = [self.master, self.result_r]
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("pty session timed out")
            r, _, _ = select.select(fds, [], [], min(remaining, 0.25))
            if not r:
                return
            for fd in r:
                try:
                    data = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO and fd == self.master:
                        data = b""
                    else:
                        raise
                if not data:
                    if fd == self.master:
                        self.master = -1
                        fds.remove(fd)
                    else:
                        self.result_r = -1
                        fds.remove(fd)
                    continue
                if fd == self.master:
                    self.output += data
                else:
                    self._result_buf += data
                    while b"\n" in self._result_buf:
                        line, self._result_buf = self._result_buf.split(b"\n", 1)
                        self.events.append(json.loads(line.decode()))

    def wait_event(self, kind):
        """Wait for a shell event of the given kind."""
        while True:
            for event in self.events:
                if event["event"] == kind:
                    return event
            self._read_available()
            if self.result_r == -1:
                raise RuntimeError(f"result pipe closed before event {kind}")

    def wait_output(self, marker, since=0):
        """Wait until marker (bytes) appears in master output at >= since."""
        while True:
            idx = self.output.find(marker, since)
            if idx >= 0:
                return idx + len(marker)
            self._read_available()
            if self.master == -1:
                raise RuntimeError(f"master closed before {marker!r}; got {self.output!r}")

    def output_quiet(self, quiet_seconds=0.4):
        """Wait for output to settle; return True if no new bytes arrived."""
        end = time.monotonic() + quiet_seconds
        before = len(self.output)
        while time.monotonic() < end:
            try:
                self._read_available()
            except TimeoutError:
                break
        return len(self.output) == before

    def write(self, data):
        os.write(self.master, data)

    def foreground(self):
        os.write(self.cmd_w, b"fg")

    def resize(self, rows, cols):
        fcntl.ioctl(
            self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
        )

    def finish(self):
        """Final launcher status event; also reaps the shell child."""
        event = None
        try:
            while True:
                for e in self.events:
                    if e["event"] in ("exit", "signaled"):
                        event = e
                        break
                if event:
                    break
                if self.result_r == -1:
                    break
                self._read_available()
        except (TimeoutError, RuntimeError):
            pass
        for fd in (self.master, self.result_r, self.cmd_w):
            try:
                if fd != -1:
                    os.close(fd)
            except OSError:
                pass
        try:
            os.waitpid(self.shell_pid, 0)
        except ChildProcessError:
            pass
        return event

    def abort(self):
        try:
            os.kill(self.shell_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.finish()


def base_env(mode):
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
        "PROBE_MODE": mode,
    }


def normalized(session, since=0):
    return session.output[since:].replace(b"\r\n", b"\n")


# --- Test 1: controlling TTY, foreground pgrp, session, winsize ----------
session = PtySession(base_env("tty"), rows=43, cols=132)
try:
    spawned = session.wait_event("spawned")
    session.wait_output(b"READY")
    text = normalized(session).decode()
    facts = dict(
        line.split(":", 1) for line in text.splitlines() if ":" in line
    )
    session.write(b"q")
    final = session.finish()
    check(
        "tty: probe has controlling tty on all stdio fds",
        facts.get("ISATTY") == "111",
        str(facts),
    )
    check(
        "tty: probe process group is the terminal foreground group",
        facts.get("FG") == "1" and facts.get("TCGETPGRP") == facts.get("PGRP"),
        str(facts),
    )
    check(
        "tty: terminal size inherited",
        facts.get("WINSZ") == "43x132",
        str(facts),
    )
    check(
        "tty: probe is in the caller's session and is the launcher's child",
        facts.get("SID") == str(spawned["sid"])
        and facts.get("PPID") == str(spawned["pid"]),
        f"facts={facts} spawned={spawned}",
    )
    check(
        "tty: exit status through PTY session",
        final and final["event"] == "exit" and final["code"] == 42,
        str(final),
    )
except Exception as exc:
    session.abort()
    check("tty session facts", False, repr(exc))


# --- Test 2: resize delivers SIGWINCH with the new size ------------------
session = PtySession(base_env("tty"))
try:
    session.wait_event("spawned")
    session.wait_output(b"READY")
    mark = len(session.output)
    session.resize(40, 100)
    session.wait_output(b"EVT:SIGWINCH:40x100", since=mark)
    quiet = session.output_quiet()
    text = normalized(session, since=mark)
    occurrences = text.count(b"EVT:SIGWINCH")
    session.write(b"q")
    final = session.finish()
    check(
        "resize: SIGWINCH delivered once with new size",
        occurrences == 1 and quiet,
        f"occurrences={occurrences} out={text!r}",
    )
    check("resize: session still healthy", final and final["event"] == "exit" and final["code"] == 42, str(final))
except Exception as exc:
    session.abort()
    check("resize SIGWINCH", False, repr(exc))


# --- Test 3: Ctrl-C reaches Pi exactly once, launcher survives -----------
session = PtySession(base_env("tty"))
try:
    session.wait_event("spawned")
    session.wait_output(b"READY")
    mark = len(session.output)
    session.write(b"\x03")  # ^C -> SIGINT to the foreground process group
    session.wait_output(b"EVT:SIGINT:1", since=mark)
    quiet = session.output_quiet()
    text = normalized(session, since=mark)
    occurrences = text.count(b"EVT:SIGINT")
    session.write(b"q")
    final = session.finish()
    check(
        "ctrl-c: SIGINT delivered exactly once",
        occurrences == 1 and quiet,
        f"occurrences={occurrences} out={text!r}",
    )
    check(
        "ctrl-c: launcher survived a handled SIGINT and reproduced exit 42",
        final and final["event"] == "exit" and final["code"] == 42,
        str(final),
    )
except Exception as exc:
    session.abort()
    check("ctrl-c handled", False, repr(exc))


# --- Test 4: Ctrl-C with default disposition kills launcher by SIGINT ----
session = PtySession(dict(base_env("tty"), PROBE_NO_SIGINT_HANDLER="1"))
try:
    session.wait_event("spawned")
    session.wait_output(b"READY")
    session.write(b"\x03")
    final = session.finish()
    check(
        "ctrl-c: fatal SIGINT reproduces as launcher death by SIGINT",
        final and final["event"] == "signaled" and final["sig"] == signal.SIGINT,
        str(final),
    )
except Exception as exc:
    session.abort()
    check("ctrl-c fatal", False, repr(exc))


# --- Test 5: keyboard Ctrl-Z stop and fg resume --------------------------
session = PtySession(base_env("tty"))
try:
    session.wait_event("spawned")
    session.wait_output(b"READY")
    mark = len(session.output)
    session.write(b"\x1a")  # ^Z -> SIGTSTP from the line discipline
    stopped = session.wait_event("stopped")
    check(
        "ctrl-z: launcher job stopped with SIGTSTP",
        stopped["sig"] == signal.SIGTSTP,
        str(stopped),
    )
    session.foreground()
    session.wait_output(b"EVT:SIGCONT:1", since=mark)
    quiet = session.output_quiet()
    text = normalized(session, since=mark)
    occurrences = text.count(b"EVT:SIGCONT")
    session.write(b"q")
    final = session.finish()
    check(
        "ctrl-z: fg resumed the job exactly once",
        occurrences == 1 and quiet,
        f"occurrences={occurrences} out={text!r}",
    )
    check(
        "ctrl-z: exit status after resume",
        final and final["event"] == "exit" and final["code"] == 42,
        str(final),
    )
except Exception as exc:
    session.abort()
    check("ctrl-z keyboard job control", False, repr(exc))


# --- Test 6: Pi-style suspend (kill(0, SIGTSTP)) and fg resume -----------
session = PtySession(base_env("tty"))
try:
    session.wait_event("spawned")
    session.wait_output(b"READY")
    mark = len(session.output)
    session.write(b"z")  # probe stops its whole process group, like Pi's Ctrl-Z
    session.wait_output(b"SUSPEND", since=mark)
    stopped = session.wait_event("stopped")
    check(
        "pi-suspend: launcher job stopped with SIGTSTP",
        stopped["sig"] == signal.SIGTSTP,
        str(stopped),
    )
    session.foreground()
    session.wait_output(b"EVT:SIGCONT:1", since=mark)
    quiet = session.output_quiet()
    occurrences = normalized(session, since=mark).count(b"EVT:SIGCONT")
    session.write(b"q")
    final = session.finish()
    check(
        "pi-suspend: fg resumed the job exactly once",
        occurrences == 1 and quiet,
        f"occurrences={occurrences}",
    )
    check(
        "pi-suspend: exit status after resume",
        final and final["event"] == "exit" and final["code"] == 42,
        str(final),
    )
except Exception as exc:
    session.abort()
    check("pi-style group suspend", False, repr(exc))


# --- Test 7: raw-mode byte transparency through the PTY ------------------
session = PtySession(dict(base_env("rawecho"), PROBE_ECHO_COUNT="512"))
try:
    session.wait_event("spawned")
    session.wait_output(b"READY")
    # Drain READY (and anything else) so the echo stream starts clean.
    try:
        while True:
            r, _, _ = select.select([session.master], [], [], 0.2)
            if not r:
                break
            os.read(session.master, 65536)
    except OSError:
        pass
    blob = bytes(range(256)) * 2
    echoed = b""
    sent = 0
    deadline = time.monotonic() + TIMEOUT
    while sent < len(blob) or len(echoed) < len(blob):
        if time.monotonic() > deadline:
            raise TimeoutError("rawecho stalled")
        r, w, _ = select.select([session.master], [session.master], [], 0.25)
        if w and sent < len(blob):
            sent += os.write(session.master, blob[sent : sent + 4096])
        if r:
            echoed += os.read(session.master, 65536)
    # The probe's trailing DONE line may share the last read; only the
    # first len(blob) echo bytes are the transparency proof.
    final = session.finish()
    check(
        "raw PTY byte transparency (all 256 byte values)",
        echoed[: len(blob)] == blob,
        f"echoed {len(echoed)} bytes, match={echoed[: len(blob)] == blob}",
    )
    check(
        "rawecho: clean exit",
        final and final["event"] == "exit" and final["code"] == 0,
        str(final),
    )
except Exception as exc:
    session.abort()
    check("raw PTY byte transparency", False, repr(exc))


print()
print(f"pty: {len(PASSES)} passed, {len(FAILURES)} failed")
sys.exit(1 if FAILURES else 0)
