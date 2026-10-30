# Installing ttcal

ttcal is a single Python process. It needs a TimeTagger instance, a CalDAV
calendar and a place to keep a small SQLite file. It does not host or install
TimeTagger or the calendar server.

## 1. Prerequisites

- Python **3.11+** (or Docker).
- **TimeTagger API access.** In the TimeTagger web app open the menu and create
  an API token. The API base URL is the address of the app plus
  `/api/v2` — for a self-hosted instance that usually serves the app under
  `/timetagger/` this is `https://host/timetagger/api/v2`.
- **A CalDAV account.** [Radicale](https://radicale.org/) is the tested server
  and is easy to run next to TimeTagger; any CalDAV server that supports
  calendar collections and either sync-token or ETag listings works. Give ttcal
  a dedicated user or an app password, not your main login.
- **TLS.** Apple clients require HTTPS with a valid certificate. Plain HTTP is
  fine for local testing only.

## 2. Install the command

### pipx (recommended for a single user)

```bash
git clone https://github.com/your-name/ttcal.git
cd ttcal
pipx install .
ttcal --version
```

To upgrade later: `pipx install --force .` from the updated checkout.

### uv

```bash
uv tool install /path/to/ttcal
ttcal --version
```

### Virtual environment

```bash
python3 -m venv /opt/ttcal/venv
/opt/ttcal/venv/bin/pip install /path/to/ttcal
/opt/ttcal/venv/bin/ttcal --version
```

### Docker

A `Dockerfile` is included. Build and run:

```bash
docker build -t ttcal .
docker run -d --name ttcal \
  -v /srv/ttcal:/config:ro \
  -v /srv/ttcal-data:/data \
  ttcal
```

The image expects the configuration at `/config/ttcal.toml` and writes the
state database to `/data` (set `state_db = "/data/ttcal.db"`). Keep the config
read-only and `0600`.

With Compose:

```yaml
services:
  ttcal:
    build: .
    restart: unless-stopped
    volumes:
      - ./ttcal.toml:/config/ttcal.toml:ro
      - ttcal-data:/data
volumes:
  ttcal-data:
```

## 3. Configure

```bash
mkdir -p ~/.config/ttcal
cp ttcal.example.toml ~/.config/ttcal/ttcal.toml
chmod 600 ~/.config/ttcal/ttcal.toml
$EDITOR ~/.config/ttcal/ttcal.toml
```

Fill in `[timetagger]` and `[caldav]`. Every key and its default is documented
in [configuration.md](configuration.md).

## 4. Provide a CalDAV server

If you already have a CalDAV account, skip this section.

### Radicale in five minutes

Install Radicale where it can keep state (the tested version is 3.x):

```bash
python3 -m venv ~/radicale-venv
# If this reports "No such file or directory", the interpreter has no
# ensurepip; bootstrap it with https://bootstrap.pypa.io/get-pip.py.
~/radicale-venv/bin/pip install radicale bcrypt
mkdir -p ~/.config/radicale ~/radicale-data/collections
htpasswd -nbB calendar 'choose-a-password' > ~/.config/radicale/users
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

Run it under your service manager (see below), then point ttcal's `[caldav]`
section at it. On first sync ttcal creates the calendar; the client account must
be allowed to create collections.

`127.0.0.1` is correct when the reverse proxy runs on the same host. If the
proxy reaches the app over a network interface instead — Uberspace works this
way — bind `0.0.0.0` (or `::`) and keep the port closed to the outside.

### Reverse proxy

CalDAV discovery assumes the calendar server lives at the **root of its own
host** (`/.well-known/caldav`, then `PROPFIND /`). A dedicated subdomain such as
`cal.example.com` avoids path-prefix problems.

Caddy:

```
cal.example.com {
    reverse_proxy 127.0.0.1:5232
}
```

nginx:

```nginx
server {
    listen 443 ssl http2;
    server_name cal.example.com;
    # ssl_certificate ...; ssl_certificate_key ...;

    client_max_body_size 20m;
    location / {
        proxy_pass http://127.0.0.1:5232;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Do not put CalDAV under a path prefix unless your client supports it. Apple
Calendar does not discover collections reliably there.

### Example: Uberspace

Uberspace is one hosting option, not a requirement. The shape is the same as
above, except that Radicale must listen on the account's network interface, not
on loopback, because the Uberspace proxy connects from outside `127.0.0.1`.

```bash
# Radicale in a virtualenv, listening on the account's interface
python3.14 -m venv ~/radicale-venv   # any 3.11+ interpreter
~/radicale-venv/bin/pip install --upgrade radicale bcrypt
mkdir -p ~/.config/radicale ~/radicale-data/collections
htpasswd -nbB calendar 'choose-a-password' > ~/.config/radicale/users
chmod 600 ~/.config/radicale/users
# then create ~/.config/radicale/config as shown above, but with
#   hosts = 0.0.0.0:5232
#   filesystem_folder = /home/USER/radicale-data/collections

# install ttcal for the account
python3.14 -m venv ~/ttcal-venv
~/ttcal-venv/bin/pip install /path/to/ttcal

# expose the calendar on its own subdomain root
uberspace web domain add cal.example.com
uberspace web backend set cal.example.com/ --http --port 5232
```

The backend target is `DOMAIN/PATH`; there is no `--domain` option. Use
`uberspace web backend list` to confirm it reports `OK, listening`. A complete
walkthrough, including Radicale and ttcal under supervisord, is in
[deploy-uberspace.md](deploy-uberspace.md).

## 5. Run it as a service

### systemd (Linux)

Create a dedicated user and directories:

```bash
sudo useradd --system --create-home --home-dir /var/lib/ttcal ttcal
sudo -u ttcal mkdir -p /var/lib/ttcal/.config/ttcal
sudo -u ttcal cp ttcal.example.toml /var/lib/ttcal/.config/ttcal/ttcal.toml
sudo chmod 600 /var/lib/ttcal/.config/ttcal/ttcal.toml
# edit the config; set state_db = "/var/lib/ttcal/ttcal.db"
```

`/etc/systemd/system/ttcal.service`:

```ini
[Unit]
Description=ttcal TimeTagger to CalDAV mirror
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=ttcal
Group=ttcal
WorkingDirectory=/var/lib/ttcal
ExecStart=/usr/local/bin/ttcal -c /var/lib/ttcal/.config/ttcal/ttcal.toml run
Restart=on-failure
RestartSec=10

# hardening; keep the config and state inside the writable home
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/ttcal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ttcal
journalctl -u ttcal -f
```

If ttcal is installed with pipx for a normal user, use a **user** unit instead
(`~/.config/systemd/user/ttcal.service`, `systemctl --user enable --now ttcal`,
and `loginctl enable-linger USER` so it survives logout).

### supervisord (Uberspace example)

`~/etc/services.d/ttcal.ini`:

```ini
[program:ttcal]
command=%(ENV_HOME)s/.local/bin/ttcal -c %(ENV_HOME)s/.config/ttcal/ttcal.toml run
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
supervisorctl restart ttcal
supervisorctl tail -f ttcal stderr
```

Any supervisor that restarts a crashed process works; ttcal keeps all state in
SQLite, so a restart is always safe.

## 6. Verify

```bash
ttcal doctor          # config, database, TimeTagger and CalDAV reachable
ttcal sync --dry-run  # shows what the first pass would do, writes nothing
ttcal sync            # creates the calendar and backfills
ttcal status          # pairs, last sync, conflicts, skipped events
```

Then add the calendar to a client (see [usage.md](usage.md)) and check that an
event you create appears in TimeTagger within a minute. Before relying on the
mirror, run the [acceptance checklist](acceptance.md).

## 7. Upgrade

```bash
# pipx / uv
pipx install --force /path/to/ttcal
# then restart the service
sudo systemctl restart ttcal
```

The state database schema is created with `CREATE TABLE IF NOT EXISTS` and is
forward compatible within the 0.x series. Take a copy of the database before
upgrading between major versions; `ttcal repair --rebuild` can reconstruct pair
state from the two sides if needed.
