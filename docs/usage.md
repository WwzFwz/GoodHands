# Penggunaan GoodHands MVP

Implementasi per 9 Oktober 2026 menggunakan Python, Pydantic, HTTPX dan SQLite. Jalankan CLI melalui `python -m goodhands` dari repository ini, atau executable `goodhands` setelah instalasi. Role bekerja berurutan dan semua hasil tersimpan di `.goodhands/` dalam repository target.

## Konfigurasi proyek

Jalankan `goodhands init --project PATH` untuk membuat `goodhands.toml` dan contoh `task.json`. Perintah menolak overwrite. Edit kedua file sebelum menjalankan model live; contoh task default hanya cocok untuk calculator yang disediakan.

| Bagian konfigurasi | Fungsi |
| --- | --- |
| `provider` | Jenis adapter, base URL, nama environment variable untuk key dan timeout |
| `profiles.default` | Model ID, batas output token dan reservasi biaya per request |
| `profiles.strong` | Profil tambahan opsional untuk role sulit atau eskalasi |
| `role_profiles` | Pemetaan role ke profil; role yang tidak dipetakan memakai default |
| `policy` | Budget total, batas call/turn/repair/waktu, batas karakter konteks dan kebijakan reviewer |
| `runner` | Docker atau eksekusi lokal yang diizinkan secara eksplisit |
| `checks` | Command pemeriksaan bernama dalam bentuk array argumen, bukan string shell |

`task.json` menyimpan goal, acceptance criteria ber-ID, daftar check terkait, constraint, path yang boleh diedit, path test yang dilindungi, path untuk test baru, risiko, kompleksitas dan skill. ID check pada setiap criterion wajib merujuk command yang sudah dikonfigurasi pengguna. Agent tidak bisa mengganti command atau melemahkan criterion dalam run tersebut.

Kualitas kriteria penerimaan tetap penting: exit code nol hanya membuktikan pemeriksaan yang dikonfigurasi berhasil. Tentukan check yang benar-benar menguji kebutuhan, bukan sekadar perintah yang selalu berhasil. Test yang ditambahkan Tester harus tercakup oleh command discovery Anda; tambahkan `__init__.py` bila framework discovery memerlukannya pada subfolder.

Path permission menggunakan glob relatif repository, contohnya `src/**`, `calculator.py`, `tests/generated/**`. Backslash dinormalisasi. Pada Windows pencocokan izin tidak membedakan kapitalisasi. File konfigurasi GoodHands, `.git`, `.env*`, direktori kredensial yang dikenal dan file key tidak disertakan dalam snapshot. Daftar ini bukan pendeteksi seluruh rahasia; pilih repository target yang layak dikirim ke provider.

## API dan model

Default adalah OpenRouter dengan base URL `https://openrouter.ai/api/v1` dan environment variable `OPENROUTER_API_KEY`. `init --provider vercel` menyiapkan adapter kompatibel ke `https://ai-gateway.vercel.sh/v1` dengan `AI_GATEWAY_API_KEY`. Hanya satu gateway per konfigurasi; tidak diperlukan akun pada keduanya.

Model ID sengaja tidak dikunci ke merek atau versi tertentu. Pilih model yang mendukung tool calling, lalu bandingkan keberhasilannya pada task nyata. Adapter meminta provider OpenRouter mendukung parameter yang dikirim. Artefak akhir dikirim melalui tool `submit_result` dan divalidasi lokal terhadap skema Pydantic; tidak bergantung pada model menghasilkan JSON bebas yang selalu benar.

Provider lain dapat memakai `kind = "compatible"` jika mengikuti format Chat Completions yang didukung. Adapter generik ini memerlukan HTTPS. Ollama lokal, streaming, multimodal dan reasoning parameter khusus belum tersedia sebagai integrasi siap pakai.

Key hanya dibaca dari environment pada pemanggilan API, tidak disalin ke checkpoint dan tidak diwariskan secara otomatis ke test runner. Jangan menyimpan key dalam task, skill atau prompt. `doctor` hanya menampilkan apakah key tersedia, tanpa menampilkan nilainya atau mengirim request inference.

Rujukan protokol: [API OpenRouter](https://openrouter.ai/docs/api_reference/overview), [usage accounting OpenRouter](https://openrouter.ai/docs/cookbook/administration/usage-accounting), dan [Chat Completions Vercel](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions).

## Preset dan role mandiri

```powershell
goodhands run --project PATH --task task.json --preset quick
goodhands run --project PATH --task task.json --preset feature
goodhands run --project PATH --task task.json --preset system
goodhands run --project PATH --task task.json --role consultant
```

- `quick`: Consultant, Coder, check otomatis, Reviewer sesuai risiko, dokumentasi opsional dan pemeriksaan akhir.
- `feature`: menambahkan Code Architect, Tester awal jika kompleks, serta analisis Tester setelah eksekusi check.
- `system`: menambahkan System Architect. `architecture_required` dalam task juga dapat memicunya.
- `--role`: hanya role yang diminta, dengan output `report_ready`. Tester mandiri menjalankan check sebelum menganalisis bukti. Hasil mode mandiri tidak dapat langsung diterapkan melalui `apply`.

Reviewer wajib untuk risk medium/high atau policy `reviewer = "always"`. Review rencana berjalan pada risk high atau kebijakan always. Risiko dan kompleksitas berasal dari task pengguna; model tidak memilih untuk melewati gate.

Setelah snapshot berubah, Controller memperbarui konteks Consultant sebelum role lanjutan yang membutuhkannya. Ini konservatif: belum ada indeks dependensi konteks yang dapat membatasi refresh hanya ke simbol terdampak. Agent tetap dapat membaca sumber langsung.

Default repair budget adalah dua putaran. Setelah diagnosis biasa buntu, Controller memanggil satu sesi Debugger, lalu memberi satu kesempatan implementasi berdasarkan diagnosis. Jika masih gagal, run diblokir. Profil eskalasi opsional berlaku untuk Debugger/Coder setelah pemicu tersebut; tidak ada loop tanpa batas.

## Runner dan lingkungan uji

Docker adalah default. Jalankan engine dan sediakan image sebelum run:

```powershell
docker pull python:3.12-slim
```

Image contoh cukup untuk test Python standard library. Proyek dengan dependency lain memerlukan image yang sudah memuat dependency tersebut. Container dijalankan tanpa jaringan, dengan root filesystem dan mount source hanya-baca, tmpfs sementara, serta batas proses/memori/CPU. Command yang membutuhkan cache atau output build harus diarahkan ke `/tmp`. Runner tidak memasang dependency atau mengunduh image diam-diam.

Untuk proyek terpercaya, ubah `runner.kind = "local"` dan gunakan `--allow-local-exec` pada run maupun resume. Local runner bukan sandbox: command tetap bisa membaca/menulis di host sesuai izin proses. Command selalu berasal dari konfigurasi pengguna, bukan string shell buatan model. Alias `python`/`python3` memakai interpreter harness; tetapkan path interpreter proyek secara eksplisit jika dependency berbeda.

Runner mencatat argv, exit code, durasi, timeout, output terbatas dan snapshot. Perubahan source selama check membuat bukti gagal. Pemeriksaan yang timeout atau melewati batas log juga tidak dapat dianggap lulus. Pengujian sebenarnya tidak dijalankan oleh LLM.

## Role dan skill yang bisa diganti

Default role ada di `goodhands/defaults/agents/NAMA_ROLE/README.md`. Jalankan `goodhands agents init` untuk menyalin template ke `agents/`, atau gunakan `goodhands agents configure --role coder --request "instruksi biasa"` agar Configurator menuliskannya. Lihat [panduan Configurator](agent-configuration.md) untuk format, subfolder skill, validasi, kompilasi JSON, dan penerapan draft. `roles_dir` dan `skills_dir` dapat diatur sebagai key top-level TOML; JSON per role lama tetap didukung tanpa duplikasi dengan sumber Markdown.

Override role dapat mempersempit tool, tetapi tidak memperluas batas izin bawaan. Reviewer tidak bisa diberi tool menulis, dan Debugger tidak dapat membuat patch produk. Menonaktifkan role yang diperlukan preset ditolak; jika Debugger dinonaktifkan, run yang membutuhkan diagnosis akan berhenti dengan kendala.

Skill berupa Markdown, dipilih melalui `task.skills` atau `role.skills`. Nama sederhana seperti `python` mencari skill lokal/bawaan. Referensi `.md` dalam README relatif terhadap folder role dan dapat menunjuk subfolder atau skill bersama di dalam proyek; dalam task/JSON legacy path tersebut relatif terhadap proyek. Isi role/skill disalin ke checkpoint ketika run dibuat agar perubahan file di tengah run tidak mengubah instruksi yang sedang dieksekusi. Plugin Python arbitrer dan penambahan jenis role/tool baru melalui konfigurasi belum didukung; antarmuka Python internal dapat diperluas melalui perubahan kode yang ditinjau.

## Biaya dan batas

Sebelum setiap request live, harness menyisihkan `request_reserve_usd`. Setelah respons, `usage.cost` dipakai jika tersedia. Jika tidak tersedia, harga token yang Anda konfigurasi dapat menghasilkan estimasi. Jika keduanya tidak tersedia atau outcome jaringan tidak diketahui, reservasi tetap dihitung sebagai biaya yang belum diketahui; tidak dianggap nol.

Reservasi adalah batas aplikasi yang konservatif, bukan jaminan tagihan provider. Sesuaikan nilainya dengan konteks, output limit dan harga model. Tagihan nyata dapat melampaui estimasi; request yang sedang berlangsung tidak dapat dibatalkan hanya karena biaya aktual baru dilaporkan belakangan. Gunakan juga batas pengeluaran provider jika tersedia.

Kegagalan HTTP tidak diulang otomatis oleh harness. Checkpoint mempertahankan reservasi dan pengguna dapat melakukan resume secara sengaja. Routing/fallback provider internal OpenRouter tetap mengikuti layanan gateway; fallback lintas gateway belum diimplementasikan.

## Resume dan hasil yang tidak diketahui

```powershell
goodhands resume RUN_ID --project PATH --allow-local-exec
goodhands resume RUN_ID --project PATH --feedback-file clarification.txt --allow-local-exec
```

Resume mempertahankan config, task, role, artefak dan counter run, serta melanjutkan tahap yang belum selesai. Role yang terputus memulai kembali sesi role tersebut dari snapshot terakhir; histori percakapan lengkap tidak direplay. Perubahan file memakai hash agar aksi yang sudah terjadi tidak ditimpa dengan asumsi lama.

Jika snapshot berubah di luar harness atau outcome tool tidak diketahui, inspect `diff` dan log dahulu. `resume --reconcile` menerima workspace yang sudah ditinjau, membatalkan bukti lama dan mengulang perencanaan. Perintah ini tidak mengembalikan budget/repair counter ke nol. Task atau acceptance criteria yang berubah memerlukan run baru.

Tambahan anggaran bersifat eksplisit melalui `--budget-usd`, `--max-model-calls`, atau `--additional-seconds`. Batas tersebut tidak menghapus limit repair yang sudah terpakai. Run demo tidak di-resume memakai provider live.

## Artefak dan penerapan patch

```text
TARGET/.goodhands/
  state.db
  execution.lock
  runs/RUN_ID/
    base/
    workspace/
    artifacts/*.json
    events.jsonl
    apply-backup/
```

`base` menyimpan snapshot awal. `workspace` memuat hasil kerja. Artefak menyimpan konteks, kontrak, rencana pengujian, review, diagnosis dan bukti runner. `events.jsonl` mencatat perpindahan tahap, tool dan accounting model. Folder state tidak perlu di-commit dan dapat mengandung salinan kode proyek.

`apply` menolak perubahan jika source snapshot berbeda dari baseline, termasuk file dependensi yang tidak disentuh patch. Ini lebih konservatif daripada hanya memeriksa file yang berubah, tetapi mencegah bukti test lama diterapkan pada source yang berbeda. Backup disimpan sebelum perubahan; error biasa mengembalikan file yang sudah disentuh. Penerapan banyak file belum merupakan transaksi filesystem tahan mati listrik; setelah proses apply terputus, tinjau backup dan source sebelum tindakan selanjutnya.

## Batas MVP yang masih terbuka

- Model live belum divalidasi pada proyek Anda; mock HTTP menguji kontrak adapter, bukan kemampuan coding model.
- Runner Docker memerlukan engine yang dapat diakses dan image siap pakai; pengujian lokal tidak membuktikan setup container pengguna.
- Batas snapshot: 10.000 file, 2 MB per file dan 100 MB total. Direktori dependency umum dikecualikan; `.gitignore` khusus proyek belum menjadi mesin pengecualian umum.
- Eksekusi masih sinkron dan berurutan. Tidak ada streaming, UI, worker terdistribusi atau merge antarcoder.
- Context budget memakai jumlah karakter, bukan tokenizer per model. Melebihi batas menghentikan stage; tidak ada pemotongan requirement diam-diam.
- Klaim semantik Reviewer dan Tester tetap membutuhkan evaluasi manusia dan check yang baik. Simulator tidak menggantikan benchmark kualitas model.
