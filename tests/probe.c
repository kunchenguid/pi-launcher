/*
 * probe.c - test double that stands in for the bundled Pi payload.
 *
 * The launcher under test never knows the difference: it resolves and
 * execs Contents/Resources/pi/pi, which in the test bundle is this
 * binary. The probe exposes what the launcher must preserve: argv,
 * environment, cwd, stdio bytes, terminal state, signals, and exit/signal
 * termination. Modes are selected with PROBE_MODE so the same binary
 * covers every test.
 */

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

static struct termios g_saved_termios;
static bool g_termios_saved = false;
static volatile sig_atomic_t g_sigint_count = 0;
static volatile sig_atomic_t g_sigcont_count = 0;

static void die(const char *what) {
  fprintf(stderr, "probe: %s: %s\n", what, strerror(errno));
  _exit(111);
}

static void print_hex_bytes(const char *label, const char *data, size_t len) {
  printf("%s%zu:", label, len);
  for (size_t i = 0; i < len; i++) {
    printf("%02x", (unsigned char)data[i]);
  }
  printf("\n");
}

static int env_cmp(const void *a, const void *b) {
  return strcmp(*(const char *const *)a, *(const char *const *)b);
}

extern char **environ;

/* dump: report argv, environment, cwd, and parent, then exit. */
static int mode_dump(int argc, char *argv[]) {
  printf("PPID:%d\n", (int)getppid());
  printf("ARGC:%d\n", argc);
  for (int i = 0; i < argc; i++) {
    char label[32];
    snprintf(label, sizeof(label), "ARGV%d:", i);
    print_hex_bytes(label, argv[i], strlen(argv[i]));
  }
  char cwd[4096];
  if (getcwd(cwd, sizeof(cwd)) == NULL) {
    die("getcwd");
  }
  printf("CWD:%s\n", cwd);

  size_t count = 0;
  for (char **e = environ; *e != NULL; e++) {
    count++;
  }
  char **sorted = calloc(count, sizeof(char *));
  if (sorted == NULL) {
    die("calloc");
  }
  for (size_t i = 0; i < count; i++) {
    sorted[i] = environ[i];
  }
  qsort(sorted, count, sizeof(char *), env_cmp);
  printf("ENVCOUNT:%zu\n", count);
  for (size_t i = 0; i < count; i++) {
    const char *eq = strchr(sorted[i], '=');
    if (eq == NULL) {
      print_hex_bytes("ENVNAME:", sorted[i], strlen(sorted[i]));
      printf("ENVVALUE:0:\n");
    } else {
      print_hex_bytes("ENVNAME:", sorted[i], (size_t)(eq - sorted[i]));
      print_hex_bytes("ENVVALUE:", eq + 1, strlen(eq + 1));
    }
  }
  fflush(stdout);
  const char *code = getenv("PROBE_EXIT");
  return code != NULL ? atoi(code) : 0;
}

/* cat: copy stdin to stdout byte-exact, then mark stderr and exit. */
static int mode_cat(void) {
  char buf[65536];
  for (;;) {
    ssize_t n = read(STDIN_FILENO, buf, sizeof(buf));
    if (n < 0) {
      if (errno == EINTR) {
        continue;
      }
      die("read");
    }
    if (n == 0) {
      break;
    }
    ssize_t off = 0;
    while (off < n) {
      ssize_t w = write(STDOUT_FILENO, buf + off, (size_t)(n - off));
      if (w < 0) {
        if (errno == EINTR) {
          continue;
        }
        die("write");
      }
      off += w;
    }
  }
  const char *marker = "PROBE-STDERR-MARKER\n";
  (void)write(STDERR_FILENO, marker, strlen(marker));
  const char *code = getenv("PROBE_EXIT");
  return code != NULL ? atoi(code) : 0;
}

/* selfsig: die by the signal named in PROBE_SIGNAL. */
static int mode_selfsig(void) {
  const char *name = getenv("PROBE_SIGNAL");
  if (name == NULL) {
    die("PROBE_SIGNAL unset");
  }
  int sig = 0;
  if (strcmp(name, "SIGTERM") == 0) sig = SIGTERM;
  else if (strcmp(name, "SIGKILL") == 0) sig = SIGKILL;
  else if (strcmp(name, "SIGABRT") == 0) sig = SIGABRT;
  else if (strcmp(name, "SIGINT") == 0) sig = SIGINT;
  else if (strcmp(name, "SIGHUP") == 0) sig = SIGHUP;
  else die("unknown PROBE_SIGNAL");
  kill(getpid(), sig);
  _exit(112); /* Unreachable for fatal signals. */
}

static void restore_termios(void) {
  if (g_termios_saved) {
    tcsetattr(STDIN_FILENO, TCSANOW, &g_saved_termios);
    g_termios_saved = false;
  }
}

static void set_termios(bool raw) {
  if (tcgetattr(STDIN_FILENO, &g_saved_termios) != 0) {
    die("tcgetattr");
  }
  g_termios_saved = true;
  struct termios t = g_saved_termios;
  if (raw) {
    cfmakeraw(&t);
  } else {
    /* cbreak: byte-at-a-time reads, no echo, but ISIG stays on so the
     * line discipline still generates SIGINT/SIGTSTP from ^C/^Z. */
    t.c_lflag &= (tcflag_t)~(ICANON | ECHO);
    t.c_cc[VMIN] = 1;
    t.c_cc[VTIME] = 0;
  }
  if (tcsetattr(STDIN_FILENO, TCSANOW, &t) != 0) {
    die("tcsetattr");
  }
}

static void write_all_stdout(const char *data, size_t len) {
  size_t off = 0;
  while (off < len) {
    ssize_t w = write(STDOUT_FILENO, data + off, len - off);
    if (w < 0) {
      if (errno == EINTR) {
        continue;
      }
      _exit(113);
    }
    off += (size_t)w;
  }
}

static void on_sigint(int sig) {
  (void)sig;
  g_sigint_count++;
  char buf[32];
  int n = snprintf(buf, sizeof(buf), "EVT:SIGINT:%d\n", (int)g_sigint_count);
  write_all_stdout(buf, (size_t)n);
}

static void on_sigcont(int sig) {
  (void)sig;
  g_sigcont_count++;
  char buf[32];
  int n = snprintf(buf, sizeof(buf), "EVT:SIGCONT:%d\n", (int)g_sigcont_count);
  write_all_stdout(buf, (size_t)n);
}

static void on_sigwinch(int sig) {
  (void)sig;
  struct winsize ws;
  char buf[64];
  if (ioctl(STDIN_FILENO, TIOCGWINSZ, &ws) == 0) {
    int n = snprintf(buf, sizeof(buf), "EVT:SIGWINCH:%ux%u\n", ws.ws_row, ws.ws_col);
    write_all_stdout(buf, (size_t)n);
  }
}

/*
 * tty: report terminal/session facts, then play a scripted byte protocol
 * on stdin until 'q':
 *   'z' -> print SUSPEND and stop the whole process group (Pi's Ctrl-Z)
 *   'q' -> restore termios, print BYE, exit PROBE_EXIT (default 42)
 *   any other byte -> ignored (events come from signal handlers)
 * PROBE_TERMIOS=raw uses full raw mode (default cbreak).
 * PROBE_NO_SIGINT_HANDLER=1 leaves SIGINT at its default disposition.
 */
static int mode_tty(void) {
  printf("PPID:%d\n", (int)getppid());
  printf("SID:%d\n", (int)getsid(0));
  printf("PGRP:%d\n", (int)getpgrp());
  printf("ISATTY:%d%d%d\n", isatty(0) ? 1 : 0, isatty(1) ? 1 : 0, isatty(2) ? 1 : 0);
  pid_t fg = tcgetpgrp(STDIN_FILENO);
  printf("TCGETPGRP:%d\n", (int)fg);
  printf("FG:%d\n", fg == getpgrp() ? 1 : 0);
  struct winsize ws;
  if (ioctl(STDIN_FILENO, TIOCGWINSZ, &ws) != 0) {
    die("TIOCGWINSZ");
  }
  printf("WINSZ:%ux%u\n", ws.ws_row, ws.ws_col);

  struct sigaction sa;
  memset(&sa, 0, sizeof(sa));
  sigemptyset(&sa.sa_mask);
  if (getenv("PROBE_NO_SIGINT_HANDLER") == NULL) {
    sa.sa_handler = on_sigint;
    sigaction(SIGINT, &sa, NULL);
  }
  sa.sa_handler = on_sigcont;
  sigaction(SIGCONT, &sa, NULL);
  sa.sa_handler = on_sigwinch;
  sigaction(SIGWINCH, &sa, NULL);

  bool raw = getenv("PROBE_TERMIOS") != NULL && strcmp(getenv("PROBE_TERMIOS"), "raw") == 0;
  set_termios(raw);
  printf("READY\n");
  fflush(stdout);

  for (;;) {
    unsigned char c;
    ssize_t n = read(STDIN_FILENO, &c, 1);
    if (n < 0) {
      if (errno == EINTR) {
        continue;
      }
      restore_termios();
      die("read");
    }
    if (n == 0) {
      restore_termios();
      die("unexpected EOF");
    }
    if (c == 'q') {
      restore_termios();
      printf("BYE\n");
      fflush(stdout);
      const char *code = getenv("PROBE_EXIT");
      return code != NULL ? atoi(code) : 42;
    }
    if (c == 'z') {
      printf("SUSPEND\n");
      fflush(stdout);
      /* Exactly what Pi does on Ctrl-Z: stop the whole process group. */
      kill(0, SIGTSTP);
    }
  }
}

static volatile sig_atomic_t g_sigterm_count = 0;

static void on_sigterm_count(int sig) {
  (void)sig;
  g_sigterm_count++;
  char buf[32];
  int n = snprintf(buf, sizeof(buf), "EVT:SIGTERM:%d\n", (int)g_sigterm_count);
  write_all_stdout(buf, (size_t)n);
}

/* sigwait: count SIGTERM deliveries and exit 55 on the first one (or die
 * by SIGTERM's default disposition when PROBE_TERM_DEFAULT=1). Proves a
 * signal aimed at the launcher pid is forwarded to Pi exactly once. */
static int mode_sigwait(void) {
  printf("READY\n");
  fflush(stdout);
  if (getenv("PROBE_TERM_DEFAULT") == NULL) {
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sigemptyset(&sa.sa_mask);
    sa.sa_handler = on_sigterm_count;
    sigaction(SIGTERM, &sa, NULL);
  }
  for (;;) {
    if (g_sigterm_count > 0) {
      return 55;
    }
    pause();
  }
}

/* rawecho: full raw mode, echo exactly PROBE_ECHO_COUNT bytes, exit 0. */
static int mode_rawecho(void) {
  const char *count_env = getenv("PROBE_ECHO_COUNT");
  long remaining = count_env != NULL ? atol(count_env) : 512;
  set_termios(true);
  printf("READY\n");
  fflush(stdout);
  while (remaining > 0) {
    unsigned char buf[4096];
    long chunk = remaining < (long)sizeof(buf) ? remaining : (long)sizeof(buf);
    ssize_t n = read(STDIN_FILENO, buf, (size_t)chunk);
    if (n < 0) {
      if (errno == EINTR) {
        continue;
      }
      restore_termios();
      die("read");
    }
    if (n == 0) {
      restore_termios();
      die("unexpected EOF");
    }
    write_all_stdout((const char *)buf, (size_t)n);
    remaining -= n;
  }
  restore_termios();
  printf("DONE\n");
  fflush(stdout);
  return 0;
}

int main(int argc, char *argv[]) {
  const char *mode = getenv("PROBE_MODE");
  if (mode == NULL) {
    mode = "dump";
  }
  int rc;
  if (strcmp(mode, "dump") == 0) {
    rc = mode_dump(argc, argv);
  } else if (strcmp(mode, "cat") == 0) {
    rc = mode_cat();
  } else if (strcmp(mode, "selfsig") == 0) {
    rc = mode_selfsig();
  } else if (strcmp(mode, "tty") == 0) {
    rc = mode_tty();
  } else if (strcmp(mode, "sigwait") == 0) {
    rc = mode_sigwait();
  } else if (strcmp(mode, "rawecho") == 0) {
    rc = mode_rawecho();
  } else {
    fprintf(stderr, "probe: unknown PROBE_MODE %s\n", mode);
    rc = 111;
  }
  return rc;
}
