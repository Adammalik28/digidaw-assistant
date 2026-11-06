"""
Spoken examples for the "Digidaw" detector (scripts/train_wake.py): the name and words that sound like it, in several synthetic voices.
Only these fixed words go to the voice service. Usage: make_wake_data.py <folder>
"""
import asyncio
import os
import subprocess
import sys

import edge_tts
import numpy as np

VOICES = ["id-ID-ArdiNeural", "id-ID-GadisNeural", "ms-MY-OsmanNeural", "ms-MY-YasminNeural", "jv-ID-DimasNeural", "jv-ID-SitiNeural",
          "su-ID-JajangNeural", "su-ID-TutiNeural", "en-US-GuyNeural", "en-GB-RyanNeural", "en-US-JennyNeural"]
RATES = ["-25%", "-10%", "+0%", "+15%"]
PITCHES = ["-15Hz", "+0Hz", "+15Hz"]
NAME = ["Digidaw", "Digidaw.", "Digidaw!", "Digidaw?", "Digi daw", "Hey Digidaw", "Halo Digidaw", "Digidaw, buka Firefox", "Digidaw, putar musik"]
CLOSE = ["digital", "digi", "didi", "gigi", "biji", "bidang", "dibawa", "di bawah", "di dalam", "dia dong", "gigi dong", "lagi dong", "dikit dong", "ding dong",
         "hijau daun", "biji daun", "dido", "dino", "dinda", "tidak", "tiga", "dua", "digital marketing", "bagi dua", "siapa dia", "di sana", "dikasih", "digoda", "digaji",
         "diganti", "ditawar", "diberi", "pidato", "video", "radio", "studio", "window", "shadow", "tomorrow", "hallo", "bye bye", "big data", "the guy",
         "dig it now", "did he go", "the widow", "digest", "dictator", "ya", "tidak jadi", "buka", "tutup", "oke", "stop"]
SENTENCES = ["Selamat pagi semuanya, hari ini cuacanya cerah sekali.", "Jangan lupa makan siang ya, nanti kamu sakit.", "Saya mau pergi ke pasar membeli sayur dan ikan.",
             "Buka Firefox dan putar musik yang santai.", "Tutup semua aplikasi yang tidak dipakai.", "Pindah ke workspace dua sekarang.", "Harga bahan pokok naik lagi minggu ini.",
             "Terima kasih sudah menonton video ini, jangan lupa subscribe.", "Pertandingan tadi malam berakhir dengan skor dua kosong.", "Kuliahnya dimulai jam sepuluh pagi di ruang dua puluh satu.",
             "Aku tidak tahu kenapa dia marah kepadaku kemarin.", "Lampunya tolong dimatikan kalau sudah selesai.", "Besok kita rapat dengan dosen pembimbing.",
             "The weather is really nice today, let us go outside.", "I was never there, but I remember everything you said.", "Please subscribe and leave a comment below.",
             "Berita terkini dari ibu kota, banjir melanda beberapa wilayah.", "Ayo cepat, nanti kita terlambat masuk kelas.", "Bagaimana kabar keluargamu di kampung halaman?", "Tolong kirim pesan ke ibu, bilang saya pulang malam."]


async def one(semaphore, text, voice, rate, pitch):
    async with semaphore:
        chunks = []
        try:
            async for chunk in edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
        except Exception as e:
            print("gagal", voice, text[:20], type(e).__name__, flush=True)
            return None
    if not chunks:
        return None
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "-"], input=b"".join(chunks), capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16)


async def batch(texts, rates, pitches, voices=VOICES):
    semaphore = asyncio.Semaphore(8)
    jobs = [(t, v, r, p) for t in texts for v in voices for r in rates for p in pitches]
    done = await asyncio.gather(*(one(semaphore, *job) for job in jobs))
    return [(job[1], clip) for job, clip in zip(jobs, done) if clip is not None and len(clip) > 1600]


async def main(folder):
    os.makedirs(folder, exist_ok=True)
    for name, texts, rates, pitches in (("name", NAME, RATES, PITCHES), ("close", CLOSE, ["+0%"], ["+0Hz"]), ("talk", SENTENCES, ["+0%"], ["+0Hz"])):
        if os.path.exists(os.path.join(folder, f"{name}.npz")):
            continue
        items = await batch(texts, rates, pitches)
        np.savez_compressed(os.path.join(folder, f"{name}.npz"), voices=np.array([v for v, _ in items]), **{f"c{i}": c for i, (_, c) in enumerate(items)})
        print(name, len(items), flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
