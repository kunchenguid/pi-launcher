#!/usr/bin/env python3
"""
functional.py - pipe-based functional tests for pi-launcher.

Drives the launcher from the TEST app bundle (Contents/Resources/pi/pi is
the probe binary) and asserts the transparency contract: argv, env, cwd,
stdio bytes, exit codes, signal deaths, parentage, and the fixed-target
security boundary.

Usage: functional.py <path-to-launcher-executable>
"""

import os
import signal
import subprocess
import sys
import tempfile
import time

LAUNCHER = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else None
if not LAUNCHER or not os.path.isfile(LAUNCHER):
    print("usage: functional.py <launcher-executable>", file=sys.stderr)
    sys.exit(2)

FAILURES = []
PASSES = []


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


def run_launcher(args, env=None, cwd=None, stdin_data=None):
    """Run the launcher; return (proc, stdout, stderr)."""
    full_env = dict(env) if env is not None else None
    proc = subprocess.Popen(
        [LAUNCHER] + args,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=full_env,
    )
    out, err = proc.communicate(input=stdin_data)
    return proc, out, err


def parse_dump(out):
    lines = out.decode("utf-8", errors="replace").splitlines()
    result = {"argv": [], "env": {}}
    i = 0
    envname = None
    while i < len(lines):
        line = lines[i]
        if line.startswith("PPID:"):
            result["ppid"] = int(line[5:])
        elif line.startswith("ARGC:"):
            result["argc"] = int(line[5:])
        elif line.startswith("ARGV"):
            _, rest = line.split(":", 1)
            length, hexdata = rest.split(":", 1)
            result["argv"].append(bytes.fromhex(hexdata))
        elif line.startswith("CWD:"):
            result["cwd"] = line[4:]
        elif line.startswith("ENVCOUNT:"):
            result["envcount"] = int(line[9:])
        elif line.startswith("ENVNAME:"):
            _, _length, hexdata = line.split(":", 2)
            envname = bytes.fromhex(hexdata)
        elif line.startswith("ENVVALUE:"):
            _, _length, hexdata = line.split(":", 2)
            if envname is not None:
                result["env"][envname] = bytes.fromhex(hexdata)
                envname = None
        i += 1
    return result


BASE_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/tmp"),
    "PROBE_MODE": "dump",
}


# 1. argv passthrough, byte-exact
args = ["--target", "/bin/false", "hello world", "", "ünïcødé", "-p", "--model=x", "a\nb", "--"]
proc, out, err = run_launcher(args, env=dict(BASE_ENV))
dump = parse_dump(out)
check(
    "argv passthrough is byte-exact",
    proc.returncode == 0
    and dump["argv"][1:] == [a.encode() for a in args]
    and dump["argc"] == len(args) + 1,
    f"rc={proc.returncode} argv={dump.get('argv')}",
)

# 2. environment passthrough, byte-exact
weird_env = dict(BASE_ENV)
weird_env.update(
    {
        "EMPTY_VAR": "",
        "UNICODE_VAR": "héllo ✓",
        "NEWLINE_VAR": "line1\nline2",
        "PROBE_MODE": "dump",
    }
)
proc, out, err = run_launcher([], env=weird_env)
dump = parse_dump(out)
sent = {k.encode(): v.encode() for k, v in weird_env.items()}
check(
    "environment passthrough is byte-exact",
    proc.returncode == 0 and dump["env"] == sent,
    f"rc={proc.returncode} missing={set(sent) - set(dump.get('env', {}))}",
)

# 3. cwd passthrough
with tempfile.TemporaryDirectory() as tmp:
    subdir = os.path.join(tmp, "sub dir")
    os.mkdir(subdir)
    proc, out, err = run_launcher([], env=dict(BASE_ENV), cwd=subdir)
    dump = parse_dump(out)
    check(
        "cwd passthrough",
        proc.returncode == 0 and os.path.realpath(dump["cwd"]) == os.path.realpath(subdir),
        f"rc={proc.returncode} cwd={dump.get('cwd')}",
    )

# 4. stdio byte transparency
payload = bytes(range(256)) * 4 + os.urandom(1024 * 1024)
env = dict(BASE_ENV, PROBE_MODE="cat", PROBE_EXIT="7")
proc, out, err = run_launcher([], env=env, stdin_data=payload)
check(
    "stdin/stdout byte transparency",
    proc.returncode == 7 and out == payload,
    f"rc={proc.returncode} outlen={len(out)} want={len(payload)}",
)
check(
    "stderr passthrough",
    err == b"PROBE-STDERR-MARKER\n",
    f"stderr={err[:80]!r}",
)

# 5. exit codes reproduce exactly
ok = True
for code in (0, 1, 42, 126, 127, 255):
    proc, out, err = run_launcher([], env=dict(BASE_ENV, PROBE_EXIT=str(code)))
    if proc.returncode != code:
        ok = False
        break
check("exit codes reproduce", ok, f"last rc={proc.returncode} want={code}")

# 6. signal deaths reproduce as the same signal
ok = True
detail = ""
for signame in ("SIGTERM", "SIGKILL", "SIGABRT", "SIGINT", "SIGHUP"):
    signum = getattr(signal, signame)
    proc, out, err = run_launcher(
        [], env=dict(BASE_ENV, PROBE_MODE="selfsig", PROBE_SIGNAL=signame)
    )
    if proc.returncode != -signum:
        ok = False
        detail = f"{signame}: launcher rc={proc.returncode} want={-signum}"
        break
check("signal deaths reproduce as same signal", ok, detail)

# 7. launcher stays the direct parent
env = dict(BASE_ENV)
proc = subprocess.Popen(
    [LAUNCHER], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
)
out, err = proc.communicate()
dump = parse_dump(out)
check(
    "probe is a direct child of the launcher",
    proc.returncode == 0 and dump["ppid"] == proc.pid,
    f"probe ppid={dump.get('ppid')} launcher pid={proc.pid}",
)

# 7b. a signal aimed at the launcher pid is forwarded to Pi exactly once,
#     handled or fatal


def read_until_ready(proc, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        if b"READY" in line:
            return
    raise RuntimeError("probe never became ready")


# handled variant: probe counts deliveries and exits 55 on the first
proc = subprocess.Popen(
    [LAUNCHER],
    env=dict(BASE_ENV, PROBE_MODE="sigwait"),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
read_until_ready(proc)
os.kill(proc.pid, signal.SIGTERM)
out, err = proc.communicate(timeout=10)
check(
    "SIGTERM to launcher pid forwarded once, exit reproduced",
    proc.returncode == 55 and out.count(b"EVT:SIGTERM") == 1,
    f"rc={proc.returncode} out={out!r}",
)

# fatal variant: probe keeps the default disposition and dies by SIGTERM
proc = subprocess.Popen(
    [LAUNCHER],
    env=dict(BASE_ENV, PROBE_MODE="sigwait", PROBE_TERM_DEFAULT="1"),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
read_until_ready(proc)
os.kill(proc.pid, signal.SIGTERM)
out, err = proc.communicate(timeout=10)
check(
    "SIGTERM to launcher pid propagates as launcher death by SIGTERM",
    proc.returncode == -signal.SIGTERM,
    f"rc={proc.returncode}",
)

# 8. negative: env injection cannot redirect the target
evil_dir = tempfile.mkdtemp()
evil_pi = os.path.join(evil_dir, "pi")
with open(evil_pi, "w") as f:
    f.write("#!/bin/sh\necho EVIL-PATH-TARGET-RAN\nexit 99\n")
os.chmod(evil_pi, 0o755)
env = dict(BASE_ENV)
env.update(
    {
        "PATH": evil_dir + ":" + env["PATH"],
        "PI_LAUNCHER_TARGET": "/bin/false",
        "PI_TARGET": "/bin/false",
        "TARGET": "/bin/false",
        "PI_LAUNCHER_COMMAND": "evil",
    }
)
proc, out, err = run_launcher([], env=env)
dump = parse_dump(out)
check(
    "no PATH or env target selection",
    proc.returncode == 0
    and b"EVIL-PATH-TARGET-RAN" not in out
    and dump.get("argv", [b""])[0].endswith(b"/Contents/Resources/pi/pi"),
    f"rc={proc.returncode} argv0={dump.get('argv', [None])[0]!r} out={out[:120]!r}",
)

# 9. negative: target-looking flags are passed through verbatim, not parsed
proc, out, err = run_launcher(
    ["--target", "/bin/false", "--command=evil", "--launcher-target=/bin/false"],
    env=dict(BASE_ENV),
)
dump = parse_dump(out)
check(
    "no option parsing in the launcher",
    proc.returncode == 0
    and dump["argv"][1:] == [b"--target", b"/bin/false", b"--command=evil", b"--launcher-target=/bin/false"],
    f"rc={proc.returncode} argv={dump.get('argv')}",
)

# 10. negative: launcher outside a bundle refuses to run anything
rogue = os.path.join(evil_dir, "rogue-launcher")
subprocess.run(["cp", LAUNCHER, rogue], check=True)
proc = subprocess.Popen(
    [rogue], env=dict(BASE_ENV), stdout=subprocess.PIPE, stderr=subprocess.PIPE
)
out, err = proc.communicate()
check(
    "bare launcher binary has no fallback",
    proc.returncode == 127 and b"pi-launcher:" in err and b"EVIL" not in out,
    f"rc={proc.returncode} stderr={err[:160]!r}",
)

print()
print(f"functional: {len(PASSES)} passed, {len(FAILURES)} failed")
sys.exit(1 if FAILURES else 0)
