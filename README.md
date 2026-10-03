# 🤖 DIGIDAW - Personal AI Assistant

Asisten suara cerdas berbahasa Indonesia untuk **Linux (Hyprland)** dan **Windows**, terinspirasi butler AI di
film Iron Man: cukup panggil *"Digidaw"* lalu bicara. Ia bisa mengendalikan PC, menjawab pertanyaan, memutar musik,
dan mengingat kebiasaan Anda.

**Sorotan**
- 🧠 **Otak hybrid**: model lokal **Qwen (Ollama)** menjawab obrolan & perintah sehari-hari dalam ±1 detik, gratis
  dan tetap jalan tanpa internet; **Gemini** dipakai untuk hal yang butuh internet, data terbaru, analisis layar,
  dokumen, dan pertanyaan kompleks.
- 🗣️ Wake word "Digidaw", mode percakapan, suara neural Indonesia yang natural (Microsoft Edge).
- 🖥️ Kendali PC: buka/tutup aplikasi (sekaligus beberapa), pindah workspace & pindahkan jendela, tab browser,
  volume, kecerahan, ketik teks, screenshot, file & folder, timer/alarm, kunci/matikan PC (dengan konfirmasi).
- 🎵 YouTube langsung terputar (Firefox / Chrome) tanpa klik, skip iklan, pause/lanjut/next dengan suara.
- 🧩 Belajar sendiri: mengingat fakta tentang Anda, aplikasi favorit, dan koreksi salah dengar
  (*"bukan itu, maksud saya …"*).
- 🛠️ Buat project, gambar & dokumen dengan suara, analisis keamanan sistem, HUD holografik.

---

## 📋 Persyaratan

| | Minimum | Disarankan |
| :--- | :--- | :--- |
| **OS** | Linux (Arch, Debian/Ubuntu, Fedora) dengan PipeWire, atau Windows 10/11 | Arch Linux + **Hyprland** (semua fitur jendela, workspace & hotkey) |
| **Perangkat** | Mikrofon & speaker, RAM 8 GB | RAM 16 GB + GPU ≥ 4 GB VRAM (untuk otak lokal Qwen) |
| **Internet** | Untuk suara neural & Gemini | — |
| **Akun** | [Gemini API key](https://aistudio.google.com/apikey) gratis | + [Ollama](https://ollama.com) untuk otak lokal |

Gemini API key dan Ollama sama-sama opsional, tapi minimal salah satu harus ada agar Digidaw bisa berpikir.
Tanpa keduanya, hanya perintah dasar (buka aplikasi, volume, alarm, dll.) yang jalan.

---

## 🚀 Instalasi

### Linux

```bash
git clone https://github.com/Adammalik28/digidaw-assistant.git
cd digidaw-assistant
./setup_digidaw.sh
```

Script setup akan:
1. memasang program sistem yang dibutuhkan (pacman / apt / dnf, minta password sudo),
2. menyiapkan Python 3.12 + semua modul di `~/.local/share/digidaw/venv` (lewat [uv](https://docs.astral.sh/uv/)),
3. menanyakan **Gemini API key** Anda (disimpan di `.env`, tidak pernah ikut ter-upload),
4. mengunduh model pengenal suara lokal (Whisper, ±220 MB),
5. menawarkan pemasangan **Ollama + qwen3:4b-instruct** (±2,5 GB, otak lokal),
6. menambahkan **Digidaw** ke menu aplikasi (dan opsional: jalan otomatis saat login),
7. menjalankan **pemeriksaan setup** yang menjelaskan apa yang sudah siap dan apa yang perlu diperbaiki.

Opsi: `--yes` (tanpa tanya, pakai pilihan yang disarankan) · `--autostart` · `--no-ollama`.
Jalankan lagi kapan saja untuk memperbarui.

### Windows

1. `git clone https://github.com/Adammalik28/digidaw-assistant.git` (atau *Code → Download ZIP* lalu ekstrak).
2. Klik dua kali **`setup_digidaw.bat`**. Script ini memasang Python 3.12 (lewat winget bila belum ada), semua
   modul, menanyakan Gemini API key, mengunduh model suara, menawarkan Ollama, lalu membuat shortcut di Desktop.

### Menjalankan

- **Linux**: buka **Digidaw** dari menu aplikasi, atau `./start_digidaw.sh` (tanpa terminal, log di
  `logs/digidaw.log`) · `./start_digidaw_console.sh` (log langsung di terminal) · `./start_digidaw_text.sh`
  (mode ketik, mikrofon mati).
- **Windows**: shortcut **Digidaw** di Desktop / `start_digidaw.vbs` · `start_digidaw_console.bat` (dengan log).

Lalu ucapkan: ***"Digidaw, jam berapa sekarang?"*** HUD tampil di browser; tutup HUD tidak mematikan Digidaw.
**Tampilkan HUD lagi**: Ctrl+Alt+H atau *"Digidaw, tampilkan HUD"* · **Matikan**: tombol ⏻ di HUD atau
*"Digidaw, keluar"*.

### Ada masalah?

Jalankan pemeriksaan setup. Ia memeriksa modul, program sistem, mikrofon, suara, Gemini, Ollama, model Whisper,
dan browser, lalu menjelaskan cara memperbaiki yang belum siap:

```bash
~/.local/share/digidaw/venv/bin/python check_setup.py      # Linux
.venv\Scripts\python.exe check_setup.py                     # Windows
```

Sertakan hasilnya (dan potongan `logs/digidaw.log`) saat membuat *issue*.

---

## 🧠 Otak Hybrid: Qwen (lokal) + Gemini (online)

| Permintaan | Ditangani oleh |
| :--- | :--- |
| Perintah jelas: *buka/tutup aplikasi, workspace, volume, alarm, ketik, putar lagu* | Langsung dijalankan, tanpa AI (instan) |
| Obrolan, pertanyaan ringan, hitungan, perintah yang perlu dipahami | **Qwen** lokal (±1 dtk) |
| Butuh internet / data terbaru (berita, harga, skor, *"hari ini"*, *"terbaru"*) | **Gemini** |
| Kompleks (analisis, bandingkan, kode, penjelasan detail), layar, dokumen, project | **Gemini** |
| Qwen ragu, tidak punya tool yang cocok, atau mengaku beraksi tanpa benar-benar beraksi | Otomatis diteruskan ke **Gemini** |
| Gemini tidak tersedia (kuota habis / offline) | **Qwen** menjawab sebisanya |

Atur di **Control Panel → AI**: *Otak utama* (gabungan / selalu Gemini / utamakan lokal), *Model lokal*, dan
*Gaya bicara* (saya/Anda atau aku/kamu). Log mencatat otak yang menjawab (`🧠 lokal` / `🧠 gemini`).
Model lain bisa dipakai: `ollama pull <model>` lalu isi namanya di Control Panel. Hindari model *thinking*
karena 10–50 detik per jawaban.

---

## ⚙️ Control Panel

Tombol ⚙ di HUD membuka Control Panel. Semua perubahan tersimpan di `settings.json`, dan sebagian besar
langsung aktif tanpa restart (yang perlu restart diberi tanda; ada tombol **Restart sekarang**).

| Bagian | Isi |
| :--- | :--- |
| **Umum** | Sapaan untuk Anda, kota cuaca, jalankan otomatis saat Windows menyala, buka HUD saat mulai |
| **Folder & Project** | **Folder project fokus**: dipakai saat Anda bilang *"buka project"*, *"cari file …"*, *"buat folder …"*. Juga editor project (VS Code / Explorer / Terminal), folder catatan, folder screenshot |
| **Pendengaran** | Mode percakapan & durasinya, wajib wake word, filter kebisingan, jeda akhir kalimat, hotkey |
| **Suara** | Mesin suara, suara Indonesia (Ardi/Gadis) & Inggris, kecepatan bicara, "Sebentar, Sir." |
| **Media** | Browser pemutar YouTube, skip iklan, pause musik lain, pengecilan musik saat Digidaw bicara |
| **AI** | Gemini API key (disimpan di `.env`), prioritas model cepat/pintar |
| **Keamanan & Sistem** | Konfirmasi aksi berbahaya, peringatan otomatis |
| **Tentang & Daya** | Tes suara, buka folder log, restart, matikan |

---

## 🗣️ Cara Berbicara dengan DIGIDAW

- **Wake word**: awali perintah dengan *"Digidaw, …"* (juga *"Hei Digidaw"*, *"Oke Digidaw"*).
  Google sering menulis nama ini sebagai "digital", jadi "digital" di **awal** kalimat juga dianggap panggilan.
- **Mode percakapan**: setelah Digidaw menjawab, ia **tetap mendengarkan ±10 detik** (bilah di bawah
  arc reactor menghitung mundur). Anda bisa langsung membalas tanpa menyebut "Digidaw", bahkan saat musik
  diputar, karena musiknya dikecilkan selama itu. Ucapkan *"terima kasih"* / *"sudah"* / *"cukup"* untuk menutup percakapan.
- **Boleh berjeda**: *"Digidaw … putar lagu Coldplay"* tetap tertangkap. Kalau Anda hanya bilang "Digidaw",
  terdengar bunyi *ting* dan kalimat berikutnya (dalam 8 detik) langsung dianggap perintah.
- **Mikrofon selalu aktif**: merekam terus tanpa jeda, dan otomatis "tuli" hanya selama Digidaw berbicara.
- **Deteksi suara WebRTC VAD**: mengenali pola suara manusia, jadi suara pelan tetap tertangkap, bising
  ruangan diabaikan, dan akhir kalimat terdeteksi ±0.5 detik setelah Anda berhenti bicara.
- **Tanda respons**: bunyi *tik* begitu perintah dipahami; bila jawaban butuh lebih dari 3 detik,
  Digidaw bilang "Sebentar, Sir.".
- **Push-to-talk / menyela**: **Ctrl+Alt+D** dari aplikasi mana pun, atau tombol mikrofon di HUD.
- **Ketik**: di kolom perintah HUD (tekan `/` untuk fokus, `Esc` untuk menyela).

### ⚡ Kecepatan Respons
- **Jalur cepat**: perintah sederhana dan jelas (jam, tanggal, volume, pause/lanjut/next, "putar lagu X",
  screenshot, kunci layar, buka/tutup aplikasi umum, "buka project", status sistem) dijalankan tanpa menunggu AI.
- **Satu panggilan AI**: untuk perintah aksi lain, Gemini hanya dipanggil sekali.
- **Suara streaming**: suara mulai diputar begitu potongan audio pertama tiba; frasa yang sering dipakai disimpan di cache.
- Ukuran tipikal (selesai bicara → jawaban siap): jalur cepat ±0.8 detik, via AI ±2–3.5 detik, lalu suara mulai ±0.4–1 detik.

---

## 📄 Analisis File yang Sedang Dibuka

Buka dokumen apa saja, lalu tanya Digidaw tanpa menyebut nama file:
- *"file apa ini?"*: jenis, jumlah halaman/slide/sheet, ukuran, penulis, dan isinya secara singkat
- *"rangkum dokumen ini"*, *"apa kesimpulan PDF ini"*, *"siapa penulisnya"*, *"di halaman berapa membahas X"*,
  *"berapa total di sheet penjualan"*, *"terjemahkan slide ini"*, dan pertanyaan lanjutan lainnya
- *"simpan hasil analisis"*: jawaban lengkapnya (poin-poin, nomor halaman) disimpan sebagai catatan dan dibuka

Digidaw menemukan file-nya dengan tepat: **Word/Excel/PowerPoint** ditanya langsung ke aplikasinya (termasuk dokumen
yang belum disimpan), **PDF di browser** lewat alamat tab, viewer lain (Acrobat, Foxit, Notepad, VS Code…) lewat
program atau daftar *Recent files*. Bisa juga menyebut nama: *"rangkum file laporan.pdf"*.
Format: PDF (termasuk hasil scan), Word, Excel, PowerPoint, CSV, teks, kode, dan gambar.

*"Diagnosa sistem"* membaca Event Viewer, mengelompokkan error penting (crash aplikasi, mati mendadak, disk, driver,
perangkat keras), lalu menjelaskannya dalam bahasa sehari-hari.

## ⏰ Timer, Alarm & Pengingat

- *"timer 10 menit"*, *"timer 1 jam 30 menit untuk masak"*, *"timer setengah jam"*
- *"alarm jam 6 pagi"*, *"bangunkan saya jam 5.45"*, *"alarm jam setengah 7 setiap hari kerja"*,
  *"alarm setiap senin dan kamis jam 7 pagi olahraga"*
- *"ingatkan saya 20 menit lagi untuk angkat jemuran"*, *"ingatkan besok jam 8 rapat"*, *"ingatkan jam 7 malam telepon ibu"*
- *"alarm apa saja"*, *"batalkan alarm jam 6"*, *"batalkan semua timer"*
- Saat berbunyi: suara alarm berulang dan HUD menampilkan tombol **STOP / TUNDA**. Cukup ucapkan *"stop"* atau
  *"tunda"* tanpa "Digidaw", tekan **Esc** di HUD, atau **Ctrl+Alt+D**. Alarm berhenti sendiri setelah 60 detik.
- Tersimpan walau Digidaw di-restart. Pengingat yang terlewat saat Digidaw mati diberitahukan saat menyala.
  Lama bunyi dan durasi tunda bisa diatur di Control Panel → Umum.

Aplikasi yang dibuka Digidaw (Notepad, folder, file, catatan, browser) kini langsung **tampil di depan**.

---

## 🔒 Mode Privat & Tanpa Internet

- **Mode privat** (aktif secara default): setiap kalimat dikenali **di laptop** dulu (Whisper *tiny*, ±0,25 detik). Hanya kalimat
  yang memanggil "Digidaw", atau yang diucapkan saat Digidaw memang sedang mendengarkan, yang dikirim ke Google agar
  akurat. Obrolan orang di sekitar Anda **tidak pernah keluar dari laptop**.
- **Tanpa internet**: perintah suara tetap jalan, dikenali di laptop dengan Whisper *base*. Contohnya jam, buka aplikasi,
  volume, dan timer. Digidaw memberi tahu saat internet putus dan saat tersambung lagi.
- Model tersimpan di `models\` (±220 MB) dan diunduh otomatis bila belum ada. Atur di Control Panel → Pendengaran.

## 🌅 Ringkasan Pagi & Perintah Setengah

- Saat Digidaw **pertama kali menyala hari itu** (termasuk saat ikut menyala bersama Windows), ia menyapa dengan tanggal,
  cuaca, pengingat, dan catatan keamanan. Atur kotanya di Control Panel → Umum.
- Ucapkan perintah setengah seperti *"Digidaw, buka"*, maka Digidaw bertanya *"Buka apa?"*. Jawab *"Firefox"* saja.
  Ini juga berlaku untuk *putar*, *pindah ke*, *cari*, *tutup*, dan lainnya.
- Setelah memulai pekerjaan latar (gambar, file, scan), Digidaw tidak membuka jendela percakapan, supaya kata yang
  kebetulan terdengar tidak dianggap permintaan baru.
- "Baterai sudah terisi penuh" hanya diucapkan sekali per pengisian, bukan setiap kali Digidaw menyala.

---

## ⏸️ Jeda Digidaw & Kontrol Musik Cepat

- **Tombol Esc (di jendela HUD)**: saat jendela HUD aktif, tekan **Esc** untuk langsung bicara tanpa memanggil
  "Digidaw", dan **Esc** lagi untuk membatalkan (berhenti mendengarkan atau memotong jawaban). Di aplikasi lain Esc
  berfungsi normal. Dari aplikasi lain, gunakan **Ctrl+Alt+D**. Bisa diubah ke *tekan dua kali* atau dimatikan
  (Control Panel → Pendengaran).
- **Buka HUD dengan suara**: *"Digidaw, buka HUD"* / *"tampilkan dashboard"* membuka atau memunculkan jendela HUD, berguna
  saat Digidaw berjalan otomatis sejak Windows menyala. *"Digidaw, sembunyikan HUD"* meminimalkannya, dan Digidaw
  tetap mendengarkan.
- **Jeda pendengaran** (misalnya saat ngobrol dengan teman): *"Digidaw, istirahat dulu"*, *"Digidaw, jangan dengarkan
  dulu"*, *"Digidaw, mode senyap"*, atau dengan durasi: *"… selama 30 menit"*. Bisa juga lewat tombol ⏸ di HUD.
  Selama dijeda, obrolan diabaikan dan tidak dicatat. Kalimat panjang bahkan tidak dikirim ke Google.
  Aktifkan lagi dengan *"Digidaw, aktif"*, **Ctrl+Alt+D**, atau tombol ▶.
- **Musik tanpa menyebut "Digidaw"** saat lagu diputar: cukup *"pause"*, *"stop"*, *"next"*, *"skip"*, lalu
  *"play"* untuk melanjutkan (bisa dimatikan di Control Panel → Pendengaran).
- **Sleep timer**: *"matikan musik dalam 30 menit"*, *"matikan laptop dalam 15 menit"* (dengan konfirmasi).
- **Navigasi halaman**: *"scroll ke bawah"*, *"scroll ke atas 3 kali"*, *"kembali"*, *"refresh halaman"*, *"tab baru"*,
  *"tab berikutnya"*, *"buka lagi tab yang tadi ditutup"*.
- Obrolan orang lain yang terdengar **tidak ditulis ke file log** (hanya jumlah katanya). Nama mirip "Digidaw" di akhir
  kalimat obrolan tidak lagi dianggap perintah.

---

## 🎨 Generate Gambar & File (Antigravity)

- **Gambar**: *"Digidaw, buatkan gambar kucing astronot"*, *"bikin 3 logo kedai kopi"*, *"generate wallpaper
  pemandangan gunung"*. Antigravity memakai alat `generate_image`-nya, dan hasilnya dibuka otomatis.
- **File**: *"buatkan file Excel anggaran bulanan"*, *"bikin presentasi tentang AI 5 slide"*, *"buat surat lamaran
  kerja dalam Word"*, *"buat PDF jadwal belajar"*. Hasilnya berupa file .docx / .xlsx / .pptx / .pdf sungguhan.
- Hasil disimpan di **Control Panel → Folder & Project → Folder hasil generate** (default
  `Documents\Digidaw Generated`), dipisah ke subfolder `Gambar` dan `File`. File kerja sementara agen tidak ikut.
- *"buka gambar tadi"*, *"buka folder generate"*, *"status pembuatan gambar"*, *"batalkan pembuatan gambar"*.
- Di HUD: tombol **+ GAMBAR** / **+ FILE** mengisi kolom perintah, dan tab **Generate** menampilkan progres serta
  galeri hasil. Klik untuk membuka; klik kanan untuk menampilkan di folder.

---

## 🏗️ Buat Project dengan Suara

1. Ucapkan *"Digidaw, buat project website toko kue"* (atau *bikin aplikasi / website / game …*).
2. Digidaw masuk **mode brief**: ceritakan project selengkap mungkin: tujuan, fitur, tampilan.
   Anda tidak perlu menyebut "Digidaw" di tengah penjelasan, dan transkripnya tampil di HUD.
   Ucapkan **"selesai"** (atau diam 20 detik) kalau sudah; **"batal"** untuk membatalkan. Bisa juga diketik di HUD.
3. Kalau deskripsinya masih kurang jelas (misalnya hanya *"buat aplikasi catatan"*), Digidaw mengajukan
   **maksimal dua pertanyaan** dulu (platform? fitur inti?). Jawab lalu ucapkan "selesai", atau bilang
   **"terserah"** / **"lanjut saja"** agar Digidaw memilih sendiri. Bisa dimatikan di Control Panel → Project Builder.
4. Digidaw merangkum project-nya lalu bertanya *"Mulai sekarang?"*, jawab **ya**.
5. Project dibuat di latar belakang oleh **Antigravity CLI** (default) atau **Claude Code**
   (sebut *"… pakai Claude Code"*, atau ubah default di Control Panel → Project Builder), di folder baru
   `Projects\<nama-project>`. Progresnya tampil di panel **Pembuatan Project** di HUD; Digidaw tetap bisa dipakai.
6. Setelah selesai, Digidaw menjalankan project di jendela terminal, mengecek bahwa aplikasinya merespons,
   membuka browser, dan mengumumkan *"… sudah selesai dan berjalan"*. **Screenshot tampilannya** muncul di panel
   Pembuatan Project (klik untuk memperbesar).

**Riwayat versi (backup lokal, tanpa git)**: setiap kali project selesai dibuat atau diubah, Digidaw menyimpan
versinya di `<folder project>\.digidaw\versions` (file yang sama hanya disimpan sekali; `node_modules`, `venv`,
`dist` dll. tidak ikut, dan diinstall ulang otomatis saat perlu). Edit manual Anda juga tersimpan sebelum agen
mengubah project.
- *"riwayat versi project kalkulator"*: daftar versi
- *"apa saja yang diubah di project kalkulator"*: file yang berubah pada perubahan terakhir + laporan agen
- *"kembalikan project kalkulator ke versi sebelumnya"* / *"… ke versi 2"* / *"batalkan perubahan terakhir"*:
  file dikembalikan (dengan konfirmasi), lalu project dijalankan ulang. Pemulihan juga tercatat sebagai versi,
  jadi bisa dibatalkan lagi. Kalau perubahan gagal, Digidaw langsung menyarankan perintah ini.

**Beberapa project sekaligus**: minta project baru walau yang lain masih dikerjakan. Maksimal 2 dikerjakan
bersamaan (ubah di Control Panel); sisanya masuk **antrean** dan dimulai otomatis. Setiap project mendapat
port sendiri (3100, 3101, …) supaya tidak bentrok.

**Ubah project dengan suara**: *"Digidaw, tambahkan fitur riwayat perhitungan ke kalkulator"*,
*"ubah warna project-nya jadi biru"*, *"perbaiki error di project kalkulator"*. Agen melanjutkan percakapan
yang sama (jadi ingat konteks project-nya), lalu Digidaw menjalankan ulang dan mengecek project-nya lagi.

Perintah lain: *"status project"* (atau *"status project kalkulator"*), *"batalkan pembuatan project todo"*, *"buka project kalkulator"* (membuka editor
dan menjalankannya; kalau sudah berjalan tidak dijalankan dua kali), *"hentikan project"*, *"project apa saja"*.
Semua project diingat walau Digidaw di-restart (`<folder project>\.digidaw\project.json`).
Log agen ada di `<folder project>\.digidaw\build.log`; brief lengkap di `BRIEF.md`.

> Agen berjalan dengan izin otomatis (bisa menginstall paket dan menjalankan perintah tanpa bertanya)
> agar project bisa selesai tanpa ditunggui. Itu bisa dimatikan di Control Panel, tetapi agen lalu bisa berhenti
> di tengah jalan saat butuh izin.

---

## 🛡️ Analisis Keamanan Sistem

Ucapkan *"Digidaw, analisis sistem"* (juga: *"analisis keamanan"*, *"cek virus"*, *"apakah laptop saya aman"*,
atau tombol **SCAN KEAMANAN** di HUD). Dalam ±10–60 detik Digidaw memeriksa:

- **Windows Defender**: antivirus & real-time protection, Tamper Protection, umur database, kapan terakhir full scan
- **Riwayat ancaman**: apa saja yang pernah terdeteksi, masih aktif atau sudah ditangani, dan apakah filenya masih ada
- **Firewall, Windows Update, UAC, proxy & file hosts** (tanda pembajakan browser/DNS)
- **Program startup & scheduled task**: perintah tersamar (PowerShell ter-encode, download-and-run), program tanpa
  tanda tangan digital dari folder rawan (Temp/Downloads/AppData), entri yatim
- **Proses & jaringan**: program tak bertanda tangan yang berjalan dari folder rawan, port yang terbuka ke jaringan
  (termasuk Remote Desktop), aplikasi remote access (TeamViewer, AnyDesk, …)
- **File & software**: file menyamar berekstensi ganda (`invoice.pdf.exe`), installer/skrip tak bertanda tangan
  di Downloads, software repack/crack/activator, tool cheat

Hasilnya: skor 0–100 yang dibacakan, notifikasi di HUD, dan **laporan lengkap beserta saran** yang dibuka di browser
(tersimpan di `Documents\Digidaw Reports`). *"Buka laporan keamanan"* membuka laporan terakhir.
Analisis ini **hanya membaca**: tidak ada file yang dihapus atau diubah. *"Status sistem"* tetap untuk ringkasan
CPU/RAM/baterai.

---

**Tindak lanjut** (selalu minta konfirmasi dulu):
- *"bersihkan startup yatim"*: hapus entri startup/scheduled task yang programnya sudah tidak ada
  (cadangan JSON disimpan di `Digidaw Reports`; sebagian butuh izin administrator, jadi muncul jendela UAC)
- *"tutup aplikasi remote"*: tutup TeamViewer/AnyDesk/… beserta layanan latarnya
- *"jalankan quick scan"* / *"jalankan full scan"*: pemindaian Windows Defender di latar belakang, hasilnya dibacakan
- *"scan file itu"* / *"hapus file itu"*: untuk file yang baru saja diperingatkan (hapus = ke Recycle Bin)

**Otomatis di latar belakang** (atur di Control Panel → Keamanan & Sistem):
- Scan keamanan tiap 7 hari. Digidaw **hanya bersuara kalau ada temuan baru** dibanding scan sebelumnya.
- **Penjaga Downloads**: begitu unduhan selesai, Digidaw memperingatkan program tanpa tanda tangan digital, skrip,
  file image disk (.iso/.img), dan file menyamar (`invoice.pdf.exe`), lengkap dengan situs asalnya.
  Installer yang ditandatangani resmi hanya muncul sebagai notifikasi kecil di HUD.

---

## 👁️ Tahu Apa yang Sedang Anda Lihat

Digidaw mengingat jendela/tab yang terakhir Anda pakai (klik HUD tidak mengubahnya) dan membaca alamat tab
browser (Chrome, Edge, Firefox, Brave, Opera):
- *"ringkas halaman ini"*, *"artikel ini tentang apa"*: isi halaman diringkas; untuk halaman yang butuh login,
  video, atau aplikasi web, Digidaw membaca layarnya
- *"tutup tab ini"*, *"simpan link ini"* (ke `Link Tersimpan.md` di folder catatan), *"salin link ini"*
- *"jelaskan / terjemahkan teks yang saya blok"*: blok teks di aplikasi apa pun, lalu minta
- *"saya sedang buka apa"*

**Perintah gabungan**: *"buka Firefox dan cari Instagram"*, *"buka notepad lalu putar lagu Hindia"* dijalankan
berurutan; langkah web mengikuti browser yang Anda sebut.

---

## 🩺 Kalau Digidaw Lambat atau Tidak Merespons

**Indikator AI** di bagian atas HUD menunjukkan kondisi AI yang sebenarnya, bukan selalu "kuota":
🟢 normal (model & pemakaian per menit) · 🟡 *KUOTA PENUH · 40 dtk* (hitung mundur) / *SERVER SIBUK* ·
🔴 *OFFLINE* (internet), *API KEY DITOLAK*, *BELUM DIATUR*, atau *ERROR* (masalah di dalam Digidaw; arahkan
mouse untuk melihat detailnya). Klik indikator, atau ucapkan *"Digidaw, cek koneksi AI"*, untuk memeriksa ulang.
Setiap kali menyala, Digidaw menguji AI sekali dengan semua fiturnya dan memberi tahu kalau ada masalah.
Kalau AI gagal, jawaban suaranya menyebut penyebab yang sebenarnya.

**Restart**: tombol **⟳** di samping tombol ⏻ (⏻ = *matikan* total), atau ucapkan *"Digidaw, restart Digidaw"* /
*"restart dirimu"*. *"Restart"* saja tetap berarti restart laptop (dengan konfirmasi).

- **Kuota AI gratis**: setiap model Gemini gratis dibatasi ±15 permintaan per menit. Digidaw
  menghitungnya sendiri dan berpindah antar beberapa model sebelum kena batas, lalu kembali ke
  model tercepat. Pemakaian per menit terlihat di bagian **AI** pada status bar HUD. Kalau semua
  model sedang penuh, Digidaw memberi tahu berapa detik lagi; perintah dasar (volume, musik, buka
  aplikasi) tetap jalan tanpa AI.
- **Nama tidak terdengar**: Google sering menulis "Digidaw" sebagai kata lain ("digital", "didi",
  "lagi dong"…). Ucapan yang diabaikan muncul di log HUD dengan tombol **+ "…" = Digidaw**; klik
  sekali agar lain kali dikenali. Daftarnya bisa diatur di Control Panel → Pendengaran.
- **Tidak pernah macet total**: setiap perintah dibatasi 40 detik dan setiap permintaan AI 20 detik.
- **Diagnosis**: `logs\digidaw.log` mencatat waktu setiap perintah (⏱) dan laporan kesehatan tiap
  10 menit (♥: thread, RAM, antrean suara, pemakaian AI per model).

---

## 🖥️ HUD Holografik

Panel tambahan: **Keamanan** (skor, scan terakhir/berikutnya, penjaga unduhan, tombol SCAN / LAPORAN / DEFENDER),
**Lokasi** (folder Project Builder, hasil generate, catatan, screenshot; klik untuk membuka), dan panel bertab
**Project / Generate** (pengerjaan aktif, project terbaru dengan status berjalan & jumlah versi, galeri hasil generate).
Tab berpindah otomatis ke pekerjaan yang sedang berjalan. Tombol ⏸ di atas menjeda pendengaran.

Saat start, jendela HUD ala Iron Man terbuka otomatis (`http://127.0.0.1:8765`):
arc reactor yang bereaksi (siaga · mendengarkan · memproses · berbicara), telemetri live
(CPU, RAM, baterai, disk, jaringan), cuaca, log percakapan + skill yang dijalankan, *now playing*
dengan tombol kontrol, timer, dan tombol protokol cepat.
Jika jendelanya tertutup: **Ctrl+Alt+H** atau ucapkan *"Digidaw, tampilkan HUD"*.

Keamanan: server HUD hanya menerima koneksi dari PC ini dan setiap perintah butuh token sesi acak,
sehingga website lain yang terbuka di browser tidak bisa mengirim perintah ke Digidaw.

---

## 🎙️ Contoh Perintah

DIGIDAW berjalan dalam **Agent Mode**: Gemini AI memahami maksud Anda lalu menjalankan
*skill* yang dibutuhkan (bisa beberapa sekaligus). Bicara saja secara natural.

| Kategori | Contoh Perintah |
| :--- | :--- |
| **Musik & Video** | *"Putar lagu Coldplay Yellow"* (langsung terputar, tanpa klik), *"Putar playlist lagu galau"* (YouTube Mix non-stop), *"Pause"*, *"Lanjutkan"*, *"Lagu berikutnya"*, *"Lagu apa ini?"*, *"Maju 30 detik"*, *"Ulangi lagu ini"*, *"Layar penuh"*, *"Tutup pemutar YouTube"* |
| **Vision** | *"Lihat layar saya, ini error apa?"*, *"Terjemahkan teks di layar"*, *"Jelaskan kode yang sedang saya buka"* |
| **Info Real-time** | *"Harga emas Antam hari ini?"*, *"Persib kemarin menang?"*, *"Berita teknologi hari ini"*, *"Briefing harian"* |
| **Protokol** | *"House Party Protocol"*, *"Aktifkan protokol kerja / santai / fokus / tidur"*, *"Clean Slate Protocol"*, *"Buat protokol baru bernama gaming: buka Steam dan volume 60"* |
| **Catatan & Dikte** | *"Buka notepad dan catat berikut: rapat jam 3"*, *"Tambahkan ke catatan tadi: bawa laptop"*, *"Mulai dikte"* → semua ucapan diketik ke aplikasi aktif, *"Selesai dikte"* |
| **Aplikasi & Jendela** | *"Buka Steam"*, *"Tutup Chrome"* (ditutup normal, seperti klik X), *"Paksa tutup Spotify"*, *"Pindah ke WhatsApp"*, *"Maksimalkan jendela ini"*, *"Taruh Chrome di kiri layar"*, *"Jendela apa saja yang terbuka?"* |
| **Website** | *"Buka Instagram"*, *"Buka detik.com"*, *"Googling resep nasi goreng"* |
| **File & Folder** | *"Buka folder Downloads"*, *"Cari file skripsi"*, *"Buat folder Proyek di desktop"*, *"Hapus file X"* (masuk Recycle Bin) |
| **Pembersihan** | *"Hapus sampah di Recycle Bin"*, *"Bersihkan file temp"* |
| **Volume & Layar** | *"Volume 40"*, *"Keraskan suara"*, *"Mute"*, *"Kecerahan 70"*, *"Screenshot"* (tersimpan + disalin ke clipboard) |
| **Keyboard & Clipboard** | *"Ketik halo semuanya"*, *"Tekan ctrl+s"*, *"Ringkas teks yang saya copy"*, *"Salin hasilnya ke clipboard"* |
| **Timer & Pengingat** | *"Ingatkan saya 10 menit lagi angkat jemuran"*, *"Ingatkan jam 15:30 meeting"*, *"Batalkan semua timer"* |
| **Sistem** | *"Status sistem"*, *"Cuaca hari ini"*, *"Cek jaringan"*, *"Kunci komputer"*, *"Matikan komputer"*, *"Batalkan shutdown"* |
| **Memori** | *"Ingat bahwa nama panggilan saya King"*, *"Apa yang kamu ingat tentang saya?"* |
| **Perintah bebas** | *"Jalankan perintah ipconfig"* — PowerShell apa saja, untuk hal yang belum ada skill-nya |
| **Keluar** | *"Keluar"*, *"Exit"*, *"Shutdown Digidaw"* |

### 🎵 Tentang Pemutar YouTube
Lagu/video diputar di jendela browser khusus DIGIDAW (profil terpisah, tidak menyentuh browser Anda) sehingga
video **langsung terputar tanpa klik**, iklan dilewati, dan bisa di-pause/lanjut/skip/seek dengan tepat. Satu tab
dipakai ulang untuk setiap lagu. Browser mengikuti browser default Anda (**Firefox** dikendalikan lewat WebDriver
BiDi; **Chrome/Edge/Brave/Chromium** lewat DevTools); ubah di Control Panel → Media. Anda boleh login YouTube
sekali di jendela itu; login-nya tersimpan. Perintah *pause / lanjutkan / berikutnya / lagu apa ini* juga
berlaku untuk Spotify dan pemutar lain (MPRIS di Linux, Media Session di Windows).
Catatan: jendela khusus ini membuka port kendali lokal (hanya dari PC ini), jadi gunakan untuk media saja,
jangan untuk login bank/email.

### 🛡️ Safety Protocol
Aksi berbahaya/tidak bisa dibatalkan (shutdown, restart, sleep, sign out, kosongkan Recycle Bin,
hapus file temp, jalankan PowerShell) selalu ditanyakan dulu, jawab **"ya"** atau **"tidak"**.
Ini mencegah salah dengar mikrofon berakibat fatal. Bisa dimatikan lewat
`CONFIRM_DANGEROUS_ACTIONS = False` di `config.py` (tidak disarankan).

### 🔔 Peringatan Proaktif
Tanpa diminta, Digidaw akan bicara saat baterai tinggal 20/10/5%, charger dicolok/dicabut,
baterai penuh, CPU > 90% lebih dari 1 menit, RAM hampir penuh, atau disk C < 5 GB.

### 📴 Mode Offline
Jika internet/Gemini tidak tersedia, DIGIDAW otomatis memakai pencocokan kata kunci lokal
untuk perintah-perintah dasar di atas.

---

## ⚙️ Pengaturan Penting (`config.py`)

| Pengaturan | Fungsi |
| :--- | :--- |
| `REQUIRE_WAKE_WORD`, `FOLLOWUP_SECONDS` | Wajib sebut "Digidaw" / lama jendela percakapan lanjutan |
| `PUSH_TO_TALK_HOTKEY` | Hotkey bicara/menyela (default `ctrl+alt+d`) |
| `TTS_ENGINE`, `EDGE_VOICE_ID`, `EDGE_VOICE_EN` | Mesin & karakter suara (default: Ardi untuk Indonesia, Ryan British untuk Inggris) |
| `MEDIA_BROWSER`, `MEDIA_BRING_TO_FRONT`, `YOUTUBE_AUTO_SKIP_ADS` | Perilaku pemutar YouTube |
| `DUCK_MEDIA`, `DUCK_LEVEL` | Kecilkan musik saat Digidaw bicara |
| `HUD_ENABLED`, `HUD_AUTO_OPEN`, `HUD_PORT` | HUD holografik |
| `PROACTIVE_ALERTS` | Peringatan baterai/CPU/RAM/disk |
| `USER_TITLE` | Sapaan untuk Anda (default "Sir") |
| `BRAIN_MODE`, `LOCAL_MODEL`, `ADDRESS_STYLE` | Otak hybrid, model Ollama, gaya bicara saya/Anda atau aku/kamu |

Pengaturan dari Control Panel tersimpan di `settings.json` dan menimpa nilai di `config.py`.
Protokol tersimpan di `protocols.json` (bisa diedit manual), memori di `memory_semantic.json`,
catatan di folder Documents/Digidaw Notes, screenshot di Pictures/Screenshots.

---

## 🧩 Struktur Kode & Menambah Skill

| File | Isi |
| :--- | :--- |
| `main.py` | Titik masuk, dashboard, cek dependency |
| `core.py` | Orkestrator: antrean perintah, wake word, follow-up, dikte, push-to-talk |
| `brain.py` | Router perintah cepat, otak hybrid (Qwen lokal + Gemini agent), koreksi & penjaga klaim palsu |
| `local_llm.py` · `llm.py` | Klien Ollama · klien & rotasi model Gemini |
| `persona.py` · `habits.py` · `memory.py` | Gaya bicara natural · kebiasaan & koreksi salah dengar · memori jangka panjang |
| `system_control.py` | Skill sistem: aplikasi, file, volume, catatan, timer, power, dll |
| `media.py` · `firefox_player.py` | Pemutar YouTube (DevTools / WebDriver BiDi), media session, ducking |
| `window_manager*.py` | Jendela & workspace (Hyprland di Linux, user32 di Windows) |
| `linux_desktop.py` · `oscompat.py` | Lapisan sistem Linux (hyprctl, PipeWire, XDG) · perbedaan Windows/Linux |
| `ai_skills.py` · `screen_context.py` | Vision layar, pencarian web, berita · konteks jendela & tab yang aktif |
| `protocols.py` · `alarms.py` · `monitor.py` | Protokol multi-langkah · timer/alarm · peringatan proaktif |
| `hud_server.py` · `hud/index.html` | Server & tampilan HUD |
| `project_builder.py` · `generator.py` | Buat project · gambar & dokumen dengan suara |
| `settings.py` | Skema & penyimpanan Control Panel (`settings.json`), autostart, pemilih folder |
| `tts.py` · `stt.py` · `local_stt.py` | Suara keluar (Edge neural, cache) · pengenalan suara (Google + Whisper lokal) |
| `check_setup.py` · `setup_digidaw.*` | Pemeriksaan & instalasi |

Menambah skill: tulis fungsi Python (dengan type hint + docstring yang jelas) di modul yang sesuai,
lalu masukkan ke daftar `AI_TOOLS` modul itu. Gemini otomatis bisa memakainya, dan skill itu juga
bisa dipakai di dalam protokol. Agar Qwen juga bisa memakainya, tambahkan namanya ke `LOCAL_TOOL_NAMES`
dan kelompok kata kuncinya di `LOCAL_TOOL_GROUPS` (`brain.py`).

---

## 🔐 Data Pribadi

Semua data pribadi tersimpan **hanya di komputer Anda** dan tidak ikut ter-upload (lihat `.gitignore`):
`.env` (API key), `settings.json` (pengaturan Control Panel), `memory_semantic.json` (memori),
`habits.json` (kebiasaan & koreksi), `alarms.json`, `logs/`, model di `models/`, dan profil browser pemutar
`browser_profile*/`. Untuk mulai dari nol, hapus file-file tersebut.
