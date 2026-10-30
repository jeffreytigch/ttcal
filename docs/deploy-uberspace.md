# Example: deploying on Uberspace

This is a **worked example**, not a requirement. It shows one complete setup on
[Uberspace](https://uberspace.de/) 7-style accounts: Radicale behind the
account's web backend, ttcal under supervisord, TLS handled by the host.

Adapt paths, user names and host names. The generic instructions are in
[install.md](install.md).

## What you need

- SSH access to the account.
- A working TimeTagger instance (this example assumes it is served under
  `https://time.example.com/timetagger/`, so the API base is
  `https://time.example.com/timetagger/api/v2`).
- A **Python 3.11 or newer** interpreter. The system `python3` on older accounts
  can be too old; find one that works:

  ```bash
  ls /usr/bin/python3.* 2>/dev/null
  python3.14 --version    # use whatever 3.11+ interpreter is available
  ```

- `htpasswd` (present on Uberspace) to create the CalDAV password file.
- `supervisorctl` (present on Uberspace).

The account already runs other services; all commands below stay inside the
home directory and use unprivileged ports. Nothing needs root.

## 1. Run Radicale

```bash
# a virtualenv for Radicale
python3.14 -m venv ~/radicale-venv
~/radicale-venv/bin/pip install --upgrade pip
~/radicale-venv/bin/pip install --upgrade radicale bcrypt

# configuration and data
mkdir -p ~/.config/radicale ~/radicale-data/collections
htpasswd -nbB caldav 'choose-a-long-password' > ~/.config/radicale/users
chmod 600 ~/.config/radicale/users
```

`~/.config/radicale/config`:

```ini
[server]
hosts = 127.0.0.1:5232

[auth]
type = htpasswd
htpasswd_filename = /home/USER/.config/radicale/users
htpasswd_encryption = bcrypt

[storage]
filesystem_folder = /home/USER/radicale-data/collections
```

Replace `USER` with the account name. Radicale listens on loopback only; the
web backend below exposes it with HTTPS. Do not expose port 5232 directly.

Supervisord unit `~/etc/services.d/radicale.ini`:

```ini
[program:radicale]
command=%(ENV_HOME)s/radicale-venv/bin/radicale --config %(ENV_HOME)s/.config/radicale/config
directory=%(ENV_HOME)s
autostart=yes
autorestart=true
startsecs=5
stdout_logfile=%(ENV_HOME)s/logs/radicale.out.log
stderr_logfile=%(ENV_HOME)s/logs/radicale.err.log
```

```bash
supervisorctl reread
supervisorctl update
supervisorctl status radicale
curl -i -u caldav:'choose-a-long-password' http://127.0.0.1:5232/
```

The last command should answer with a redirect to `/.web/` (200/302), not a
connection error.

## 2. Expose the calendar on its own subdomain

CalDAV discovery expects the server at the **root of its host**. Do not put it
under a path such as `/caldav/`; Apple Calendar does not discover that reliably.
TimeTagger may keep its existing path prefix; the calendar needs its own host.

Add the subdomain (DNS must point at Uberspace; the control panel or the manual
explains the details for the account):

```bash
uberspace web domain add cal.example.com
cd ~/html/cal.example.com
uberspace web backend set / --http --port 5232
uberspace web backend list
```

The backend list should show `cal.example.com/` pointing at port `5232`. TLS is
issued by the host; give it a minute, then check:

```bash
curl -i -X PROPFIND -u caldav:'choose-a-long-password' \
  https://cal.example.com/ -H 'Depth: 0'
```

Expect **207 Multi-Status**. A 401 means the password file or encryption setting
is wrong; a 404/502 means the backend or the Radicale process is not running.

## 3. Install ttcal

```bash
git clone https://github.com/your-name/ttcal.git ~/ttcal-src
python3.14 -m venv ~/ttcal-venv
~/ttcal-venv/bin/pip install --upgrade ~/ttcal-src
~/ttcal-venv/bin/ttcal --version
```

### Configuration

```bash
mkdir -p ~/.config/ttcal ~/.local/state/ttcal
cp ~/ttcal-src/ttcal.example.toml ~/.config/ttcal/ttcal.toml
chmod 600 ~/.config/ttcal/ttcal.toml

# secrets in separate private files
printf '%s' 'YOUR-TIMETAGGER-API-TOKEN' > ~/.config/ttcal/timetagger-token
printf '%s' 'choose-a-long-password' > ~/.config/ttcal/caldav-password
chmod 600 ~/.config/ttcal/timetagger-token ~/.config/ttcal/caldav-password
```

`~/.config/ttcal/ttcal.toml`:

```toml
[timetagger]
api_url = "https://time.example.com/timetagger/api/v2"
token_file = "~/.config/ttcal/timetagger-token"

[caldav]
url = "https://cal.example.com/"
username = "caldav"
password_file = "~/.config/ttcal/caldav-password"
calendar = "TimeTagger"

[ttcal]
state_db = "~/.local/state/ttcal/ttcal.db"
backfill_days = 90
poll_interval = 60
log_level = "INFO"
```

If your TimeTagger instance does not issue API tokens, use `username` and
`password_file` instead of `token_file`; ttcal logs in through
`bootstrap_authentication`.

### First run

Run these **before** starting the service, so only one process touches the
calendar:

```bash
~/ttcal-venv/bin/ttcal -c ~/.config/ttcal/ttcal.toml doctor
~/ttcal-venv/bin/ttcal -c ~/.config/ttcal/ttcal.toml sync --dry-run
~/ttcal-venv/bin/ttcal -c ~/.config/ttcal/ttcal.toml sync
~/ttcal-venv/bin/ttcal -c ~/.config/ttcal/ttcal.toml status
```

Check the dry-run count before the real pass: it tells you how many events will
be created from the backfill horizon. The first real pass creates the
`TimeTagger` calendar in Radicale and writes those events.

## 4. Run ttcal under supervisord

`~/etc/services.d/ttcal.ini`:

```ini
[program:ttcal]
command=%(ENV_HOME)s/ttcal-venv/bin/ttcal -c %(ENV_HOME)s/.config/ttcal/ttcal.toml run
directory=%(ENV_HOME)s
autostart=yes
autorestart=true
startsecs=10
stdout_logfile=%(ENV_HOME)s/logs/ttcal.out.log
stderr_logfile=%(ENV_HOME)s/logs/ttcal.err.log
```

```bash
supervisorctl reread
supervisorctl update
supervisorctl status ttcal
supervisorctl tail -f ttcal stderr
```

A healthy log shows one startup line and then silence; a pass with changes logs
one summary line.

## 5. Add the calendar to your devices

Use the account you created with `htpasswd`:

- **macOS**: System Settings → Internet Accounts → Other → CalDAV, server
  `cal.example.com`, the `caldav` username and its password.
- **iOS**: Settings → Apps → Calendar → Accounts → Other → CalDAV, same values.
- **Thunderbird / DAVx5**: CalDAV URL `https://cal.example.com/`.

The `TimeTagger` calendar appears after the first sync. Press ⌘R in Calendar.app
to force a refresh.

## 6. Upgrades

```bash
cd ~/ttcal-src
git pull
~/ttcal-venv/bin/pip install --upgrade .
supervisorctl restart ttcal
~/ttcal-venv/bin/ttcal -c ~/.config/ttcal/ttcal.toml status
```

Back up `~/.local/state/ttcal/ttcal.db` before a version jump. It contains the
pairings and tombstones; `ttcal repair --rebuild` can restore pairings from
content but not tombstones or conflict history.

Radicale upgrades independently:

```bash
~/radicale-venv/bin/pip install --upgrade radicale bcrypt
supervisorctl restart radicale
```

## 7. Undo

```bash
supervisorctl stop ttcal
supervisorctl remove ttcal   # if the unit is no longer wanted
```

Removing the ttcal service stops mirroring; it does not delete records or
events. Removing the calendar in a client would delete the events and, on the
next pass, hide the corresponding records — stop the service first if you want
to keep the records untouched.

## Operating notes

- Keep exactly one ttcal process per state database and calendar.
- Run manual `ttcal sync` only while the service is stopped, or accept that two
  passes may race for a moment.
- The account's cron or watchdog can check liveness with
  `supervisorctl status ttcal`; ttcal itself retries failed passes and does not
  exit on transient network errors.
- Logs live in `~/logs`. `log_level = "DEBUG"` is for diagnosis, not permanent
  use.
