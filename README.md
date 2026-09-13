# MySQL Periodic Backup ke Google Drive (Docker)

Dump MySQL **satu file per tabel** setiap jam **00.00 Asia/Jakarta**, lalu unggah ke Google Drive (OAuth akun kantor).

```text
<folder Drive>
  06092026/
    kas/
      failed_jobs.sql
      kas.sql
    backup.log
```

## Setup

`.env` dan `config.yaml` **tidak** di-git. Salin dari contoh lalu isi di setiap host:

```bash
cp .env.example .env
cp config.example.yaml config.yaml
# oauth-client.json + token.json dari Google OAuth
./scripts/load-secrets.sh
docker compose up -d --build
docker compose exec backup python /app/backup.py
```

`load-secrets.sh` menyalin `config.yaml`, `oauth-client.json`, dan `token.json` ke Docker volume (bukan bind-mount dari NFS/home, supaya tidak kena `Permission denied`).

Setelah mengubah `config.yaml` atau OAuth di host, jalankan lagi `./scripts/load-secrets.sh` lalu `docker compose restart backup`.

Pilih database tertentu:

```bash
docker compose exec backup python /app/backup.py --list
docker compose exec backup python /app/backup.py -d lab_report
docker compose exec backup python /app/backup.py -d kocak_2 -d app_mixer_v2_4
docker compose exec -it backup python /app/backup.py --pick
```

Jadwal `0 0 * * *` (00.00 WIB) tetap mem-backup **semua** database di `config.yaml`.

Login Google ulang (hanya jika token hilang):

```bash
docker compose run --rm -p 8080:8080 backup python /app/auth.py
```

Buka URL yang tercetak di browser Mac.

Log: `docker compose logs -f backup`

## Incremental

Default `INCREMENTAL=1`. Setiap tabel dicek dulu lewat `SHOW TABLE STATUS` (ukuran, jumlah baris, auto_increment, waktu update). 

- Tabel **berubah**: di-dump ulang lalu diunggah.
- Tabel **tidak berubah**: tidak di-`SELECT *` (hemat server); file kemarin disalin di Drive ke folder tanggal hari ini supaya restore tetap lengkap.

Paksa dump semua:

```bash
docker compose exec -e INCREMENTAL=0 backup python /app/backup.py
```

Ini bukan binlog MySQL. Kalau InnoDB tidak mengisi `UPDATE_TIME` dan update baris tidak mengubah ukuran tabel, ada kemungkinan kecil perubahan terlewat. Untuk `kas` ukurannya kecil, risikonya rendah.

## File config

- `.env` — `GDRIVE_FOLDER_ID`, retensi (dibaca Docker di host)
- `config.yaml` — host MySQL, user, password, database
- `oauth-client.json` + `token.json` — OAuth

Di container, tiga file terakhir disimpan di volume `backup-config` (`/app/config/`), diisi lewat `./scripts/load-secrets.sh`.

Password yang mengandung `@` harus diapit tanda kutip. Samakan dengan yang dipakai aplikasi (misalnya `MAGANG_DB_PASSWORD` di newpas).

## Privilege MySQL

User harus bisa login dari IP Docker/Mac ke server. Privilege: `SELECT`, `SHOW VIEW`, `TRIGGER`, `EVENT`, `LOCK TABLES`.

## Dashboard Clone DB

Web **pemantauan clone & replikasi** (bukan cutover) di port **8095** (host): topology `.94 → .13.30 → .13.31`, koneksi MySQL, lag, verifikasi, riwayat, peringatan, dan start clone per database.

Lihat [CLONE.md](CLONE.md). Di server (pertama kali):

```bash
cp .env.example .env          # isi GDRIVE_*, CLONE_* (lihat CLONE.md)
cp config.example.yaml config.yaml   # edit topology + connections
./scripts/setup-monitor-config.sh    # salin ke monitor-config/ (fallback mount)
docker compose up -d --build
# buka http://<server>:8095
```

File `.env` tidak di-git; tanpa `.env`, `docker compose` versi lama gagal — buat dari `.env.example`.

Dashboard membaca `config.yaml` di host (bind-mount). Backup Drive tetap terpisah; setelah ubah secret backup jalankan `./scripts/load-secrets.sh`.
