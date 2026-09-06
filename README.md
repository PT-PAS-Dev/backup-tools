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

Token Google (`token.json`) dan `config.yaml` yang sudah jalan bisa dipakai lagi.

```bash
docker compose up -d --build
docker compose exec backup python /app/backup.py
```

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

- `.env` — `GDRIVE_FOLDER_ID`, retensi
- `config.yaml` — host MySQL, user, password, database
- `oauth-client.json` + `token.json` — OAuth

Password yang mengandung `@` harus diapit tanda kutip. Samakan dengan yang dipakai aplikasi (misalnya `MAGANG_DB_PASSWORD` di newpas).

## Privilege MySQL

User harus bisa login dari IP Docker/Mac ke server. Privilege: `SELECT`, `SHOW VIEW`, `TRIGGER`, `EVENT`, `LOCK TABLES`.
