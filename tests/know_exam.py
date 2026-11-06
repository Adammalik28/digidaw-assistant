"""
What does the local model really know? General questions with a checked answer (a pattern in the reply). Nothing is run: only words. Used to decide whether it may answer these itself.

Run:  env -u DIGIDAW_NO_CONSOLE SDL_AUDIODRIVER=dummy PYTHONPATH=. ~/.local/share/digidaw/venv/bin/python tests/know_exam.py
"""
QUESTIONS = [
    # Indonesia
    ("siapa presiden pertama indonesia", "soekarno|sukarno"), ("siapa wakil presiden pertama indonesia", "hatta"), ("kapan indonesia merdeka", "1945"), ("siapa pencipta lagu indonesia raya", "supratman"),
    ("ibu kota jawa barat apa", "bandung"), ("ibu kota lampung apa", "bandar lampung|tanjung ?karang"), ("pulau terbesar di indonesia apa", "kalimantan|papua|sumatera|sumatra"), ("candi borobudur ada di mana", "magelang|jawa tengah"),
    ("siapa penulis laskar pelangi", "andrea"), ("danau terbesar di indonesia apa", "toba"), ("gunung tertinggi di indonesia apa", "jaya|cartenz|carstensz"), ("mata uang indonesia apa", "rupiah"),
    ("dasar negara indonesia apa", "pancasila"), ("siapa pahlawan emansipasi wanita indonesia", "kartini"), ("bahasa resmi indonesia apa", "indonesia"),
    # geography
    ("ibu kota jepang apa", "tokyo"), ("ibukota australia apa", "canberra"), ("ibu kota malaysia apa", "kuala lumpur"), ("ibu kota prancis apa", "paris"), ("ibu kota korea selatan apa", "seoul"),
    ("gunung tertinggi di dunia apa", "everest"), ("sungai terpanjang di dunia apa", "nil|amazon"), ("samudra terluas apa", "pasifik"), ("benua terbesar apa", "asia"), ("negara terluas di dunia apa", "rusia"),
    # science
    ("planet terdekat dari matahari apa", "merkurius"), ("planet terbesar di tata surya apa", "jupiter|yupiter"), ("rumus kimia air apa", "h.?2.?o"), ("berapa kecepatan cahaya", "300|299"), ("hewan tercepat di darat apa", "cheetah"),
    ("kenapa langit biru", "hambur|cahaya|sinar"), ("kenapa air laut asin", "garam|mineral"), ("apa itu fotosintesis", "cahaya|tumbuhan"), ("gas yang kita hirup untuk bernapas apa", "oksigen"), ("organ pemompa darah apa", "jantung"),
    ("berapa jumlah tulang manusia dewasa", "206"), ("berapa hari dalam setahun", "365"), ("berapa derajat air mendidih", "100"), ("siapa penemu telepon", "bell"), ("siapa penemu lampu pijar", "edison"),
    ("siapa penemu teori relativitas", "einstein"), ("siapa penulis harry potter", "rowling"), ("siapa pendiri microsoft", "gates"), ("siapa pendiri facebook", "zuckerberg"), ("siapa pelukis mona lisa", "vinci"),
    # meanings and language
    ("apa itu inflasi", "harga"), ("apa itu demokrasi", "rakyat"), ("apa itu algoritma", "langkah|urutan|instruksi"), ("apa itu cash flow", "arus|aliran|masuk"), ("apa bedanya virus dan bakteri", "sel|hidup"),
    ("apa bahasa inggrisnya kucing", "cat"), ("apa bahasa inggrisnya buku", "book"), ("terjemahkan selamat pagi ke inggris", "good morning"), ("apa arti thank you", "terima kasih"), ("apa bahasa jepangnya terima kasih", "arigato"),
    # sums and reasoning
    ("berapa 15 kali 15", "225"), ("berapa 144 dibagi 12", "12"), ("akar kuadrat dari 81 berapa", "9"), ("kalau sekarang jam 10 dan 3 jam lagi jam berapa", "13|1 siang|1"), ("berapa jumlah sisi segitiga", "3|tiga"),
]

# written before knowledge.py's facts were: the score to quote. By kind, to decide what the local model may answer on its own.
FRESH = {
    "indonesia": [("presiden keempat indonesia siapa", "wahid|gus dur"), ("presiden ketiga indonesia siapa", "habibie"), ("ibu kota sulawesi selatan apa", "makassar"), ("ibu kota sumatera barat apa", "padang"),
                  ("ibu kota kalimantan barat apa", "pontianak"), ("ibu kota papua apa", "jayapura"), ("ibu kota bali apa", "denpasar"), ("ibu kota jawa timur apa", "surabaya"), ("kapan hari kartini", "21 april"),
                  ("kapan sumpah pemuda", "28 oktober|1928"), ("apa semboyan indonesia", "bhinneka"), ("lambang negara indonesia apa", "garuda"), ("sungai terpanjang di indonesia apa", "kapuas"), ("danau toba ada di provinsi mana", "sumatera utara|sumatra utara|sumut"),
                  ("candi prambanan ada di mana", "yogyakarta|sleman|klaten|jawa tengah"), ("pulau komodo ada di provinsi mana", "nusa tenggara timur|ntt"), ("berapa provinsi di indonesia", "38"), ("siapa bapak pendidikan indonesia", "dewantara"), ("kapan hari pahlawan", "10 november"), ("indonesia punya berapa zona waktu", "3|tiga")],
    "dunia": [("ibu kota jerman apa", "berlin"), ("ibu kota mesir apa", "kairo"), ("ibu kota thailand apa", "bangkok"), ("negara terkecil di dunia apa", "vatikan|vatican"), ("gurun terluas di dunia apa", "sahara|antartika"), ("mata uang jepang apa", "yen"), ("negara dengan penduduk terbanyak apa", "india|tiongkok|china|cina")],
    "ilmu": [("planet merah itu planet apa", "mars"), ("logam cair pada suhu kamar apa", "raksa"), ("berapa jumlah planet di tata surya", "8|delapan"), ("siapa penemu gravitasi", "newton"), ("siapa penemu penisilin", "fleming"), ("satuan gaya apa", "newton"), ("hewan terbesar di dunia apa", "paus biru"), ("kenapa daun berwarna hijau", "klorofil"), ("kenapa besi berkarat", "oksigen|oksidasi|air"), ("berapa kaki laba laba", "8|delapan")],
    "arti": [("apa itu blockchain", "catatan|buku besar|data|transaksi"), ("apa itu vaksin", "kekebalan|imun"), ("apa itu resesi", "ekonomi"), ("apa itu api dalam pemrograman", "antarmuka|interface|aplikasi"), ("apa arti merdeka", "bebas"), ("apa itu gravitasi", "gaya|tarik"), ("apa bedanya hardware dan software", "fisik|perangkat|program")],
    "bahasa": [("apa bahasa inggrisnya rumah", "house|home"), ("apa bahasa inggrisnya air", "water"), ("apa bahasa indonesianya dog", "anjing"), ("terjemahkan aku cinta kamu ke inggris", "i love you"), ("apa arti good night", "selamat malam"), ("terjemahkan how are you ke indonesia", "apa kabar|bagaimana kabar"), ("bahasa arab terima kasih apa", "syukran")],
    "hitung": [("berapa 25 persen dari 200", "50"), ("berapa 17 ditambah 28", "45"), ("1 jam berapa detik", "3600|3\\.600"), ("berapa setengah dari 90", "45"), ("berapa 7 kali 8", "56")],
}


# written after knowledge.py was finished, meant to reach past it: the score that says something. Trick questions: right = handed to the online AI or says there's no such thing.
FRESH2 = {
    "indonesia": [("siapa gubernur pertama jakarta", "suwiryo|soewirjo|sumarno|soemarno"), ("siapa menteri pendidikan pertama indonesia", "dewantara"), ("apa nama bandara di bali", "ngurah rai"), ("apa nama pelabuhan utama jakarta", "priok"),
                  ("berapa sila pancasila", "5|lima"), ("sila ketiga pancasila apa", "persatuan"), ("siapa pencipta lagu ibu kita kartini", "supratman"), ("siapa pencipta lagu hari merdeka", "mutahar"), ("kota pahlawan itu kota apa", "surabaya"),
                  ("kota kembang itu kota apa", "bandung"), ("kota gudeg itu kota apa", "yogya|jogja"), ("kota hujan itu kota apa", "bogor"), ("pulau dewata itu pulau apa", "bali"), ("suku terbesar di indonesia apa", "jawa"), ("agama mayoritas di indonesia apa", "islam"),
                  ("gunung tertinggi di sumatera apa", "kerinci"), ("gunung tertinggi di jawa barat apa", "cire?mai|cereme|ceremai"), ("siapa presiden indonesia tahun 2000", "wahid|gus dur"), ("siapa presiden indonesia tahun 2010", "yudhoyono|sby"),
                  ("siapa wakil presiden jokowi periode pertama", "jusuf kalla"), ("siapa yang menjahit bendera pusaka", "fatmawati"), ("dimana teks proklamasi dibacakan", "pegangsaan"), ("candi terbesar di indonesia apa", "borobudur"), ("nama selat antara jawa dan bali apa", "bali"),
                  ("provinsi termuda di indonesia apa", "papua barat daya"), ("kapan hari kebangkitan nasional", "20 mei"), ("kapan hari guru", "25 november"), ("ibu kota ntb apa", "mataram"), ("ibu kota kaltim apa", "samarinda"), ("ibu kota kalsel apa", "banjarbaru"),
                  ("ibu kota maluku utara apa", "sofifi"), ("novel bumi manusia karya siapa", "pramoedya"), ("lagu tanah airku ciptaan siapa", "sud|saridjah")],
    "dunia": [("presiden pertama amerika serikat siapa", "washington"), ("siapa pendiri tesla", "eberhard|tarpenning|musk"), ("ratu inggris terlama siapa", "elizabeth"), ("mata uang swiss apa", "fran[kc]"), ("benua terkecil apa", "australia"), ("ibu kota kanada apa", "ottawa"),
              ("ibu kota peru apa", "lima"), ("ibu kota kolombia apa", "bogot"), ("ibu kota nepal apa", "kathmandu|katmandu"), ("ibu kota irlandia apa", "dublin"), ("ibu kota kuba apa", "havana"), ("negara dengan bendera daun maple apa", "kanada|canada"),
              ("gunung tertinggi di afrika apa", "kilimanjaro"), ("sungai terpanjang di asia apa", "yangtze|yangtse|chang jiang"), ("negara penghasil kopi terbesar di dunia apa", "bra[sz]il"), ("siapa penemu benua amerika", "columbus|kolumbus"),
              ("perang dunia kedua berakhir tahun berapa", "1945"), ("tembok berlin runtuh tahun berapa", "1989"), ("siapa manusia pertama di bulan", "armstrong"), ("apa nama samudra terkecil", "arkti"), ("pesepakbola argentina paling terkenal siapa", "messi|maradona")],
    "ilmu": [("apa lambang kimia emas", "\\bau\\b"), ("apa lambang kimia besi", "\\bfe\\b"), ("apa simbol kimia natrium", "\\bna\\b"), ("berapa titik beku air", "\\b0\\b|nol"), ("gas apa yang dihembuskan saat bernapas", "karbon ?dioksida|co2|co₂"), ("organ terbesar tubuh manusia apa", "kulit"),
             ("vitamin apa dari sinar matahari", "vitamin d"), ("siapa penemu vaksin polio", "salk"), ("planet apa yang punya cincin", "saturnus"), ("bulan mengelilingi apa", "bumi"), ("berapa jarak bumi ke matahari", "150 juta|149"), ("hewan tertinggi di dunia apa", "jerapah"),
             ("berapa jumlah kromosom manusia", "46|23"), ("apa nama proses air menjadi uap", "penguapan|evaporasi"), ("berapa sudut segitiga sama sisi", "60"), ("satuan frekuensi apa", "hertz"), ("siapa bapak fisika modern", "einstein|galile"), ("siapa penemu atom", "dalton|demokritus|democritus|rutherford"),
             ("kenapa bulan punya fase", "matahari|posisi|cahaya"), ("kenapa kita bermimpi", "otak|tidur")],
    "arti": [("apa itu phishing", "penipuan|menipu|palsu|curi"), ("apa itu vpn", "jaringan|private|virtual"), ("apa itu reksadana", "investasi|dana|investor"), ("apa itu stoikisme", "filsafat|filosofi"), ("apa itu metafora", "bahasa|kiasan|perumpamaan|perbandingan"),
             ("apa arti carpe diem", "hari|saat ini|momen|manfaat"), ("apa bedanya ram dan rom", "sementara|permanen|baca"), ("apa bedanya gempa tektonik dan vulkanik", "lempeng|gunung"), ("apa itu open source", "kode|sumber"), ("apa itu ekosistem", "makhluk|lingkungan")],
    "bahasa": [("apa bahasa inggrisnya sepatu", "shoe"), ("apa bahasa inggrisnya lapar", "hungry"), ("apa bahasa jepangnya selamat pagi", "ohayo|おはよう"), ("apa bahasa korea terima kasih", "kamsa|gamsa|감사"), ("apa bahasa indonesianya butterfly", "kupu"),
               ("apa arti i miss you", "rindu|kangen"), ("apa bahasa jawa makan", "mangan|dhahar|nedha|maem"), ("apa bahasa sunda terima kasih", "hatur nuhun|nuhun")],
    "jebakan": [("siapa presiden indonesia ke-20", "belum|tidak|bukan"), ("ibu kota atlantis apa", "fiksi|mitos|tidak|legenda"), ("siapa penemu kucing", "tidak|hewan"), ("apa itu flumbertik", "tidak|belum"), ("siapa penulis novel pelangi di atas awan biru", "tidak|belum"), ("berapa kaki ular", "tidak|nol|\\b0\\b"), ("siapa juara dunia catur dari indonesia", "belum|tidak|bukan")],
}


# written last and run once, after everything above was tuned: the honest score (FRESH2 was tuned on after its first run).
FRESH3 = {
    "indonesia": [("siapa presiden kelima indonesia", "megawati"), ("wakil presiden sby periode kedua siapa", "boediono"), ("ibu kota sulawesi tengah apa", "palu"), ("ibu kota riau apa", "pekanbaru"), ("ibu kota banten apa", "serang"),
                  ("kapan hari batik nasional", "2 oktober"), ("kapan peristiwa rengasdengklok", "16 agustus|1945"), ("siapa pengarang siti nurbaya", "marah rusli"), ("tari kecak dari mana", "bali"), ("rumah honai dari mana", "papua"),
                  ("pulau terbesar kedua di indonesia apa", "sumatera|sumatra|papua"), ("siapa pendiri nahdlatul ulama", "hasyim"), ("siapa pendiri muhammadiyah", "dahlan"), ("siapa patih majapahit yang terkenal", "gajah mada"), ("apa nama kerajaan hindu tertua di indonesia", "kutai"),
                  ("apa kepanjangan bumn", "badan usaha milik negara"), ("apa kepanjangan dpr", "dewan perwakilan rakyat"), ("apa kepanjangan tni", "tentara nasional indonesia"), ("berapa jumlah anggota asean", "10|11|sepuluh|sebelas"), ("siapa pencipta lagu bagimu negeri", "kusbini")],
    "dunia": [("ibu kota vietnam apa", "hanoi"), ("ibu kota portugal apa", "lisbon"), ("ibu kota chile apa", "santiago"), ("ibu kota hungaria apa", "budapest"), ("mata uang korea selatan apa", "won"), ("mata uang brazil apa", "real"), ("negeri sakura itu negara apa", "jepang"),
              ("menara eiffel ada di mana", "paris|prancis"), ("patung liberty ada di negara mana", "amerika"), ("kapan perang dunia pertama mulai", "1914"), ("siapa presiden amerika yang dibunuh tahun 1963", "kennedy"), ("siapa pemimpin nazi jerman", "hitler"), ("berapa jumlah negara di asean", "10|11|sepuluh|sebelas"), ("olimpiade pertama modern di kota mana", "athena|athens")],
    "ilmu": [("apa lambang kimia perak", "\\bag\\b"), ("apa rumus kimia garam dapur", "nacl"), ("berapa suhu normal tubuh manusia", "36|37"), ("planet apa yang disebut bintang kejora", "venus"), ("apa nama galaksi kita", "bima sakti|milky"), ("siapa penemu mesin uap", "watt"), ("satuan tekanan apa", "pascal"),
             ("berapa jumlah gigi orang dewasa", "32"), ("berapa jumlah warna pelangi", "7|tujuh"), ("hewan apa yang disebut raja hutan", "singa"), ("kenapa es mengapung di air", "massa jenis|kerapatan|densitas"), ("kenapa kita menguap", "oksigen|otak|lelah|ngantuk"), ("berapa keliling lingkaran dengan jari jari 7", "44|43"), ("siapa penemu dna", "watson|crick|franklin|miescher")],
    "arti": [("apa itu inflasi dalam ekonomi", "harga"), ("apa itu firewall", "keamanan|jaringan|lalu lintas"), ("apa itu machine learning", "data|belajar"), ("apa itu cloud computing", "internet|server|jaringan"), ("apa arti deadline", "batas|tenggat"), ("apa arti insya allah", "jika|kehendak|izin"), ("apa bedanya virus dan malware", "malware|program"), ("apa bedanya cuaca dan iklim", "jangka|lama|panjang")],
    "bahasa": [("apa bahasa inggrisnya meja", "table|desk"), ("apa bahasa inggrisnya kursi", "chair"), ("apa bahasa inggrisnya sekolah", "school"), ("apa bahasa inggrisnya senang", "happy|glad"), ("apa arti see you later", "sampai jumpa|nanti"), ("apa bahasa jepangnya kucing", "neko|猫"), ("apa bahasa indonesianya apple", "apel"), ("apa bahasa indonesianya tomorrow", "besok")],
    "jebakan": [("siapa presiden indonesia yang ke-15", "belum|tidak|bukan"), ("apa ibu kota wakanda", "fiksi|tidak|film"), ("kapan indonesia menang piala dunia", "belum|tidak|pernah"), ("siapa penemu matahari", "tidak|bukan"), ("berapa jumlah kaki ikan", "tidak|nol|\\b0\\b"), ("apa itu grombolan zartik", "tidak|belum"), ("siapa penulis buku senja di kebun mars", "tidak|belum")],
}


if __name__ == "__main__":
    # the knowledge path as it really runs: answered here (right or wrong) or handed to the online AI
    import os
    import re
    import sys
    import time
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import brain
    import metrics
    metrics.count = lambda *a, **k: None      # (an exam isn't use: today's real counters stay as they are)
    which = sys.argv[1] if len(sys.argv) > 1 else "fresh"
    groups = {"lama": QUESTIONS, **FRESH} if which == "semua" else {"lama": QUESTIONS} if which == "lama" else FRESH if which == "fresh" else FRESH3 if which == "akhir" else FRESH2
    total = {"benar": 0, "salah": 0, "serah": 0}
    times = []
    routes: dict = {}
    handed = []
    for kind, questions in groups.items():
        counts = {"benar": 0, "salah": 0, "serah": 0}
        wrong = []
        for question, answer in questions:
            start = time.time()
            reply = brain._know_answer(question, offline=False)
            times.append(time.time() - start)
            how = brain._know_stats["how"] if reply else "ke Gemini"
            right = bool(reply and re.search(answer, reply.lower()))
            routes.setdefault(how, [0, 0])[0 if right else 1] += 1
            if not reply:
                counts["serah"] += 1
                handed.append(question)
            elif right:
                counts["benar"] += 1
            else:
                counts["salah"] += 1
                wrong.append((question, brain._know_stats["how"], reply[:70].replace("\n", " ")))
        print(f"{kind:10s} dijawab sendiri: benar {counts['benar']}, salah {counts['salah']} | ke Gemini {counts['serah']}", flush=True)
        for row in wrong:
            print("    SALAH", row, flush=True)
        for key in total:
            total[key] += counts[key]
    done = total["benar"] + total["salah"]
    print("per jalur:", {how: f"{r} benar, {w} salah" for how, (r, w) in routes.items() if how != "ke Gemini"})
    print("ke Gemini:", handed)
    print(f"TOTAL dari {done + total['serah']}: dijawab sendiri {done} ({100 * total['benar'] // max(1, done)}% benar, {total['salah']} salah), ke Gemini {total['serah']}; median {sorted(times)[len(times) // 2]:.2f} dtk, terlama {max(times):.2f} dtk")
    sys.stdout.flush()
    os._exit(0)
