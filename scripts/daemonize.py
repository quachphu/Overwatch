"""Generic double-fork daemonizer. Usage: daemonize.py <logfile> -- <cmd> [args...]

Exists because plain `nohup cmd & ; disown` was observed to die whenever the spawning shell
call ended, even though the same pattern kept an unrelated `ngrok` agent alive for the whole
session (RESEARCH.md §13.17). A full daemon (fork, setsid, fork, redirect fds, exec) survives
because it leaves the caller's session and process group entirely rather than merely detaching
from job control.
"""

import os
import sys

WORKDIR = "/Users/phuthienquach/Downloads/Overwatch"


def main() -> None:
    args = sys.argv[1:]
    if "--" not in args:
        sys.exit("usage: daemonize.py <logfile> -- <cmd> [args...]")
    sep = args.index("--")
    logfile = args[0]
    cmd = args[sep + 1 :]

    if os.fork() > 0:
        return
    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    os.chdir(WORKDIR)
    log_fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull_fd, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.execvp(cmd[0], cmd)  # noqa: S606 — intentional: this *is* the daemonizing exec.


if __name__ == "__main__":
    main()
