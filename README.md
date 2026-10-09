# GoodHands

GoodHands adalah harness pengembangan software lokal dengan role yang bisa dikonfigurasi: Consultant, System Architect, Code Architect, Coder, Tester, Reviewer, Debugger, dan Documentor. Satu Coder bekerja pada salinan repository. Workflow hanya menerima hasil setelah pemeriksaan yang ditentukan pengguna lulus pada snapshot akhir.

MVP menyediakan CLI, adapter OpenRouter dan Chat Completions kompatibel, kontrak Pydantic, checkpoint SQLite, artefak JSON, batas biaya/iterasi, review sesuai risiko, diagnosis saat buntu, serta diff dan penerapan patch terpisah. Belum ada UI web atau beberapa Coder paralel.

## Mulai tanpa API

Python 3.11 atau lebih baru diperlukan. Di PowerShell, dari folder repository:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\goodhands.exe demo
```

Untuk workspace ini, `.venv` sudah disiapkan dan paket sudah dipasang; dua perintah setup pertama tidak perlu diulang. Pada Linux/macOS, executable berada di `.venv/bin/`.

Demo tidak menggunakan API atau kredit: respons model disimulasikan secara deterministik, sedangkan pembacaan file, perubahan patch dan test Python benar-benar dijalankan. Statusnya `completed_simulated`, sehingga hasil demo tidak dianggap bukti kualitas model sungguhan. Setiap demo default memakai direktori baru di `.goodhands/demos/` dan tidak mengubah file asalnya.

Untuk menguji loop kegagalan dan Debugger:

```powershell
.\.venv\Scripts\goodhands.exe demo --exercise-recovery
```

## Menggunakan model sungguhan

Mulai dari proyek contoh yang disediakan:

```powershell
.\.venv\Scripts\goodhands.exe init --project examples/calculator --provider openrouter --model "MODEL_ID_PILIHAN_ANDA" --runner local
```

Ganti model ID dengan model yang tersedia dan mendukung tool calling. `init` membuat `goodhands.toml` dan `task.json`, serta menolak menimpa file yang sudah ada. Contoh task yang dibuat cocok dengan proyek calculator; pada repository lain, ubah tujuan, acceptance criteria, path dan command pemeriksaannya terlebih dahulu.

Masukkan API key melalui prompt lokal PowerShell agar tidak tertulis sebagai literal dalam riwayat perintah:

```powershell
$apiCredential = Read-Host "OpenRouter API key" -AsSecureString
$env:OPENROUTER_API_KEY = [System.Net.NetworkCredential]::new("", $apiCredential).Password
.\.venv\Scripts\goodhands.exe doctor --project examples/calculator
.\.venv\Scripts\goodhands.exe run --project examples/calculator --task task.json --allow-local-exec
```

`--allow-local-exec` mengizinkan command proyek berjalan pada komputer Anda. Gunakan pada repository dan command yang dipercaya; salinan workspace tidak membatasi akses proses ke host. Default `init` tanpa `--runner local` memakai Docker. Detail setup runner dan API ada di [panduan penggunaan](docs/usage.md).

OpenRouter adalah default; Vercel AI Gateway hanya alternatif koneksi model. Aplikasi berjalan lokal dan tidak memerlukan hosting Vercel. Akses model live membutuhkan akun/API key dan kuota atau kredit provider. Demo dan test lokal tidak memakai kredit inference; tidak perlu membeli GPU atau server untuk jalur API ini.

Adapter memakai [tool calling OpenRouter](https://openrouter.ai/docs/guides/features/tool-calling) dan menyediakan konfigurasi [Chat Completions Vercel AI Gateway](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions). Ketersediaan model dan biaya dicek pada provider ketika dipakai.

## Melihat dan menerapkan hasil

Ganti `RUN_ID` dengan ID pada output run; `--project` selalu menunjuk repository target yang sama.

```powershell
.\.venv\Scripts\goodhands.exe runs --project examples/calculator
.\.venv\Scripts\goodhands.exe show RUN_ID --project examples/calculator
.\.venv\Scripts\goodhands.exe diff RUN_ID --project examples/calculator
.\.venv\Scripts\goodhands.exe apply RUN_ID --project examples/calculator
```

`apply` hanya menerima workflow selesai, memeriksa snapshot hasil verifikasi, menolak jika repository sumber berubah sejak run dibuat, dan menyimpan backup. GoodHands tidak melakukan commit, push, atau deployment otomatis.

## Mengatur agent tanpa menghafal format

Configurator adalah agent khusus untuk menulis konfigurasi, terpisah dari workflow coding.
Setelah model dan API key diatur, berikan instruksi biasa:

```powershell
.\.venv\Scripts\goodhands.exe agents configure --project examples/calculator --role coder --request "Ikuti kontrak Code Architect, gunakan OOP bila relevan, dan hindari abstraksi berlebihan."
```

Output menampilkan draft README/skill, diff, biaya, dan perintah `agents apply` untuk
menerapkannya. Tambahkan `--apply` jika ingin hasil valid langsung diterapkan. Jika maksud
permintaan belum jelas, Configurator mengeluarkan pertanyaan. Compiler dan validator bekerja
tanpa LLM; hanya penulisan draft oleh Configurator yang memakai inference.

Untuk mencoba tanpa biaya: `.\.venv\Scripts\goodhands.exe agents demo`. Untuk mengedit sendiri:
jalankan `agents init`, lalu edit `agents/NAMA_ROLE/README.md` dan jalankan `agents validate`.
Detail format, command, dan batas tersedia di [panduan konfigurasi agent](docs/agent-configuration.md).

## Dokumentasi

- [Workflow dan pemilik keputusan](docs/agent-workflow.md)
- [Arsitektur dan trade-off](docs/harness-architecture.md)
- [Penggunaan, konfigurasi dan batas MVP](docs/usage.md)
- [Configurator dan folder Markdown per agent](docs/agent-configuration.md)
- [Menjalankan pemeriksaan pengembangan](scripts/check/README.md)

## Pemeriksaan pengembangan

```powershell
.\.venv\Scripts\python.exe scripts/check/run.py
```

Suite menguji workflow normal, kegagalan dan Debugger, batas biaya, resume, perlindungan test, path traversal, konflik penerapan patch, dan protokol provider melalui HTTP mock. Integrasi live dengan API berbayar dan eksekusi Docker perlu diuji terpisah pada lingkungan yang menyediakan kredensial dan engine.
