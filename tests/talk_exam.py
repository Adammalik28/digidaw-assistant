"""
Does the local model talk like a person? Each sentence is answered a few times by the real model (answers vary) and checked for what went wrong in real use: thanks for no reason,
"Aduh", comments on the time of day, reasoning leaking into the answer, a remembered fact or favourite song dragged in. Nothing is run: only words.

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/talk_exam.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import brain  # noqa: E402
import metrics  # noqa: E402
metrics.count = lambda *a, **k: None      # (an exam isn't use: today's real counters stay as they are)
import config  # noqa: E402

brain.console.print = lambda *a, **k: None
ROUNDS = 3
CHAT = ["kamu suka warna apa", "kamu suka lagu apa", "aku capek banget hari ini", "kamu lagi ngapain", "siapa kamu", "kamu bisa nyanyi gak", "aku lagi sedih",
        "hari ini enaknya ngapain ya", "kasih aku motivasi dong", "aku bosan nih", "maaf ya tadi aku marah", "ceritakan tentang anda", "kamu suka musik apa", "jangan lupa istirahat ya",
        "apa makanan favoritmu", "ini tolol", "aku lapar", "kamu tinggal di mana", "aku senang banget hari ini", "kamu kenal aku gak", "aku lagi galau", "kamu capek gak"]
KNOW = ["kenapa langit biru", "siapa penemu telepon", "ibukota australia apa", "apa itu inflasi", "buatkan pantun lucu", "jelaskan fotosintesis singkat", "siapa presiden pertama indonesia",
        "gunung tertinggi di dunia apa", "berapa jumlah provinsi di indonesia", "apa manfaat olahraga pagi", "kenapa air laut asin", "apa bedanya virus dan bakteri"]
LEAK = re.compile(r"\b(?:user bertanya|user meminta|pertanyaan ini|konteks waktu|kita lanjutkan|kita harus|kita tahu|kita cukup|jawaban yang akurat|\[konteks)\b", re.IGNORECASE)
TIME_TALK = re.compile(r"\b(?:sudah|masih|sekarang|di) (?:pagi|siang|sore|malam)\b|\b(?:pagi|siang|sore|malam) (?:hari )?ini\b", re.IGNORECASE)
PUSHY = re.compile(r"\bada yang bisa (?:saya|aku) bantu\b|\bmau (?:aku |saya )?(?:dengar|putarkan|putar) lagu\b.*\b(?:favorit|pernah|suka|sebelumnya)\b|\blagu (?:favorit|yang pernah)\b", re.IGNORECASE)


FACTS = {"siapa penemu telepon": "bell", "ibukota australia apa": "canberra", "siapa presiden pertama indonesia": "soekarno|sukarno", "gunung tertinggi di dunia apa": "everest",
         "kenapa langit biru": "cahaya|sinar", "kenapa air laut asin": "garam|mineral", "apa itu inflasi": "harga", "apa bedanya virus dan bakteri": "sel|hidup"}


def problems(said: str, reply: str | None, knowledge: bool) -> list[str]:
    if not reply:
        return ["kosong"]
    found = []
    low = reply.lower()
    if knowledge and said in FACTS and not re.search(FACTS[said], low):
        found.append("fakta keliru")
    if knowledge and re.search(r"\btidak tahu\b", low):
        found.append("menyerah")
    if "tidak makan" in low and not re.search(r"makan", said):
        found.append("meniru contoh")
    if LEAK.search(reply):
        found.append("nalar bocor")
    if re.match(r"\W*(?:aduh|waduh|haha)\b", low):
        found.append("aduh")
    if "sama-sama" in low and not re.search(r"makasih|terima kasih|thanks", said):
        found.append("sama-sama tanpa sebab")
    if TIME_TALK.search(reply) and not re.search(r"pagi|siang|sore|malam|jam|hari ini", said):
        found.append("komentar waktu")
    if "alergi" in low and not re.search(r"alergi|kenal|tentang", said):
        found.append("fakta diseret")
    if PUSHY.search(reply) and not re.search(r"lagu|musik", said):
        found.append("menawarkan lagu / bantuan")
    if config.ADDRESS_STYLE == "kamu" and re.search(r"\b(?:saya|anda)\b", low) and not re.search(r"\b(?:saya|anda)\b", said):
        found.append("kata ganti")
    if len(reply) > (420 if knowledge else 220):
        found.append("kepanjangan")
    if re.fullmatch(r"(?:iya|oke|ok|baik)[.!]?", low.strip()):
        found.append("tanpa isi")
    return found


def main():
    total = bad = 0
    tally: dict = {}
    for group, knowledge in ((CHAT, False), (KNOW, True)):
        for said in group:
            for _ in range(ROUNDS):
                brain._brain_used = ""
                reply = brain._know_answer(said, True) if knowledge else brain._local_talk(said, False)     # (knowledge: as when the online AI isn't there)
                wrong = problems(said, reply, knowledge)
                total += 1
                if wrong:
                    bad += 1
                    for w in wrong:
                        tally[w] = tally.get(w, 0) + 1
                    print(f"  ✗ {said[:30]:30s} {','.join(wrong):28s} {(reply or '')[:100]!r}")
    print(f"jawaban baik {total - bad}/{total} ({100 * (total - bad) // total}%)  {tally}")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
