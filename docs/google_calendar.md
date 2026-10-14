# Menyambungkan agenda Digidaw ke Google Calendar

Sekali saja, sekitar 5 menit. Setelah itu setiap acara yang Anda tambahkan ke Digidaw otomatis muncul di
kalender **"Digidaw"** di Google Calendar (HP, web, dan perangkat lain), lengkap dengan notifikasinya.

Digidaw hanya meminta izin `calendar.app.created`: ia hanya bisa mengelola kalender yang ia buat sendiri,
tidak bisa membaca atau mengubah kalender Anda yang lain.

## 1. Buat project dan aktifkan Calendar API
1. Buka <https://console.cloud.google.com/> dan masuk dengan akun Google Anda.
2. Klik pemilih project di atas → **New Project** → nama: `Digidaw` → **Create**.
3. Buka <https://console.cloud.google.com/apis/library/calendar-json.googleapis.com> → **Enable**.

## 2. Layar persetujuan (OAuth consent screen)
1. Buka **APIs & Services → OAuth consent screen** (atau "Google Auth Platform").
2. User type: **External** → isi nama aplikasi `Digidaw` dan email Anda.
3. Di **Audience**, tambahkan email Anda sebagai **Test user**.
4. Lalu klik **Publish app** (status *In production*). Kalau dibiarkan *Testing*, Google mencabut izinnya tiap
   7 hari dan Anda harus menyambung ulang. Peringatan "Google hasn't verified this app" saat masuk itu wajar
   untuk aplikasi pribadi: klik **Advanced → Go to Digidaw**.

## 3. Buat kunci OAuth
1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Desktop app**, nama: `Digidaw` → **Create** → **Download JSON**.
3. Simpan file itu sebagai:
   ```bash
   mkdir -p ~/.config/digidaw
   mv ~/Downloads/client_secret_*.json ~/.config/digidaw/google_client.json
   chmod 600 ~/.config/digidaw/google_client.json
   ```

## 4. Sambungkan
Ucapkan **"Digidaw, hubungkan Google Calendar"**. Browser terbuka → pilih akun → izinkan.
Digidaw membuat kalender "Digidaw" dan mengisinya dengan agenda Anda.

Perintah lain: "status Google Calendar", "putuskan Google Calendar".
Alternatif tanpa suara: `~/.local/share/digidaw/venv/bin/python gcal_sync.py login`
