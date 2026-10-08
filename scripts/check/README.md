# Pemeriksaan pengembangan

Dari root repository, jalankan:

```powershell
.\.venv\Scripts\python.exe scripts/check/run.py
```

Script menjalankan lint Ruff, pemeriksaan format, lalu pytest. Exit code nonzero menghentikan rangkaian ketika ada kegagalan. Dependensi pengembangan dipasang melalui `pip install -e ".[dev]"`.

Fixture test disimpan di `.goodhands/test-fixtures/` agar memakai direktori workspace dan ACL Windows biasa. Cache pytest dinonaktifkan untuk kompatibilitas sandbox. Fixture dipertahankan untuk diagnosis dan tidak masuk Git. Test pembuatan symlink dapat dilewati jika akun Windows tidak memiliki izin tersebut.

Test provider memakai HTTP mock; tidak perlu API key, saldo provider atau koneksi inference. Test workflow memakai simulator model tetapi menjalankan command Python nyata. Pengujian Docker dan model live dilakukan terpisah setelah engine, image dan kredensial tersedia.
