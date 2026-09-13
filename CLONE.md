# Dashboard Clone DB

Web internal untuk **clone** MariaDB production dan **pantau replikasi**. Bukan cutover, failover, atau migrasi Laravel.

URL: `http://localhost:8095` · service Docker: `docker compose up monitor` (terpisah dari backup Drive)

### Istilah singkat

| Istilah | Maksud |
|---------|--------|
| **Dashboard Clone DB** | Halaman web port 8095 di host (8090 di dalam container) |
| **Kredensial MySQL** | Hanya `.env` — lihat tabel di bagian Configuration |
| **User replikasi** | `CLONE_REPL_*` — akun di **master** `.94` untuk slave connect |

Nama env lama `MONITOR_*` masih didukung.

Topology:

```text
.95 Laravel  →  .94 PRODUCTION MASTER (read/write, stays up)
                     |
                     v
                .13.30 DATABASE CLONE
                     |
                     v
                .13.31 DATABASE CLONE
```

Backup-to-Drive (`backup.py`) is unchanged. This service only **reads** `.94` and may **write on clone hosts** after confirmation.

## What this is / is not

Does:

- Poll MariaDB 10.4 status (`SHOW SLAVE STATUS`, GTID, binlog, size)
- Show clone status: `CLONING`, `SYNCING`, `SYNCHRONIZED`, `LAGGING`, `ERROR`, `OFFLINE`, `UNKNOWN`
- Verify structure/size between source and clone
- Keep 7 days of metrics and deduplicated alerts
- Optionally run an initial dump+restore **on the clone**, then `CHANGE MASTER` / `START SLAVE` **on the clone only**

Does not:

- Stop, restart, or `RESET MASTER` on `.94`
- Turn `.94` into a replica
- Change Laravel database settings
- Promote clone / cut over production
- Delete binlogs
- `DROP DATABASE` on the source
- Invent progress percentages when size comparison is not reliable

## Assumptions

1. MariaDB **10.4.32** on all three database hosts. Queries use 10.4 names (`SHOW SLAVE STATUS`, `START SLAVE`, `CHANGE MASTER TO`), not MySQL 8 `REPLICA` syntax.
2. `.94` remains the only production writer. GTID/binlog/`server_id` are **read live**, never hard-coded.
3. Chain `.13.30 → .13.31` needs `log_bin=ON` and `log_slave_updates=ON` **on `172.21.13.30`**. If those are off, the UI blocks cloning to `.31` and does **not** restart `.94`. Apply `my.cnf` on clone host yourself.
4. OS metrics (CPU/RAM/disk) on Ubuntu require SSH. Windows `.94` shows `UNAVAILABLE` for host metrics; MariaDB metrics still work.
5. Initial clone runs over **SSH on the clone host** so ~45 GB does not pass through the Mac. If MariaDB is **only in Docker**, set **`docker_container`** on that clone in `config.yaml` (output of `docker ps --format '{{.Names}}'`). Dump/restore uses `docker exec … mariadb-dump` / `mariadb` inside the container. If SSH user is not in the `docker` group, set **`docker_sudo: true`**. The clone job runs `sudo -S docker …` using **`MONITOR_SSH_PASSWORD`** (same as SSH login) unless you set **`CLONE_SSH_SUDO_PASSWORD_<NODE>`**. Alternatively use passwordless sudo for docker or add the user to the `docker` group. Dump runs in the container; import uses **`127.0.0.1:3306`** on the host (published Docker port). Without `docker_container`, the host needs `mariadb-client` and import via `local_mysql_host` (default `127.0.0.1:3306`).
6. The replication account on the upstream is created **manually**. This app does not `CREATE USER` on production.
7. `information_schema.table_rows` is an InnoDB estimate. Verification uses it as a hint, not a checksum.
8. Per-poll inventory is whatever exists on the server that moment. The old “44.7 GB / kocak_2 13.90 GB” list is a reference only.

## Privileges

Monitoring user (source and clones):

```sql
GRANT SELECT, REPLICATION CLIENT, PROCESS, SHOW VIEW
ON *.* TO 'monitor'@'<monitor-ip>' IDENTIFIED BY '...';
```

On clones, the same user also needs dump restore rights if you use it for import (`CREATE`, `INSERT`, `DROP` on application schemas — clone host only).

Replication user (on **upstream**, created by you):

```sql
GRANT REPLICATION SLAVE, REPLICATION CLIENT
ON *.* TO 'repl'@'172.21.13.30' IDENTIFIED BY '...';
-- and from .31 to .30 when chaining
FLUSH PRIVILEGES;
```

Put that user in `.env` as `CLONE_REPL_USER` / `CLONE_REPL_PASSWORD`.

## Configuration

Add a `topology` block to `config.yaml` (see `config.example.yaml`). Source can reuse a backup `connections[]` entry via `connection: mypas`.

**User/password MySQL tidak disimpan di `config.yaml`** — hanya di `.env` (service `backup` + `monitor` memakai `env_file: .env`).

| Target | Variabel `.env` |
|--------|------------------|
| Backup + master via `connection: mypas` | `MYSQL_USER_MYPAS`, `MYSQL_PASSWORD_MYPAS` |
| Clone `clone-13-30` | `CLONE_MYSQL_USER_CLONE_13_30`, `CLONE_MYSQL_PASSWORD_CLONE_13_30` |
| Clone `clone-13-31` | `CLONE_MYSQL_USER_CLONE_13_31`, `CLONE_MYSQL_PASSWORD_CLONE_13_31` |
| Replikasi ke master | `CLONE_REPL_USER`, `CLONE_REPL_PASSWORD` |
| SSH ke clone | `CLONE_SSH_PASSWORD` |

Nama env = prefix + **nama node/connection** dari YAML (huruf besar, `-` jadi `_`). `MONITOR_*` / password di YAML masih didukung sebagai fallback, tidak disarankan.

`docker compose` bind-mount config via `monitor-config/`. Setelah ubah **`.env`** atau topology: `docker compose up -d --force-recreate monitor` (`restart` saja **tidak** memuat ulang variabel dari `.env`). Backup Drive: `./scripts/load-secrets.sh` jika perlu.

**Monitor `Restarting (1)` / `Permission denied` pada config:** Jangan pakai `/scripts/...` — jalankan **`./scripts/setup-monitor-config.sh`** dari folder repo. Dashboard mount **`monitor-config/config.yaml`** (bukan bind file `config.yaml` langsung). Lalu `docker compose up -d --force-recreate monitor`. SELinux: `chmod 644 monitor-config/config.yaml` dan `chcon -Rt container_file_t monitor-config`. Cek: `docker compose run --rm --no-deps monitor cat /app/monitor-config/config.yaml | head -3`. `./scripts/diagnose-monitor.sh` untuk ringkasan.

**`config.yaml` jadi folder / mount error “not a directory”:** Jika pernah `docker compose up` tanpa file `config.yaml`, Docker bisa membuat **folder** `config.yaml`. Hapus folder itu, buat file (`cp config.example.yaml config.yaml`). Dashboard mount ke `/app/monitor-config.yaml` (bukan `/app/config/`) supaya tidak bentrok dengan volume backup. Pastikan `file config.yaml` = **regular file**. Setelah `git pull`, `docker compose up -d --build monitor`.

Local run without Docker:

```bash
python3 -m pip install -r requirements.txt
python3 -m monitor
```

## Clone job safety

Start clone is disabled until:

- Target role is `clone` and host is **not** the source IP
- You type the clone hostname/name
- Pre-check passes (connectivity, 10.4.x, disk, SSH, replication user)
- You confirm replace if the clone already has application databases

`DROP DATABASE` / `CHANGE MASTER` / `START SLAVE` run only on the clone. Source connections only execute `SELECT` and `SHOW`.

Progress: while restore runs, the monitor compares clone `information_schema` size vs source size. If that ratio is not trustworthy, the UI shows `INITIAL CLONE RUNNING` without a fake percent.

## Alerts

`SOURCE_DOWN`, `CLONE_DOWN`, `REPLICATION_STOPPED`, `REPLICATION_ERROR`, `REPLICATION_LAG`, `BINLOG_RETENTION_RISK`, `DISK_LOW`, `CPU_HIGH`, `RAM_HIGH`, `DATABASE_GROWTH`.

Same `(type, server, fingerprint)` is suppressed during `alert_cooldown_minutes` (default 30). Binlog risk compares replica lag to source `expire_logs_days`. The tool never purges logs.

## Out of scope for this delivery

Full login UI, RBAC, and a separate admin page for Start/Stop/Configure Replication. Clone start requires typing the clone hostname plus an audit row (no passwords stored).
