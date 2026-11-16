# Native startup and resource boundaries

`kelly startup install --config config.json` explicitly registers startup for the
current account. `enable` remains an alias. `startup status` reports registration;
`startup uninstall` (also `disable`) removes only the matching registration.
Installation and ordinary server startup never register login services.

On macOS the LaunchAgent lives in `~/Library/LaunchAgents/` with a label derived
from the configured data home. It launches the same Python interpreter with
`-m kelly.startup --run` and an absolute config path in the config directory.
Bootstrap starts it immediately and RunAtLoad starts it at subsequent graphical
logins. It runs only while that account is logged in. A conflicting plist is
refused. Service status describes launchd registration, not HTTP readiness.
Windows retains the current-user, limited Task Scheduler task and `pythonw.exe`.

`kelly stop` uses authenticated loopback HTTP and observes the daemon lock; it
never kills an arbitrary PID. Both systems rotate UTF-8 server logs daily and
preserve active files, links, unrelated files and the last thirty days.

`kelly doctor` checks the database and native prerequisites locally. macOS checks
Security.framework and launchctl; Windows checks DPAPI and PowerShell. It does
not decrypt secrets or contact providers. A healthy installation reports `ok`.

`python scripts/check_install.py` builds and installs the locked wheel in a
temporary environment, checks health, authenticated shutdown, restart, backup,
restore, synthetic-key removal and uninstall. Evidence remains in its printed
temporary directory. On macOS every process uses an explicitly selected disposable
keychain that the check creates and deletes; it never changes the default keychain.

Darwin rejects RLIMIT_AS. Before opening the parser's stdin gate the parent
verifies physical-footprint accounting through `proc_pid_rusage`; a watchdog
samples it every 10 ms and sends SIGKILL to the worker's process group above the
configured limit. Accounting failures fail closed. An already exited process
has no accounting record and is handled as an exit, not an accounting failure.
RLIMIT_CPU remains active. This sampled boundary may overshoot between samples;
it is not an instantaneous allocation cap or a network sandbox.
Linux retains RLIMIT_AS/RLIMIT_CPU and Windows retains its Job Object.

POSIX mutable-file guards hold no-follow descriptors and recheck pathname
identity before and after guarded work. Unlike Windows delete-sharing denial,
POSIX permits an external rename but rejects the guarded operation when the
identity changed. SQLite's byte locking remains available on both systems.

Service tests use temporary homes and a stateful fake launchctl. Real bootstrap,
logout/login persistence and Windows execution require separate host acceptance.
