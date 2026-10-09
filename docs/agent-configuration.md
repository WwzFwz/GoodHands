# Configurator dan konfigurasi agent melalui Markdown

Configurator merupakan agent khusus yang dijalankan lewat `goodhands agents configure`.
Ia menggunakan model/provider yang sudah dikonfigurasi, tetapi memiliki instruksi, kontrak
output, dan loop sendiri. Ia tidak menjadi tahap tambahan setiap kali workflow coding berjalan.

## Menulis dalam bahasa biasa

Siapkan `goodhands.toml` dengan `goodhands init`, pilih model yang mendukung tool calling,
dan isi API key lewat environment seperti pada [README](../README.md). Dari terminal dengan
environment GoodHands aktif:

```powershell
goodhands agents configure --project PATH --role coder --request "Coder mengikuti rencana Code Architect. Gunakan OOP bila membantu, jangan membuat abstraksi berlebihan, dan jaga tes yang sudah ada."
```

Untuk instruksi panjang, simpan teks biasa dalam file UTF-8:

```powershell
goodhands agents configure --project PATH --role tester --request-file keinginan-tester.txt
```

`--request-file` dibaca relatif terhadap direktori terminal, sedangkan `--project` menunjuk
repository tujuan. Kamu tidak perlu menulis YAML atau JSON. Configurator menerima permintaan,
README lama, skill yang direferensikan, batas role, dan kesalahan parser jika ada.
Ia tidak menerima seluruh kode repository dan tidak mempunyai tool shell atau runner.

Output berisi ID draft, ringkasan, diff, status, jumlah request model, dan biaya/reservasi.
Secara default konfigurasi aktif belum berubah:

```powershell
goodhands agents diff RUN_ID --project PATH
goodhands agents apply RUN_ID --project PATH
```

Tambahkan `--apply` pada `agents configure` jika ingin draft yang lolos validasi langsung
diterapkan. Tidak ada pertanyaan konfirmasi tambahan pada jalur ini. Validitas format tidak
menjamin bahwa instruksi hasil LLM sudah sesuai maksudmu; diff tetap ditampilkan.

Jika status `config_needs_input`, baca `questions` dan jalankan `agents configure` lagi dengan
permintaan lengkap ditambah jawaban. Ini membuat run baru dengan anggaran baru; bukan resume
percakapan otomatis. `config_blocked` berarti batas tercapai atau draft belum valid, sehingga
tidak ada perubahan aktif. Panggilan jaringan yang gagal tidak diulang otomatis.

Untuk demo tanpa API:

```powershell
goodhands agents demo
```

Demo memakai respons tetap dan membuat proyek baru di `.goodhands/config-demos/`; parsing,
validasi, diff, dan penerapan draft tetap melalui implementasi asli. Demo bukan bukti kualitas
model live. Tidak perlu membeli layanan untuk demo atau tes lokal.

## Mengedit folder sendiri

```powershell
goodhands agents init --project PATH
goodhands agents validate --project PATH
goodhands agents compile --project PATH
```

`agents init` menyalin delapan role bawaan ke folder `agents` dan menolak menimpa folder yang
sudah ada. Configurator dapat membuat folder satu role langsung tanpa menjalankan init ini.
Role yang belum dioverride tetap memakai definisi bawaan.

```text
agents/
  consultant/
    README.md
  coder/
    README.md
    skills/
      implementation.md
      references/
        examples.md
  tester/
    README.md
skills/
  shared/
    python.md
```

Contoh `agents/coder/README.md` yang mempersempit tool Coder:

```markdown
---
name: coder
version: "1"
tools: [list_files, read_file, search_text, write_file, run_check]
enabled: true
skills:
  - skills/implementation.md
  - ../../skills/shared/python.md
  - python
---
# Coder

Implementasikan dan integrasikan perubahan sesuai kontrak Code Architect.

## Batas

Pertahankan acceptance criteria dan tes tepercaya. Laporkan kontradiksi sebelum
mengubah kontrak. Jangan mengklaim check lulus tanpa bukti runner.
```

Bagian YAML menentukan metadata, sementara seluruh isi setelah penutup `---` menjadi
instruksi tanpa diringkas compiler. Gunakan string untuk `version`, misalnya `"1"`.
Nama folder cocok dengan nama role; `code-architect` dan `code_architect` sama-sama diterima
untuk `name: code_architect`, tetapi jangan membuat dua override untuk role yang sama.

Skill adalah teks Markdown biasa. Referensi berakhiran `.md` relatif terhadap folder role;
referensi nama sederhana seperti `python` memakai `skills_dir` jika tersedia, kemudian skill
bawaan. Referensi `../` diperbolehkan setelah dinormalisasi selama hasilnya berada di dalam
repository dan bukan file/direktori terlarang. File di luar proyek, symlink, junction, dan
hardlink ditolak. README dan skill masing-masing dibatasi 32 KB.

Hanya skill yang tercantum secara eksplisit yang dimasukkan ke konteks. File dalam subfolder
tidak dimuat otomatis; link Markdown tidak diikuti secara rekursif. Referensi tambahan dapat
dibaca oleh agent engineering melalui tool baca bila dibutuhkan dan diizinkan. `task.skills`
tetap berlaku sebagai skill bersama untuk task, termasuk path `.md` relatif terhadap proyek.

## Sumber, kompilasi, dan kompatibilitas

Sumber bawaan ada di `goodhands/defaults/agents/NAMA_ROLE/README.md`. Sumber proyek otomatis
dicari di `agents/`; untuk folder lain, letakkan `roles_dir = "harness-roles"` di bagian paling
atas `goodhands.toml`, sebelum tabel pertama. `skills_dir` lama tetap didukung.

`agents compile` menulis `.goodhands/compiled/roles.json`, berisi versi skema, definisi efektif
semua role, dan teks skill yang terpilih per role. Hasil ini deterministik dan merupakan
artefak turunan untuk inspeksi/integrasi, bukan file yang perlu diedit. Runtime selalu membaca
sumber terbaru saat membuat run, lalu membekukan role dan skill di checkpoint. Ia tidak
menggunakan cache JSON yang mungkin sudah usang. Mengedit README tidak mengubah run berjalan.

Override JSON lama per role masih dibaca. JSON dan Markdown untuk role yang sama ditolak
sebagai duplikasi. Configurator tidak memigrasikan JSON secara otomatis; pindahkan metadata
ke frontmatter dan `instructions` ke body Markdown sebelum menggunakannya. Role/tool baru
di luar delapan role engineering masih memerlukan perubahan kode controller.

## Alur dan pembagian tanggung jawab

1. Pengguna memberi deskripsi biasa atau mengedit Markdown secara langsung.
2. Configurator mengusulkan README dan skill untuk satu role pada salinan repository.
3. Validator memeriksa metadata, identitas, izin, path, dan referensi skill.
4. Kesalahan dikembalikan ke Configurator, paling banyak tiga respons model total dan tetap
   tunduk pada batas policy. Ambiguitas maksud dikembalikan sebagai pertanyaan kepada pengguna.
5. Draft valid disimpan sebagai artefak beserta registry hasil kompilasi. Pengguna menerapkan
   draft dengan `agents apply`, atau memilih `--apply` sejak awal.
6. Run engineering berikutnya memakai sumber baru. Compiler sendiri tidak memanggil LLM.

Configurator hanya boleh mengubah README role tujuan dan file `skills/**/*.md` di dalam
folder role tersebut. Setiap skill yang dihasilkan harus tercantum dalam README. Tidak ada
operasi hapus file, perubahan role lain, atau perubahan skill bersama otomatis. Referensi ke
skill bersama yang sudah ada tetap dapat dipakai. Tool dan status enabled yang valid tidak
boleh diubah oleh Configurator; perubahan tersebut dilakukan pengguna pada sumber Markdown
dan tetap dibatasi izin maksimum harness. README lama yang tidak dapat diparse memakai
batas role bawaan sebagai titik awal; perhatikan diff sebelum menerapkan perbaikan tersebut.

Penerapan menolak draft yang berubah setelah validasi, perubahan source repository sejak
draft dibuat, atau perubahan `goodhands.toml`. Backup disimpan di direktori run. Sebagaimana
apply patch biasa, rollback tersedia untuk error biasa, bukan transaksi tahan mati listrik.
Snapshot repo disimpan lokal untuk pemeriksaan konflik, tetapi tidak dikirim seluruhnya ke
model. API key tidak dimasukkan ke prompt maupun artefak konfigurasi.

## Model, biaya, dan trade-off

Configurator memakai `profiles.default`, atau profil khusus:

```toml
[profiles.config]
model = "MODEL_ID_PILIHAN_ANDA"
request_reserve_usd = 0.10

[role_profiles]
configurator = "config"
```

`--profile config` dapat memilih profil untuk satu pemanggilan. Batas `policy` yang sama
mengatur biaya, jumlah panggilan, waktu, dan ukuran konteks. Accounting dibagi melalui modul
`inference.py`, sehingga engineering dan Configurator memakai aturan reservasi yang sama.
Reservasi bukan jaminan batas tagihan provider. Akun/API key dan kredit hanya diperlukan saat
memanggil model live; tidak ada biaya layanan parser tambahan.

| Pilihan | Manfaat | Trade-off |
| --- | --- | --- |
| Agent Configurator terpisah | Prompt dan konteks khusus authoring; tidak membebani setiap run coding | Ada jalur CLI dan kontrak output tambahan untuk dipelihara |
| Markdown dengan YAML frontmatter | Instruksi enak diedit, metadata dapat diperiksa | Menambah dependency PyYAML; kesalahan format perlu ditangani |
| Validator/compiler deterministik | Hasil konsisten, gratis dari sisi inference, izin tetap dikontrol program | Tidak memastikan makna instruksi benar |
| Draft sebelum apply, dengan opsi langsung apply | Perubahan terlihat dan dapat diperiksa | Jalur default membutuhkan satu command tambahan |
| Skill eksplisit, tanpa pemuatan rekursif | Konteks terkontrol dan biaya lebih mudah diprediksi | Pengguna/Configurator harus memilih referensi yang dibutuhkan |
| Snapshot seluruh source lokal untuk konflik | Memakai mekanisme apply/rollback yang sudah diuji | Perubahan source yang tidak terkait juga dapat memblokir apply |

Tes menggunakan respons simulator dan kegagalan provider buatan. Kemampuan model live dalam
mempertahankan maksud instruksi tetap perlu diuji dengan API dan model pilihanmu.
