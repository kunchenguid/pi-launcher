/*
 * pi-launcher - transparent fixed-target launcher for a bundled Pi CLI.
 *
 * Security and process contract (do not weaken):
 *
 *   - The only executable this program will ever run is the single Pi CLI
 *     bundled inside its own app bundle at a fixed relative path. There is
 *     no target option, no PATH lookup, no environment override, and no
 *     shell. All command-line arguments are passed to Pi verbatim.
 *
 *   - The launcher stays alive as Pi's direct parent in the caller's
 *     existing session: same controlling terminal, same process group,
 *     same foreground job. It never allocates a PTY, never daemonizes,
 *     never touches the terminal itself.
 *
 *   - Signals the terminal delivers to the foreground process group
 *     (SIGINT, SIGQUIT, SIGTSTP, SIGWINCH, ...) reach Pi directly. The
 *     launcher never re-delivers those, so Pi sees each one exactly once.
 *     The launcher ignores SIGINT/SIGQUIT so it can outlive a Pi that
 *     handles them, and it keeps default dispositions for the stop
 *     signals so Pi's Ctrl-Z (which stops the whole process group) and
 *     the shell's `fg` (which continues it) behave exactly as they do
 *     for a directly invoked `pi`.
 *
 *   - Signals directed at the launcher process itself (SIGHUP, SIGTERM,
 *     SIGUSR1, SIGUSR2) are forwarded to Pi once, so killing the
 *     launcher cannot orphan a live Pi.
 *
 *   - The launcher reaps Pi and reproduces its result: Pi's exit code,
 *     or death by the same signal that killed Pi.
 */

#include <errno.h>
#include <libproc.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

/* Path of the bundled Pi CLI relative to this executable, which lives at
 * <App>.app/Contents/MacOS/<launcher>. */
static const char kPayloadRelPath[] = "../Resources/pi/pi";

/* Signals directed at the launcher that are forwarded to Pi. Keyboard and
 * job-control signals are deliberately absent: the terminal delivers those
 * to the whole foreground process group, so Pi already receives them
 * exactly once. */
static const int kForwardedSignals[] = {SIGHUP, SIGTERM, SIGUSR1, SIGUSR2};

/* Child process id, shared with the forwarding handler. 0 = not yet known. */
static volatile sig_atomic_t g_child_pid = 0;

static void forward_to_child(int sig) {
  if (g_child_pid > 0) {
    kill((pid_t)g_child_pid, sig);
  }
}

/* Print a launcher-owned diagnostic and exit. These are the only bytes the
 * launcher ever writes; everything else on stdio belongs to Pi. */
static void die(int status, const char *what, const char *detail) {
  if (detail != NULL) {
    fprintf(stderr, "pi-launcher: %s: %s\n", what, detail);
  } else {
    fprintf(stderr, "pi-launcher: %s\n", what);
  }
  exit(status);
}

/* Resolve the absolute, symlink-free path of the bundled Pi CLI. */
static void resolve_payload_path(char *out, size_t out_size) {
  char self_path[PROC_PIDPATHINFO_MAXSIZE];
  if (proc_pidpath(getpid(), self_path, sizeof(self_path)) <= 0) {
    die(1, "cannot resolve own executable path", strerror(errno));
  }

  char resolved[PATH_MAX];
  if (realpath(self_path, resolved) == NULL) {
    die(1, "cannot canonicalize own executable path", strerror(errno));
  }

  char *slash = strrchr(resolved, '/');
  if (slash == NULL || slash == resolved) {
    die(1, "unexpected executable location", resolved);
  }
  *slash = '\0';

  char joined[PATH_MAX];
  if (snprintf(joined, sizeof(joined), "%s/%s", resolved, kPayloadRelPath) >=
      (int)sizeof(joined)) {
    die(1, "payload path too long", resolved);
  }
  if (realpath(joined, out) == NULL) {
    if (errno == ENOENT) {
      die(127, "bundled Pi is missing (reinstall the app)", joined);
    }
    die(1, "cannot canonicalize bundled Pi path", strerror(errno));
  }
  if (strlen(out) >= out_size) {
    die(1, "payload path too long", joined);
  }
}

static void check_payload(const char *path) {
  struct stat st;
  if (stat(path, &st) != 0) {
    die(127, "bundled Pi is missing (reinstall the app)", path);
  }
  if (!S_ISREG(st.st_mode)) {
    die(127, "bundled Pi is not a regular file", path);
  }
  if (access(path, X_OK) != 0) {
    die(126, "bundled Pi is not executable", path);
  }
}

/* Reset every catchable signal to its default disposition. The parent
 * installs ignores/handlers for its own supervision; none of those may
 * leak into Pi, because ignored dispositions survive exec. */
static void reset_signal_dispositions(void) {
  struct sigaction sa;
  memset(&sa, 0, sizeof(sa));
  sa.sa_handler = SIG_DFL;
  sigemptyset(&sa.sa_mask);
  for (int sig = 1; sig < NSIG; sig++) {
    if (sig == SIGKILL || sig == SIGSTOP) {
      continue;
    }
    sigaction(sig, &sa, NULL);
  }
}

static void install_parent_dispositions(sigset_t *forwarded_mask) {
  struct sigaction sa;
  memset(&sa, 0, sizeof(sa));
  sigemptyset(&sa.sa_mask);
  sa.sa_flags = SA_RESTART;

  /* The terminal already delivers these to Pi (same process group); the
   * launcher must survive them so it can keep parenting a Pi that handles
   * them, and must reproduce Pi's status if Pi dies from them. */
  sa.sa_handler = SIG_IGN;
  sigaction(SIGINT, &sa, NULL);
  sigaction(SIGQUIT, &sa, NULL);

  sa.sa_handler = forward_to_child;
  sigemptyset(forwarded_mask);
  for (size_t i = 0; i < sizeof(kForwardedSignals) / sizeof(kForwardedSignals[0]); i++) {
    int sig = kForwardedSignals[i];
    sigaction(sig, &sa, NULL);
    sigaddset(forwarded_mask, sig);
  }
}

int main(int argc, char *argv[]) {
  char payload_path[PATH_MAX];
  resolve_payload_path(payload_path, sizeof(payload_path));
  check_payload(payload_path);

  sigset_t forwarded_mask;
  install_parent_dispositions(&forwarded_mask);

  /* Block the forwarded signals around fork() so none can arrive before
   * g_child_pid is set; the child unblocks them before exec. */
  if (sigprocmask(SIG_BLOCK, &forwarded_mask, NULL) != 0) {
    die(1, "cannot block signals", strerror(errno));
  }

  pid_t child = fork();
  if (child < 0) {
    die(1, "cannot fork", strerror(errno));
  }

  if (child == 0) {
    /* Child: become Pi. Reset dispositions before unblocking so anything
     * pending lands on a default disposition, never on the parent's
     * forwarding handler. */
    reset_signal_dispositions();
    if (sigprocmask(SIG_UNBLOCK, &forwarded_mask, NULL) != 0) {
      die(1, "cannot unblock signals", strerror(errno));
    }

    char **child_argv = calloc((size_t)argc + 1, sizeof(char *));
    if (child_argv == NULL) {
      die(1, "out of memory", NULL);
    }
    child_argv[0] = payload_path;
    for (int i = 1; i < argc; i++) {
      child_argv[i] = argv[i];
    }
    child_argv[argc] = NULL;

    execve(payload_path, child_argv, environ);
    die(127, "cannot execute bundled Pi", strerror(errno));
  }

  g_child_pid = child;
  if (sigprocmask(SIG_UNBLOCK, &forwarded_mask, NULL) != 0) {
    die(1, "cannot unblock signals", strerror(errno));
  }

  /* Parent: wait for Pi, nothing else. No WUNTRACED on purpose: stop and
   * continue events are managed by the terminal and the shell at the
   * process-group level (Pi stops the whole group on Ctrl-Z; the shell
   * continues it on `fg`), so the launcher simply stops and resumes with
   * its group like any other member. */
  for (;;) {
    int status = 0;
    pid_t result = waitpid(child, &status, 0);
    if (result < 0) {
      if (errno == EINTR) {
        continue;
      }
      die(1, "cannot wait for bundled Pi", strerror(errno));
    }
    if (result != child) {
      continue;
    }
    if (WIFEXITED(status)) {
      return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
      int sig = WTERMSIG(status);
      /* Die the way Pi died so the caller observes the identical wait
       * status (e.g. exit code 130 for SIGINT). */
      signal(sig, SIG_DFL);
      kill(getpid(), sig);
      _exit(128 + sig); /* Unreachable unless the signal is blocked. */
    }
  }
}
