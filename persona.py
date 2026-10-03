"""
How Digidaw sounds: turns the fixed sentences skills return ("Membuka firefox, KING.") into natural, varied speech
("Oke, Firefox saya buka.") and keeps the user's title from ending every single sentence.

Skills keep returning their plain, predictable sentences (the AI and the logs rely on them); only what is spoken
and shown changes.
"""
import random
import re
import time

import config

T = config.USER_TITLE
TITLE_EVERY = 4               # say the title about once every this many replies...
TITLE_AFTER_IDLE = 600        # ...and always in the first reply after this many seconds of silence
OPENERS = ["Oke, ", "Siap, ", "Baik, ", "", ""]
# how app / site names are written (and so pronounced)
BRANDS = {"firefox": "Firefox", "chrome": "Chrome", "google chrome": "Google Chrome", "tiktok": "TikTok",
          "youtube": "YouTube", "instagram": "Instagram", "whatsapp": "WhatsApp", "whatsapp web": "WhatsApp Web",
          "facebook": "Facebook", "twitter": "Twitter", "x": "X", "github": "GitHub", "gmail": "Gmail",
          "spotify": "Spotify", "mousepad": "Mousepad", "terminal": "Terminal", "kitty": "Kitty",
          "thunar": "Thunar", "discord": "Discord", "telegram": "Telegram", "vscode": "VS Code", "code": "VS Code",
          "netflix": "Netflix", "shopee": "Shopee", "tokopedia": "Tokopedia", "chatgpt": "ChatGPT",
          "gemini": "Gemini", "claude": "Claude", "brave": "Brave", "edge": "Edge", "waydroid": "Waydroid",
          "steam": "Steam", "obs": "OBS", "vlc": "VLC", "gimp": "GIMP", "libreoffice": "LibreOffice"}

_state = {"replies": 0, "last_title": -99, "last_offer": -99, "last_at": 0.0, "last": ""}


def brand(name: str) -> str:
    n = name.strip()
    key = n.lower()
    if key in BRANDS:
        return BRANDS[key]
    return n[:1].upper() + n[1:] if n.islower() else n


def page_name(title: str) -> str:
    """'(7)TikTok - Make Your Day' -> 'TikTok'; 'Timothy Ronald meaning | TikTok — Mozilla Firefox' -> 'Timothy Ronald meaning'."""
    t = re.sub(r"\(\d+\)\s*", "", title.strip())                        # unread counters: "(13) WhatsApp"
    t = re.split(r"\s+[—–|-]\s+|\s*\|\s*", t)[0].strip()
    t = re.sub(r"^\w+:\s+(?=\S)", "", t)                                  # "WhatSie: WhatsApp" -> "WhatsApp"
    return t[:50] or title[:50]


def _pick(options: list[str], **values) -> str:
    """A random variant, never the one said last time if another exists."""
    choices = [o.format(**values) for o in options]
    fresh = [c for c in choices if c != _state["last"]] or choices
    return random.choice(fresh)


def _open(m):  # noqa: D401 — small rewrite helpers below: regex match -> sentence (no title)
    x = brand(m[1])
    return _pick(["{x} saya buka.", "membuka {x}.", "{x} meluncur.", "{x} sedang dibuka."], x=x)


def _closed(m):
    names = [brand(n) for n in re.split(r",\s*", m[1]) if "crashpad" not in n and "helper" not in n.lower()]
    x = " dan ".join(dict.fromkeys(names)) or brand(m[1])
    return _pick(["{x} sudah saya tutup.", "{x} sudah tertutup.", "beres, {x} ditutup."], x=x)


def _workspace(m):
    return _pick(["ke workspace {n}.", "sekarang di workspace {n}.", "workspace {n}."], n=m[1])


def _volume(m):
    return _pick(["volume sekarang {n} persen.", "suaranya saya atur ke {n} persen.", "volume {n} persen."], n=m[1])


def _brightness(m):
    return _pick(["kecerahan layar {n} persen.", "layarnya saya atur ke {n} persen.", "kecerahan jadi {n} persen."], n=m[1])


def _tab_closed(m):
    return _pick(["tab {x} sudah saya tutup.", "tab {x} ditutup."], x=page_name(m[1]))


def _switched(m):
    return _pick(["ini {x}.", "beralih ke {x}.", "{x} sudah di depan."], x=page_name(m[1]))


def _playing(m):
    mix = " dalam mode mix" if m[2] else ""
    return _pick(["memutar {x}{mix}. Selamat menikmati.", "ini dia, {x}{mix}.", "{x}, diputar sekarang{mix}."],
                 x=song_title(m[1]), mix=mix)


def _no_window(m):
    return _pick(["saya tidak menemukan jendela {x} yang terbuka.", "hmm, jendela {x} sepertinya tidak sedang terbuka."],
                 x=brand(m[1]))


def _moved(m):
    return _pick(["{x} sudah pindah ke workspace {n}.", "{x} saya pindahkan ke workspace {n}."], x=page_name(m[1]), n=m[2])


REWRITES = [(re.compile(p + rf",? ?(?:{re.escape(T)})?\.?$", re.IGNORECASE), fn) for p, fn in [
    (r"^Membuka (.+?)(?: di \w+)?", _open),
    (r"^(?!Tab )(.+?) sudah ditutup(?: paksa tanpa konfirmasi)?", _closed),
    (r"^Pindah ke workspace (\d+)", _workspace),
    (r"^Volume diatur ke (\d+) persen", _volume),
    (r"^Kecerahan layar diatur ke (\d+) persen", _brightness),
    (r"^Tab (.+?) sudah ditutup", _tab_closed),
    (r"^Beralih ke (?:tab )?(.+?)", _switched),
    (r"^Memutar (.+?)( dalam mode mix)?", _playing),
    (r"^Tidak ada jendela '(.+?)' yang terbuka", _no_window),
    (r"^(.+?) dipindah ke workspace (\d+)", _moved),
    (r"^Melanjutkan pemutaran", lambda m: _pick(["lanjut ya.", "dilanjutkan.", "oke, lanjut lagi."])),
    (r"^Media di-pause", lambda m: _pick(["saya jeda dulu.", "dijeda.", "oke, berhenti sebentar."])),
    (r"^Teks sudah saya ketik", lambda m: _pick(["sudah saya ketik.", "beres, sudah diketik."])),
]]
# a reply that already sounds natural is left alone; these only drop the stiff ", KING." / "KING, " parts
# every place the title sits in a reply ("..., KING." / "KING, ..." / "Halo KING!" / "— KING —"); removed, then put
# back at most once by the title policy
TITLE_ANY = re.compile(rf"(?:,\s*|\s+)\b{re.escape(T)}\b(?=\s*[,.!?;:—–-]|\s*$)|^\s*{re.escape(T)}\b[,!]?\s*", re.IGNORECASE)
# "aku/kamu" vs "saya/Anda": one consistent way of speaking (config.ADDRESS_STYLE)
TO_FORMAL = [(r"\bkamu\b", "Anda"), (r"\bkau\b", "Anda"), (r"\bengkau\b", "Anda"), (r"\baku\b", "saya"),
             (r"\bAku\b", "Saya"), (r"\bKamu\b", "Anda")]
TO_CASUAL = [(r"\bAnda\b", "kamu"), (r"\banda\b", "kamu"), (r"\bsaya\b", "aku"), (r"\bSaya\b", "Aku")]
ROBOTIC_LEADS = re.compile(r"^(?:Tentu(?: saja)?[,!.]\s*|Baiklah[,!.]\s*|Berikut (?:adalah |ini )?)", re.IGNORECASE)


INTERJECTION = re.compile(r"^(?:siap|oke|baik|beres|hmm|ini dia|sudah|sekarang)\b|\bsiap\b", re.IGNORECASE)


def song_title(title: str) -> str:
    """A title that sounds good spoken: '(Official Music Video)', '[4K]', '| Lyrics' and hashtags go, long ones are
    cut at a word boundary."""
    t = re.sub(r"\s*[\(\[][^\)\]]*(?:official|video|audio|lyric|lirik|mv|hd|4k|visuali[sz]er|live|remaster)[^\)\]]*[\)\]]",
               "", title, flags=re.IGNORECASE)
    t = re.split(r"\s+[|]\s+|\s+#", t)[0]
    t = re.sub(r"\s+-\s+YouTube$", "", t).strip(" -|,")
    if len(t) > 60:
        t = t[:60].rsplit(" ", 1)[0].rstrip(",;:-")
        t = re.sub(r"\s+(?:in|di|the|and|dan|of|full|ft|feat|-)$", "", t, flags=re.IGNORECASE)
    return t or title[:60]


def _part(text: str, first: bool) -> str:
    for pattern, fn in REWRITES:
        m = pattern.match(text.strip())
        if m:
            sentence = fn(m)
            opener = random.choice(OPENERS) if first and not INTERJECTION.search(sentence) else ""
            sentence = opener + sentence
            return sentence[:1].upper() + sentence[1:]
    return text.strip()


def _wants_title(reply: str) -> bool:
    now = time.time()
    idle = now - _state["last_at"] > TITLE_AFTER_IDLE
    due = _state["replies"] - _state["last_title"] >= TITLE_EVERY
    return idle or due or reply.rstrip().endswith("?") and random.random() < 0.5


# "-mu" means "your" only after these words; elsewhere it is part of the word (penemu, ilmu, bertemu, jamu, tamu)
MU_BASES = ("untuk|dengan|pada|kepada|bersama|tentang|milik|hari|tidur|kerja|kerjaan|tugas|lagu|musik|file|catatan|"
            "rumah|keluarga|teman|kesehatan|waktu|pilihan|selera|perasaan|semangat|hati|pikiran|komputer|laptop|"
            "pertanyaan|permintaan|jadwal|alarm|mimpi|istirahat|nama|suara|layar|aplikasi|kamar|tubuh|badan|mata|"
            "kepala|kopi|makan|sarapan|ujian|kuliah|project|proyek|kode|harapan|cerita|ide|rencana|usaha|kabar")
MU_RE = re.compile(rf"\b({MU_BASES})mu\b", re.IGNORECASE)


def _pronouns(text: str) -> str:
    casual = getattr(config, "ADDRESS_STYLE", "anda") == "kamu"
    for pattern, word in (TO_CASUAL if casual else TO_FORMAL):
        text = re.sub(pattern, word, text)
    if not casual:      # "tidurmu" -> "tidur Anda"
        text = MU_RE.sub(lambda m: f"{m[1]} Anda", text)
    return re.sub(r"(?<=[.!?]\s)(anda|kamu|aku|saya)\b", lambda m: m[1].capitalize(), text)


def _add_title(text: str) -> str:
    """Put the title back once, naturally: 'Oke, Firefox saya buka, KING.' / 'Halo, KING! ...'"""
    if re.search(rf"\b{re.escape(T)}\b", text):
        return text
    m = re.search(r"[.!?](?=\s|$)", text)
    if not m:
        return f"{text}, {T}."
    return text[:m.start()] + f", {T}" + text[m.start():]


# a closing offer ("Ada yang bisa saya bantu?") is fine now and then, tiring after every reply
OFFER_RE = re.compile(r"^(?:ada (?:lagi |hal lain )?yang (?:bisa|ingin|mau|perlu)|ada lagi\b|mau saya bantu\b|"
                      r"apa (?:lagi )?yang (?:bisa|ingin|mau) (?:saya bantu|anda lakukan|dilakukan)|butuh bantuan lain|"
                      r"kalau (?:butuh|perlu) (?:apa-apa|sesuatu|bantuan)|jika (?:butuh|perlu) (?:apa-apa|sesuatu|bantuan))",
                      re.IGNORECASE)
SELF_PRAISE_RE = re.compile(r"^[^.!?]*\b(?:selalu (?:siap|ada|di sini)|siap membantu kapan saja|asisten (?:terbaik|"
                            r"yang (?:hebat|canggih|pintar)))\b[^.!?]*[.!?]?$", re.IGNORECASE)
OFFER_GAP = 4                  # replies between two closing offers


def _modest(text: str, commit: bool = False) -> str:
    """Drop a closing offer if one was made recently, and drop self-praise ("saya selalu siap membantu")."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    if len(sentences) < 2:
        return text             # a reply that IS only the question ("Ada yang bisa saya bantu?") stays
    kept = []
    for i, sentence in enumerate(sentences):
        if SELF_PRAISE_RE.match(sentence):
            continue
        if i == len(sentences) - 1 and OFFER_RE.search(sentence):
            if _state["replies"] - _state["last_offer"] < OFFER_GAP:
                continue
            if commit:
                _state["last_offer"] = _state["replies"]
        kept.append(sentence)
    return " ".join(kept) or text


CANDIDATES = 10                # wordings tried per reply; one whose voice is already cached plays instantly
is_cached = None               # set by the voice module: text -> True when its audio is in the cache


def _render(reply: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", reply.strip())
    rewritten = []
    for part in parts:
        rewritten.append(_part(part, first=not rewritten))
    text = " ".join(rewritten)
    text = re.sub(r"([?!])\.", r"\1", text)            # "Do I Wanna Know?." -> "Do I Wanna Know?"
    text = TITLE_ANY.sub("", text).strip()
    text = ROBOTIC_LEADS.sub("", text)
    text = _pronouns(text)
    text = _modest(text)
    text = re.sub(r"!(?=\s|$)", ".", text, count=max(0, text.count("!") - 1))   # at most one "!"
    return text[:1].upper() + text[1:]


def variants(reply: str, tries: int = CANDIDATES) -> list[str]:
    """Different natural wordings of one reply (without the title)."""
    seen = []
    for _ in range(tries):
        text = _render(reply)
        if text not in seen:
            seen.append(text)
    return seen


def speak(reply: str) -> str:
    """The final wording of a reply: natural skill sentences, the title only now and then. Among the possible
    wordings one whose voice is already cached is preferred, so frequent replies are spoken without delay."""
    if not reply or reply.startswith("Apakah Anda yakin"):     # confirmations stay exactly as they are
        return reply
    options = variants(reply)
    want_title = _wants_title(options[0])
    titled = [_add_title(o) for o in options]
    cached = lambda texts: [t for t in texts if is_cached and is_cached(t)]
    if want_title and cached(titled):
        text = random.choice(cached(titled))
    elif cached(options) and not (want_title and time.time() - _state["last_at"] > TITLE_AFTER_IDLE):
        text, want_title = random.choice(cached(options)), False       # instant voice beats the title this time
    else:
        text = titled[0] if want_title else options[0]
    _modest(text, commit=True)
    _state["replies"] += 1
    if want_title:
        _state["last_title"] = _state["replies"]
    _state["last_at"] = time.time()
    _state["last"] = text
    return text


def warm_phrases() -> list[str]:
    """Replies worth having ready in the voice cache: the user's favourite apps and sites, workspaces and the
    everyday answers (a few wordings each)."""
    import habits
    replies = [f"Membuka {brand(n)}, {T}." for n in habits.favourites("app", 8) + habits.favourites("web", 6)]
    replies += [f"{brand(n)} sudah ditutup, {T}." for n in habits.favourites("app", 6)]
    replies += [f"Pindah ke workspace {n}, {T}." for n in range(1, 7)]
    replies += [f"Volume diatur ke {n} persen, {T}." for n in (10, 20, 30, 40, 50, 60, 70, 80, 100)]
    replies += [f"Melanjutkan pemutaran, {T}.", f"Media di-pause, {T}.", f"Teks sudah saya ketik, {T}."]
    phrases = []
    for reply in replies:
        phrases += variants(reply, tries=6)[:3]
    return phrases
