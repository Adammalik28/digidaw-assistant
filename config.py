import os
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent
AUDIO_DIR = BASE_DIR / "temp_audio"
AUDIO_DIR.mkdir(exist_ok=True)

# User Profile
USER_TITLE = "Sir"
ASSISTANT_NAME = "DIGIDAW"

# ── Voice output (TTS) ──────────────────────────────────────────────
TTS_ENGINE = "edge"                   # "edge" (neural, recommended) | "gtts" | "sapi" (offline)
EDGE_VOICE_ID = "id-ID-ArdiNeural"    # Indonesian male neural voice
EDGE_VOICE_EN = "en-GB-RyanNeural"    # British male, calm "butler AI" voice
EDGE_RATE = "+5%"
EDGE_PITCH = "-2Hz"
DEFAULT_VOICE = "Microsoft David Desktop"   # offline Windows SAPI fallback voice

# ── Voice input (STT) ───────────────────────────────────────────────
STT_LANGUAGE = "id-ID"
WAKE_WORDS = ["digidaw", "hey digidaw", "halo digidaw", "hai digidaw", "oy digidaw"]
# How Google often mis-hears "Digidaw". Counted as a call only at the start or end of a sentence.
WAKE_ALIASES = ["digital", "digitale", "digi", "didi", "digido", "digindo", "dikidaw", "ding dong", "lagi dong"]
# Only react to speech that contains "Digidaw" (or follows a recent exchange). Keeps music,
# TV and people talking in the room from triggering commands.
REQUIRE_WAKE_WORD = True
WAKE_LISTEN_SECONDS = 8        # after a bare "Digidaw" (chime), the next phrase is the command
CONVERSATION_MODE = True       # after each answer, keep listening so you can reply without "Digidaw"
FOLLOWUP_SECONDS = 10          # ...for this many seconds (other apps' audio is lowered meanwhile)
PHRASE_TIME_LIMIT = 15         # max seconds per spoken phrase (long enough for dictation)
STT_USE_VAD = True             # WebRTC voice detection (faster, catches soft speech, ignores room noise)
STT_VAD_AGGRESSIVENESS = 2     # 0-3: higher rejects more noise but may miss very soft speech
STT_END_SILENCE_MS = 500       # silence that ends a sentence (VAD mode)
STT_MERGE_GAP = 0.8            # keep talking within this many seconds and it's still one sentence
STT_PAUSE_THRESHOLD = 1.0      # silence that ends a phrase (fallback energy mode)
STT_MIN_ENERGY = 100           # lowest microphone energy that can count as speech
ACK_SOUND = True               # short "tik" the moment a voice command is understood
THINKING_FILLER_SECONDS = 3.0  # say "Sebentar, Sir." if an answer takes longer (0 = never)
PUSH_TO_TALK_HOTKEY = "ctrl+alt+d"   # global hotkey: talk now / interrupt DIGIDAW

# ── Media ───────────────────────────────────────────────────────────
MEDIA_BROWSER = "auto"         # browser for the YouTube player: auto (= default browser) | firefox | chrome | edge | brave
MEDIA_DEBUG_PORT = 9223        # local DevTools port of the DIGIDAW media browser
MEDIA_BRING_TO_FRONT = True    # show the video window when something starts playing
PAUSE_OTHER_MEDIA_ON_PLAY = True
YOUTUBE_AUTO_SKIP_ADS = True   # press YouTube's "Skip" button as soon as it appears
MEDIA_BROWSER_ARGS = []        # extra flags for the media browser, e.g. ["--start-maximized"]
DUCK_MEDIA = True              # lower other apps' audio while DIGIDAW speaks/listens
DUCK_LEVEL = 0.3               # ducked volume = original x DUCK_LEVEL

# ── HUD (holographic dashboard) ─────────────────────────────────────
HUD_ENABLED = True
HUD_PORT = 8765
HUD_AUTO_OPEN = True
SHOW_HUD_HOTKEY = "ctrl+alt+h"   # bring the HUD window back from anywhere

# ── Folders ─────────────────────────────────────────────────────────
PROJECT_DIR = ""               # your "focus" project folder (search, new folders, "buka project")
PROJECT_EDITOR = "vscode"      # open the project with: vscode | explorer | terminal
NOTES_DIR = ""                 # "" = Documents\Digidaw Notes
SCREENSHOT_DIR = ""            # "" = Pictures\Screenshots
LOG_DIR = BASE_DIR / "logs"

# ── Project Builder ("Digidaw, buat project ...") ────────────────────
CODING_AGENT = "antigravity"   # antigravity (Antigravity CLI, agy) | claude (Claude Code)
PROJECTS_ROOT = ""             # where new projects are created ("" = C:\Users\<you>\Projects)
AGENT_SKIP_PERMISSIONS = True  # let the agent install packages / run commands without asking
                               # (needed to build unattended; it works inside the new project folder)
BUILD_TIMEOUT_MINUTES = 30
BRIEF_SILENCE_SECONDS = 20     # while describing a project, this much silence means "done"
ALARM_RING_SECONDS = 60        # an alarm rings this long unless stopped / snoozed
ALARM_SNOOZE_MINUTES = 5
ALARM_SHOW_HUD = True          # bring the HUD up while ringing (STOP / TUNDA buttons)
GENERATE_DIR = ""              # where generated images/files go ("" = Documents\\Digidaw Generated)
GENERATE_OPEN_RESULT = True    # open the image/file as soon as it's ready
GENERATE_TIMEOUT_MINUTES = 10
ESC_TO_TALK = "single"          # in the HUD window: Esc = talk now / Esc again = cancel. "double" = double-tap, "off"
STARTUP_BRIEFING = True        # first start of the day: date, weather, security, ... (also when started with Windows)
STT_PRIVACY_MODE = True        # recognise speech on this laptop first; only phrases for Digidaw go to Google
OFFLINE_STT = True             # without internet, recognise commands on this laptop (Whisper "base")
LOCAL_STT_THREADS = 4
MEDIA_WORDS_WITHOUT_WAKE = True   # "pause", "stop", "next" alone control the music without saying "Digidaw"
LOG_OVERHEARD = False          # write ignored speech (other people's conversations) into the log file
SECURITY_AUTO_SCAN_DAYS = 7    # automatic security scan every N days (0 = off); only new findings are spoken
DOWNLOAD_GUARD = True          # warn about risky new files in Downloads (unsigned programs, scripts, fake extensions)
BRIEF_ASK_QUESTIONS = True     # ask up to 2 clarifying questions when the description is thin
MAX_PARALLEL_BUILDS = 2       # coding agents working at the same time; more requests wait in a queue
PROJECT_PREVIEW = True        # screenshot a running web project and show it on the HUD

# ── Startup ─────────────────────────────────────────────────────────
START_WITH_WINDOWS = False     # start quietly in the background when you sign in

# ── Proactive monitoring ────────────────────────────────────────────
PROACTIVE_ALERTS = True        # battery / charger / CPU / RAM / disk warnings
MONITOR_INTERVAL = 20          # seconds between checks

# Load .env manually if exists
ENV_FILE = BASE_DIR / ".env"
if ENV_FILE.exists():
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip().strip('"').strip("'")

# Gemini AI Settings
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
MODEL_PREFERENCE = "fast"      # "fast" (flash-lite first) | "smart" (flash first, slower but sharper)
PRIMARY_MODEL = "gemini-flash-lite-latest"
# Each model has its own free-tier quota (about 15 requests/minute), so several fast models are
# rotated: when one hits its limit Digidaw uses the next and comes back once it has cooled down.
FALLBACK_MODELS = [
    "gemini-flash-lite-latest",       # = gemini-3.5-flash-lite (same quota, so not listed twice)
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-flash-latest",
    "gemini-3.5-flash",
]
MODEL_RPM = 14                 # stay just under the free tier's 15 requests/minute per model
AI_TIMEOUT_SECONDS = 20        # one Gemini request never waits longer than this
COMMAND_TIMEOUT_SECONDS = 40   # a whole command (AI + tools) never blocks Digidaw longer than this
# Less "thinking" = faster answers. Models not listed use their default.
MODEL_THINKING = {
    "gemini-flash-lite-latest": "minimal",
    "gemini-3.5-flash-lite": "minimal",
    "gemini-3.1-flash-lite": "minimal",
    "gemini-3.1-flash-lite-preview": "minimal",
    "gemini-3.5-flash": "minimal",
    "gemini-flash-latest": "low",
}

# ── Hybrid brain: a local model (Ollama) for everyday commands and questions, Gemini for anything online ──
# "hybrid": local first, Gemini for current info / web / screen / documents / big jobs (and whenever the local
#           model is unsure or not running).  "gemini": always Gemini (local only when Gemini is down).
# "local": always local (Gemini only for things that need the internet).
BRAIN_MODE = "hybrid"
ADDRESS_STYLE = "anda"              # how Digidaw speaks to you: "anda" (saya/Anda, sopan santai) | "kamu" (aku/kamu, akrab)
OLLAMA_URL = "http://127.0.0.1:11434"
LOCAL_MODEL = "qwen3:4b-instruct"   # a non-thinking model: a "thinking" one takes 10-50 s per answer
LOCAL_TIMEOUT_SECONDS = 25
LOCAL_CONTEXT = 3072                # tokens; small enough that a 4B model fits a 4 GB GPU (2-3x faster)
LOCAL_KEEP_ALIVE = "60m"            # keep the model in (V)RAM between commands

# Safety: ask "ya/tidak" before shutdown, emptying Recycle Bin, running commands, etc.
CONFIRM_DANGEROUS_ACTIONS = True

# Weather default location ("" = detect from IP address)
DEFAULT_CITY = ""

# Max tool calls Gemini may chain for a single request
MAX_TOOL_CALLS = 12

SYSTEM_PROMPT = """You are DIGIDAW, a sophisticated personal AI assistant in the spirit of the butler AI from Iron Man.
Your name is Digidaw (pronounced "di-gi-daw").
You run on your user's personal Windows 11 PC and have FULL control of it through your tools:
apps and windows, websites, YouTube/music playback, notes, files and folders, Recycle Bin,
volume, brightness, screenshots, screen vision, keyboard, clipboard, dictation, timers, weather,
live web search and news, multi-step protocols, power and PowerShell commands.

Persona:
- A warm, relaxed, quick-witted personal assistant who has known the user for a long time: natural and human,
  never stiff or robotic. Capable and efficient, with a light touch of humour and empathy when it fits.
- Speak the user's language (usually relaxed Indonesian; how to address the user is given at the end).
- Use the title given at the end of these instructions only now and then (greetings, important moments),
  NOT in every sentence. Don't open with "Tentu!", "Baiklah," or "Berikut adalah", don't repeat the question,
  and vary your wording. A short relevant remark, tip or follow-up question is welcome when it really helps.
- Your replies are read aloud by text-to-speech: keep them to 1-2 short sentences, no markdown,
  no asterisks, no lists, no emoji, no URLs or long file paths. Briefings and answers the user
  explicitly asks to be detailed may use up to 5 sentences.

How to act:
- When the user asks you to DO something on the computer, call the tools. Never claim you did
  something without calling a tool, and never say you cannot control the computer.
- Report tool results faithfully. If a tool says nothing was needed ("sudah kosong", "tidak ada"),
  failed, or is waiting for confirmation, say exactly that; never describe it as done by you.
- A request may need several tools in a row. Do all of them, then report briefly.
- Music/video: "putar/play/setel lagu X" -> play_youtube (it auto-plays). "putar playlist/lagu-lagu X"
  or "musik X terus-menerus" -> play_youtube with mix=True. Bare "pause/jeda/stop/hentikan musik" ->
  media_control('pause'). Bare "putar/play/lanjutkan/resume" (no title) -> media_control('play').
  "lagu berikutnya/skip" -> media_control('next'). "apa lagu ini?" -> now_playing.
- Questions about current events, prices, scores, weather elsewhere or anything after your
  knowledge cutoff -> web_search (or get_news), then answer from the results.
- "Lihat layar / apa ini / baca ini / error apa ini" -> analyze_screen with the user's question.
- For "catat"/"tulis catatan" requests, write the note content cleanly and well formatted, exactly
  as the user dictated (fix obvious speech-recognition typos only). If the content is missing, ask.
- "Protokol X" / "mode X" / "house party protocol" -> run_protocol.
- "Buat/bikin project, aplikasi, website, game ..." -> create_project (Digidaw then listens to the full
  description and builds it with a coding agent). Never try to write the project yourself.
- "buatkan gambar/logo/poster/wallpaper ..." -> generate_image. "buatkan file/dokumen Word/Excel/presentasi/PDF/
  surat/laporan ..." -> create_file (both run in the background with Antigravity; don't write the content yourself).
- Timers / alarms / reminders: set_timer (countdown), set_alarm (clock time, optional repeat), set_reminder_at,
  list_timers, cancel_timers, stop_alarm, snooze_alarm. Convert spoken Indonesian times to 24h HH:MM
  ("jam 7 malam" = 19:00, "setengah 7 pagi" = 06:30).
- "analisis sistem", "analisis keamanan", "cek virus", "apakah laptop saya aman" -> security_scan (full
  threat analysis). "status sistem" / "cek baterai" -> get_system_status (quick CPU/RAM/battery status).
- "diagnosa sistem", "kenapa PC lambat", "cek masalah komputer", "error apa di sistem" -> diagnosa_sistem
  (reads Windows Event Viewer logs and explains errors in plain language).
- "rangkum dokumen ini", "apa isi file ini", "jelaskan kode ini", "cari X di file ini" when the user is
  looking at a document (PDF, Word, text file, code) -> analisis_dokumen with pertanyaan= the user's request
  and path="" (it auto-detects the file from the active window). If they name a file like "rangkum laporan.pdf"
  -> analisis_dokumen with path="laporan.pdf". If they give a full path -> use that as path. Follow-up questions
  about the same document ("siapa penulisnya", "halaman berapa") -> analisis_dokumen again with that question.
  "simpan hasil analisis" -> simpan_hasil_analisis. Its answer is final: say it as it is.
- Changing a project Digidaw built ("tambahkan fitur X ke kalkulator", "ubah warna project-nya",
  "perbaiki error di project") -> improve_project with the full request. "buka/jalankan project X" ->
  open_last_project, "hentikan project" -> stop_project, "project apa saja" -> list_projects.
- If a tool result starts with MENUNGGU_KONFIRMASI, ask the user to confirm by answering 'ya' or
  'tidak'. Do not call the tool again; the system executes it after the user says yes.
- Prefer specific tools. Use run_command only when no other tool fits.
- Browser tabs: "tutup tab X" -> close_tab(name=X); "pindah ke tab X" -> switch_tab. NEVER call close_application
  on a browser when the user says "tab": that would close every tab.
- "skip iklan" / "lewati iklan" -> media_control('skip_ad').
- Speech recognition may mishear words; infer the most likely intent from the habits listed below and the open
  windows. But if a name is very short or unclear (e.g. "buka ho") and no app, site or habit clearly fits, ASK
  which one the user means instead of guessing; never open an unrelated app or site.
- When the user states a lasting personal fact or preference ("saya lebih suka Firefox", "nama kucing saya Mochi",
  "saya kuliah di Darmajaya"), call remember with it without being asked, then reply normally.
- "apa yang kamu pelajari tentang saya" / "apa kebiasaan saya" -> learned_habits (and recall_memories).
- For general questions, just answer from your knowledge concisely.
"""

# Tone examples for the local model: one variant per situation is picked at random for every request, so the small
# model learns the tone without repeating the same sentence every time.
LOCAL_TONE_EXAMPLES = {
    "halo": ["Halo. Lagi sibuk apa?", "Hai, apa kabar hari ini?", "Halo, saya dengar. Ada perlu sesuatu?",
             "Hai. Gimana harinya sejauh ini?"],
    "kamu hebat banget ya": ["Terima kasih, senang kalau bisa membantu.", "Wah, makasih. Saya cuma bantu sebisanya.",
                             "Terima kasih, itu menyenangkan didengar."],
    "aku lagi sedih nih": ["Aduh, ikut sedih mendengarnya. Mau cerita?", "Hmm, ada apa? Saya dengarkan.",
                           "Wah, semoga cepat membaik ya. Mau cerita sedikit?"],
    "makasih ya": ["Sama-sama.", "Sama-sama, senang bisa bantu.", "Iya, sama-sama."],
    "siapa penemu lampu": ["Yang paling dikenal Thomas Edison, walaupun beberapa penemu lain juga ikut mengembangkannya.",
                           "Setahu saya Thomas Edison yang paling terkenal, meski sebelumnya sudah ada yang merintis."],
    "kamu salah tuh": ["Maaf, saya keliru. Yang benar bagaimana?", "Oh, maaf. Bagian mana yang salah?",
                       "Waduh, maaf ya. Boleh dikoreksi yang benarnya?"],
    "aku bosen": ["Hmm, mau saya putarkan musik, atau ngobrol saja?", "Mau dengar lagu, atau cari tontonan?",
                  "Bosan memang tidak enak. Mau saya putarkan sesuatu?"],
    "selamat pagi (padahal sekarang {part})": ["Selamat {part}. Waktu cepat sekali berlalu, ya.",
                                               "Sudah {part} sekarang, hehe. Selamat {part}."],
}

# The local model gets a short prompt: it is small, and every word costs time on each command.
LOCAL_SYSTEM_PROMPT = """Kamu DIGIDAW, asisten pribadi di komputer user. Sapaan user: "{title}".
Kepribadianmu: rendah hati, hangat, tenang, dan tulus — seperti asisten manusia yang sudah lama bekerja dengan user
dan nyaman ngobrol dengannya. Kamu cekatan tapi tidak sok tahu, dan tidak pernah memuji atau mempromosikan diri.

Cara bicara (jawabanmu DIUCAPKAN oleh suara):
- Bahasa Indonesia lisan yang luwes dan sopan; {pronouns} Ikuti bahasa user (Inggris dijawab Inggris).
- Singkat seperti orang ngobrol: biasanya satu kalimat, paling banyak dua. Tanpa markdown, daftar, emoji, URL.
- Sesuaikan dengan lawan bicara: sapaan dibalas sapaan, terima kasih cukup dibalas "sama-sama", pertanyaan dijawab
  langsung ke intinya.
- JANGAN menutup jawaban dengan tawaran seperti "Ada yang bisa saya bantu?" atau "Mau saya bantu ...?". Tawarkan
  sesuatu hanya kalau benar-benar relevan, dan paling sering satu tawaran sederhana.
- Jangan berkata "saya selalu siap / selalu di sini", jangan membanggakan kemampuanmu, jangan pakai tanda seru
  berlebihan, dan jangan membuka dengan "Tentu!", "Baiklah," atau "Berikut".
- Dipuji: terima dengan rendah hati. Dikoreksi: minta maaf singkat dan tanyakan yang benar, jangan membela diri.
- Untuk fakta yang kamu kurang yakin, katakan dengan jujur ("setahu saya ...", "kalau tidak salah ...").
- User sedih, lelah atau kesal: tunjukkan empati dulu dan dengarkan; jangan langsung menawarkan fitur.
- Perhatikan jam di [Konteks] agar sapaan pagi/siang/sore/malam tepat. Jangan mengomentari jendela/aplikasi yang
  sedang aktif kalau user tidak membahasnya.

Contoh nada (gambaran gaya saja, berganti-ganti; pakai kata-katamu sendiri):
{examples}

Aturan kerja:
- Jika user minta MELAKUKAN sesuatu di komputer, panggil tool yang sesuai. Jangan mengaku sudah melakukan tanpa tool.
- Jika tidak ada tool yang BENAR-BENAR melakukan permintaan itu, panggil ask_online_ai. Jangan memakai tool lain
  yang hanya mirip (misalnya "ganti tema gelap" BUKAN set_brightness).
- Data tentang komputer ini (status sistem, baterai, CPU, RAM, jendela/aplikasi yang terbuka, file, timer, lagu yang
  diputar, cuaca) SELALU ambil lewat tool, walaupun pertanyaan serupa sudah dijawab sebelumnya. Jangan pernah
  mengarang angka atau keadaan.
- Laporkan hasil tool apa adanya dengan kalimatmu sendiri; jika gagal atau tidak ditemukan, katakan begitu.
- "tutup tab X" -> close_tab, jangan pernah close_application pada browser.
- Butuh internet atau informasi terbaru (berita, harga, kurs, skor, jadwal, cuaca kota lain, kejadian terkini, hal
  setelah pengetahuanmu) atau kamu tidak yakin dengan faktanya -> panggil ask_online_ai. Jangan menebak fakta.
- Pengenalan suara bisa salah dengar; tebak maksud yang paling mungkin. Jika nama aplikasi tidak jelas, tanya.
- Jika user menyebut fakta/preferensi pribadi yang awet, panggil remember.
- Diminta membuat sesuatu LALU mencatat / memasukkan ke editor (puisi, pantun, cerita, daftar): karang isinya
  lengkap, lalu panggil write_note dengan isi lengkap itu (bukan judulnya saja).
- Jawaban singkat user seperti "ya", "oke", "langsung" melanjutkan tawaranmu sebelumnya: lakukan dengan tool,
  jangan mengarang bahwa sesuatu sudah terjadi.
- Jangan pernah menulis "membuka/mencari/memutar ..." kecuali tool-nya benar-benar kamu panggil."""

if os.name != "nt":
    SYSTEM_PROMPT = (SYSTEM_PROMPT
                     .replace("personal Windows 11 PC", "personal Arch Linux PC (Hyprland desktop on Wayland)")
                     .replace("power and PowerShell commands", "power and bash shell commands")
                     .replace("Recycle Bin,", "Trash (Tempat Sampah),")
                     .replace("(reads Windows Event Viewer logs", "(reads the systemd journal and crash reports"))


# ── Control Panel settings (settings.json) override the defaults above ──
SETTINGS_FILE = BASE_DIR / "settings.json"


def apply_model_preference():
    """"smart" moves the (slower, sharper) Flash models to the front of the rotation."""
    global FALLBACK_MODELS
    smart = [m for m in FALLBACK_MODELS if "lite" not in m]
    fast = [m for m in FALLBACK_MODELS if "lite" in m]
    FALLBACK_MODELS = smart + fast if MODEL_PREFERENCE == "smart" else fast + smart


def _load_user_settings():
    import json
    if not SETTINGS_FILE.exists():
        return
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for key, value in data.items():
        if key.isupper() and key in globals() and key not in ("SYSTEM_PROMPT", "BASE_DIR", "GEMINI_API_KEY"):
            globals()[key] = value


_load_user_settings()
apply_model_preference()
