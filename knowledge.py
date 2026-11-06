"""
What the small local model should not be trusted to remember. Asked without help it said "ibu kota Kalimantan Barat:
Samarinda" and "presiden ketiga: Soeharto", and says so with confidence. So the facts it keeps getting wrong are written
here, looked up by the words of the question, and handed to it with the question. A question they don't cover finds
nothing, so "ibu kota vanuatu" never gets an Indonesian province by accident.

General knowledge only: this file goes into the public repository.
"""
import re

_ORDINALS = ["pertama (ke-1)", "kedua (ke-2)", "ketiga (ke-3)", "keempat (ke-4)", "kelima (ke-5)", "keenam (ke-6)", "ketujuh (ke-7)",
             "kedelapan (ke-8)", "kesembilan (ke-9)", "kesepuluh (ke-10)", "kesebelas (ke-11)", "kedua belas (ke-12)", "ketiga belas (ke-13)", "keempat belas (ke-14)"]

_PRESIDENTS = ["Soekarno (Bung Karno), menjabat 1945-1967", "Soeharto (Pak Harto), menjabat 1967-1998", "B.J. Habibie, menjabat 1998-1999",
               "Abdurrahman Wahid (Gus Dur), menjabat 1999-2001", "Megawati Soekarnoputri, menjabat 2001-2004", "Susilo Bambang Yudhoyono (SBY), menjabat 2004-2014",
               "Joko Widodo (Jokowi), menjabat 2014-2024", "Prabowo Subianto, menjabat sejak 20 Oktober 2024"]
_VICE_PRESIDENTS = ["Mohammad Hatta (Bung Hatta)", "Sultan Hamengkubuwono IX", "Adam Malik", "Umar Wirahadikusumah", "Sudharmono", "Try Sutrisno", "B.J. Habibie",
                    "Megawati Soekarnoputri", "Hamzah Haz", "Jusuf Kalla (2004-2009)", "Boediono", "Jusuf Kalla (2014-2019)", "Ma'ruf Amin", "Gibran Rakabuming Raka, sejak 20 Oktober 2024"]

_PROVINCES = {
    "Aceh": "Banda Aceh", "Sumatera Utara": "Medan", "Sumatera Barat": "Padang", "Riau": "Pekanbaru", "Kepulauan Riau": "Tanjungpinang", "Jambi": "Kota Jambi",
    "Bengkulu": "Kota Bengkulu", "Sumatera Selatan": "Palembang", "Kepulauan Bangka Belitung": "Pangkalpinang", "Lampung": "Bandar Lampung", "Banten": "Serang",
    "Jawa Barat": "Bandung", "Jawa Tengah": "Semarang", "Daerah Istimewa Yogyakarta": "Kota Yogyakarta", "Jawa Timur": "Surabaya", "Bali": "Denpasar",
    "Nusa Tenggara Barat": "Mataram", "Nusa Tenggara Timur": "Kupang", "Kalimantan Barat": "Pontianak", "Kalimantan Tengah": "Palangka Raya",
    "Kalimantan Selatan": "Banjarbaru (sebelum 2022: Banjarmasin)", "Kalimantan Timur": "Samarinda", "Kalimantan Utara": "Tanjung Selor", "Sulawesi Utara": "Manado",
    "Gorontalo": "Kota Gorontalo", "Sulawesi Tengah": "Palu", "Sulawesi Barat": "Mamuju", "Sulawesi Selatan": "Makassar", "Sulawesi Tenggara": "Kendari", "Maluku": "Ambon",
    "Maluku Utara": "Sofifi", "Papua": "Jayapura", "Papua Barat": "Manokwari", "Papua Selatan": "Merauke", "Papua Tengah": "Nabire", "Papua Pegunungan": "Wamena", "Papua Barat Daya": "Sorong",
}
_CAPITALS = {
    "Jepang": "Tokyo", "Korea Selatan": "Seoul", "Korea Utara": "Pyongyang", "Tiongkok (China, Cina)": "Beijing", "India": "New Delhi", "Pakistan": "Islamabad", "Bangladesh": "Dhaka",
    "Malaysia": "Kuala Lumpur", "Thailand": "Bangkok", "Vietnam": "Hanoi", "Filipina": "Manila", "Myanmar": "Naypyidaw", "Kamboja": "Phnom Penh", "Laos": "Vientiane", "Brunei": "Bandar Seri Begawan",
    "Timor Leste": "Dili", "Arab Saudi": "Riyadh", "Iran": "Teheran", "Irak": "Baghdad", "Turki": "Ankara", "Mesir": "Kairo", "Australia": "Canberra", "Selandia Baru": "Wellington", "Rusia": "Moskow",
    "Inggris": "London", "Prancis": "Paris", "Jerman": "Berlin", "Italia": "Roma", "Spanyol": "Madrid", "Portugal": "Lisbon", "Belanda": "Amsterdam", "Belgia": "Brussel", "Swiss": "Bern", "Austria": "Wina",
    "Yunani": "Athena", "Swedia": "Stockholm", "Norwegia": "Oslo", "Finlandia": "Helsinki", "Denmark": "Kopenhagen", "Polandia": "Warsawa", "Ukraina": "Kyiv", "Amerika Serikat": "Washington, D.C.", "Kanada": "Ottawa",
    "Meksiko": "Mexico City", "Brasil": "Brasilia", "Argentina": "Buenos Aires", "Afrika Selatan": "Pretoria", "Nigeria": "Abuja", "Kenya": "Nairobi", "Maroko": "Rabat",
}
_CURRENCIES = {"Jepang": "yen", "Amerika Serikat": "dolar AS", "Inggris": "pound sterling", "negara-negara Eropa (Jerman, Prancis, Italia, Spanyol, Belanda)": "euro", "Malaysia": "ringgit",
               "Singapura": "dolar Singapura", "Thailand": "baht", "Korea Selatan": "won", "Tiongkok (China, Cina)": "yuan (renminbi)", "India": "rupee", "Arab Saudi": "riyal", "Rusia": "rubel", "Vietnam": "dong", "Filipina": "peso"}
_DAYS = {"Kartini": "21 April", "Buruh": "1 Mei", "Pendidikan Nasional (Hardiknas)": "2 Mei", "Kebangkitan Nasional (Harkitnas)": "20 Mei", "Lahir Pancasila": "1 Juni", "Anak Nasional": "23 Juli",
         "Pramuka": "14 Agustus", "Kemerdekaan Indonesia": "17 Agustus", "Kesaktian Pancasila": "1 Oktober", "Batik Nasional": "2 Oktober", "TNI": "5 Oktober", "Santri": "22 Oktober", "Ayah Nasional": "12 November", "Sumpah Pemuda": "28 Oktober",
         "Pahlawan": "10 November", "Guru Nasional": "25 November", "Ibu": "22 Desember", "Bumi": "22 April", "Lingkungan Hidup Sedunia": "5 Juni"}
_AUTHORS = {"Laskar Pelangi": "Andrea Hirata", "Sang Pemimpi": "Andrea Hirata", "Bumi Manusia": "Pramoedya Ananta Toer", "Ayat-Ayat Cinta": "Habiburrahman El Shirazy", "Perahu Kertas": "Dee Lestari", "Supernova": "Dee Lestari",
            "Siti Nurbaya": "Marah Rusli", "Tenggelamnya Kapal Van der Wijck": "Hamka", "Di Bawah Lindungan Ka'bah": "Hamka", "Negeri 5 Menara": "Ahmad Fuadi", "Dilan 1990": "Pidi Baiq", "Cantik Itu Luka": "Eka Kurniawan",
            "Harry Potter": "J.K. Rowling", "The Lord of the Rings": "J.R.R. Tolkien", "Sherlock Holmes": "Arthur Conan Doyle", "Romeo dan Juliet": "William Shakespeare"}
_INVENTORS = {"telepon": "Alexander Graham Bell", "lampu pijar": "Thomas Alva Edison", "pesawat terbang": "Wright bersaudara (Orville dan Wilbur Wright)", "penisilin": "Alexander Fleming", "radio": "Guglielmo Marconi",
              "World Wide Web (www, internet)": "Tim Berners-Lee", "mesin uap": "James Watt", "dinamit": "Alfred Nobel", "mobil bensin": "Karl Benz", "vaksin cacar": "Edward Jenner", "hukum gravitasi": "Isaac Newton",
              "teori relativitas": "Albert Einstein", "teori evolusi": "Charles Darwin", "mesin cetak": "Johannes Gutenberg", "televisi": "John Logie Baird dan Philo Farnsworth", "kereta api uap": "George Stephenson"}
_FOUNDERS = {"Microsoft": "Bill Gates dan Paul Allen", "Apple": "Steve Jobs, Steve Wozniak, dan Ronald Wayne", "Facebook (Meta)": "Mark Zuckerberg", "Google": "Larry Page dan Sergey Brin", "Amazon": "Jeff Bezos",
             "Muhammadiyah": "K.H. Ahmad Dahlan pada 1912", "Nahdlatul Ulama (NU)": "K.H. Hasyim Asy'ari pada 1926", "Budi Utomo": "dr. Soetomo dan kawan-kawan pada 20 Mei 1908", "Taman Siswa": "Ki Hadjar Dewantara pada 1922"}
_ORIGINS = {"Rendang": "Sumatera Barat (Minangkabau)", "Gudeg": "Yogyakarta", "Pempek": "Palembang, Sumatera Selatan", "Papeda": "Papua dan Maluku", "Rawon": "Jawa Timur", "Coto": "Makassar, Sulawesi Selatan", "Sate lilit": "Bali",
            "Tari Saman": "Aceh", "Tari Kecak": "Bali", "Tari Pendet": "Bali", "Reog": "Ponorogo, Jawa Timur", "Tari Jaipong": "Jawa Barat", "Tari Piring": "Sumatera Barat", "Angklung": "Jawa Barat", "Sasando": "Pulau Rote, Nusa Tenggara Timur",
            "Tifa": "Papua dan Maluku", "Rumah Gadang": "Sumatera Barat", "Rumah Joglo": "Jawa Tengah", "Rumah Honai": "Papua", "Rumah Tongkonan": "Toraja, Sulawesi Selatan", "Batik": "Jawa", "Wayang kulit": "Jawa", "Keris": "Jawa"}
_WHERE = {"Candi Borobudur (candi Buddha terbesar di dunia)": "Magelang, provinsi Jawa Tengah", "Candi Prambanan (candi Hindu)": "perbatasan Sleman (provinsi Yogyakarta) dan Klaten (provinsi Jawa Tengah)", "Danau Toba": "provinsi Sumatera Utara", "Pulau Komodo": "provinsi Nusa Tenggara Timur",
          "Raja Ampat": "provinsi Papua Barat Daya", "Tana Toraja": "provinsi Sulawesi Selatan", "Danau Kelimutu": "Flores, provinsi Nusa Tenggara Timur", "Pulau Lombok": "provinsi Nusa Tenggara Barat", "Pulau Madura": "provinsi Jawa Timur",
          "Pulau Batam": "provinsi Kepulauan Riau", "Pulau Nias": "provinsi Sumatera Utara", "Taman Laut Bunaken": "Manado, Sulawesi Utara", "Malioboro": "Kota Yogyakarta", "Gunung Tangkuban Perahu": "Jawa Barat", "Monas (Monumen Nasional)": "Jakarta Pusat",
          "Lawang Sewu": "Semarang, Jawa Tengah", "Jembatan Ampera": "Palembang, Sumatera Selatan", "Gunung Bromo": "Jawa Timur", "Gunung Merapi": "perbatasan Jawa Tengah dan Yogyakarta", "Gunung Rinjani": "Lombok, Nusa Tenggara Barat", "Gunung Agung": "Bali",
          "Gunung Krakatau": "Selat Sunda", "Gunung Tambora": "Sumbawa, Nusa Tenggara Barat", "Menara Eiffel": "Paris, Prancis", "Tembok Besar": "Tiongkok", "Taj Mahal": "Agra, India", "Piramida Giza": "Mesir", "Patung Liberty": "New York, Amerika Serikat", "Colosseum": "Roma, Italia", "Ka'bah": "Mekkah, Arab Saudi", "Angkor Wat": "Kamboja"}

_TERMS = [(1945, 1967, "Soekarno"), (1967, 1998, "Soeharto"), (1998, 1999, "B.J. Habibie"), (1999, 2001, "Abdurrahman Wahid (Gus Dur)"), (2001, 2004, "Megawati Soekarnoputri"),
          (2004, 2014, "Susilo Bambang Yudhoyono (SBY)"), (2014, 2024, "Joko Widodo (Jokowi)"), (2024, 2029, "Prabowo Subianto")]
_ABBREVIATIONS = {"DPR": "Dewan Perwakilan Rakyat", "MPR": "Majelis Permusyawaratan Rakyat", "DPD": "Dewan Perwakilan Daerah", "TNI": "Tentara Nasional Indonesia", "Polri": "Kepolisian Negara Republik Indonesia", "BUMN": "Badan Usaha Milik Negara",
                  "KPK": "Komisi Pemberantasan Korupsi", "BPJS": "Badan Penyelenggara Jaminan Sosial", "NKRI": "Negara Kesatuan Republik Indonesia", "UUD": "Undang-Undang Dasar", "KTP": "Kartu Tanda Penduduk", "SIM": "Surat Izin Mengemudi",
                  "ASEAN": "Association of Southeast Asian Nations", "PBB": "Perserikatan Bangsa-Bangsa", "WHO": "World Health Organization", "NASA": "National Aeronautics and Space Administration", "UNESCO": "United Nations Educational, Scientific and Cultural Organization",
                  "RAM": "Random Access Memory", "ROM": "Read Only Memory", "CPU": "Central Processing Unit", "GPU": "Graphics Processing Unit", "USB": "Universal Serial Bus", "PDF": "Portable Document Format", "HTTP": "Hypertext Transfer Protocol", "HTML": "Hypertext Markup Language", "VPN": "Virtual Private Network", "SSD": "Solid State Drive", "HDD": "Hard Disk Drive", "AI": "Artificial Intelligence", "GPS": "Global Positioning System", "LED": "Light Emitting Diode"}

FACTS = [
    *(f"Presiden {_ORDINALS[i]} Indonesia adalah {name}." for i, name in enumerate(_PRESIDENTS)),
    *(f"Presiden Indonesia pada tahun {year} adalah {' lalu '.join(name for start, end, name in _TERMS if start <= year <= end)}." for year in range(1945, 2030)),
    # (these two change with the 2029 election)
    "Presiden Indonesia adalah Prabowo Subianto, sejak 20 Oktober 2024.",
    "Wakil presiden Indonesia adalah Gibran Rakabuming Raka, sejak 20 Oktober 2024.",
    *(f"Kepanjangan {short} adalah {whole}." for short, whole in _ABBREVIATIONS.items()),
    *(f"Wakil presiden {_ORDINALS[i]} Indonesia adalah {name}." for i, name in enumerate(_VICE_PRESIDENTS)),
    *(f"Ibu kota {province} adalah {city}." for province, city in _PROVINCES.items()),
    *(f"Ibu kota {country} adalah {city}." for country, city in _CAPITALS.items()),
    *(f"Mata uang {country} adalah {name}." for country, name in _CURRENCIES.items()),
    *(f"Hari {day} diperingati setiap tanggal {date}." for day, date in _DAYS.items()),
    *(f"Penulis (pengarang) {book} adalah {name}." for book, name in _AUTHORS.items()),
    *(f"Penemu {thing} adalah {name}." for thing, name in _INVENTORS.items()),
    *(f"Pendiri {thing} adalah {name}." for thing, name in _FOUNDERS.items()),
    *(f"{thing} berasal dari {place}." for thing, place in _ORIGINS.items()),
    *(f"{thing} ada (terletak) di {place}." for thing, place in _WHERE.items()),

    # Indonesia
    "Ibu kota negara Indonesia adalah Jakarta; Nusantara (IKN) di Kalimantan Timur sedang dibangun sebagai ibu kota baru.",
    "Presiden wanita (perempuan) pertama Indonesia adalah Megawati Soekarnoputri.",
    "Indonesia merdeka: proklamasi kemerdekaan pada 17 Agustus 1945.",
    "Proklamator kemerdekaan Indonesia adalah Soekarno dan Mohammad Hatta.",
    "Teks proklamasi dibacakan Soekarno di Jalan Pegangsaan Timur 56, Jakarta, dan diketik oleh Sayuti Melik.",
    "Bendera pusaka Merah Putih dijahit oleh Fatmawati.",
    "Lagu kebangsaan Indonesia adalah Indonesia Raya, penciptanya Wage Rudolf Supratman (W.R. Supratman).",
    "Dasar negara Indonesia adalah Pancasila.",
    "Pancasila ada lima sila: Ketuhanan Yang Maha Esa; Kemanusiaan yang adil dan beradab; Persatuan Indonesia; Kerakyatan yang dipimpin oleh hikmat kebijaksanaan dalam permusyawaratan/perwakilan; Keadilan sosial bagi seluruh rakyat Indonesia.",
    "Konstitusi (undang-undang dasar) Indonesia adalah UUD 1945.",
    "Semboyan Indonesia adalah Bhinneka Tunggal Ika, artinya berbeda-beda tetapi tetap satu, dari kitab Sutasoma karya Mpu Tantular.",
    "Lambang negara Indonesia adalah Garuda Pancasila, dirancang oleh Sultan Hamid II.",
    "Bendera negara Indonesia adalah Merah Putih (Sang Saka Merah Putih).",
    "Mata uang Indonesia adalah rupiah.",
    "Bahasa resmi (bahasa nasional) Indonesia adalah bahasa Indonesia.",
    "Jumlah provinsi ada 38 di Indonesia.",
    "Indonesia punya 3 zona waktu: WIB (Waktu Indonesia Barat), WITA (Waktu Indonesia Tengah), WIT (Waktu Indonesia Timur).",
    "Jumlah pulau di Indonesia sekitar 17 ribu.",
    "Negara kepulauan terbesar di dunia adalah Indonesia.",
    "Pulau terbesar di dunia adalah Greenland.",
    "Jumlah penduduk Indonesia sekitar 280 juta jiwa, terbanyak keempat di dunia.",
    "Pulau besar di Indonesia: Sumatera, Jawa, Kalimantan, Sulawesi, dan Papua.",
    "Pulau terbesar di Indonesia adalah Kalimantan (bagian wilayah Indonesia); Pulau Papua seluruhnya lebih besar tetapi terbagi dengan negara Papua Nugini.",
    "Pulau dengan penduduk terbanyak (terpadat) di Indonesia adalah Jawa.",
    "Gunung tertinggi di Indonesia adalah Puncak Jaya (Carstensz Pyramid) di Papua, 4.884 meter.",
    "Gunung berapi tertinggi di Indonesia adalah Gunung Kerinci di Jambi, 3.805 meter.",
    "Gunung tertinggi di Pulau Jawa adalah Gunung Semeru di Jawa Timur, 3.676 meter.",
    "Sungai terpanjang di Indonesia adalah Sungai Kapuas di Kalimantan Barat, sekitar 1.143 km.",
    "Danau terbesar (terluas) di Indonesia adalah Danau Toba di Sumatera Utara.",
    "Selat Sunda memisahkan Pulau Jawa dan Pulau Sumatera.",
    "Selat Bali memisahkan Pulau Jawa dan Pulau Bali.",
    "Selat Lombok memisahkan Pulau Bali dan Pulau Lombok.",
    "Selat Malaka memisahkan Pulau Sumatera dan Semenanjung Malaysia.",
    "Selat Makassar memisahkan Pulau Kalimantan dan Pulau Sulawesi.",
    "Jembatan Suramadu menghubungkan Surabaya dan Pulau Madura.",
    "Komodo adalah kadal terbesar di dunia, hidup di Taman Nasional Komodo, Nusa Tenggara Timur.",
    "Jumlah negara anggota ASEAN ada 11, dengan Timor Leste yang bergabung pada Oktober 2025.",
    "Sumpah Pemuda diikrarkan pada 28 Oktober 1928.",
    "Budi Utomo berdiri pada 20 Mei 1908, menjadi tanda kebangkitan nasional.",
    "VOC (kongsi dagang Belanda) didirikan pada 1602.",
    "Kerajaan Majapahit mencapai puncak kejayaan di masa raja Hayam Wuruk dengan patih Gajah Mada, yang terkenal dengan Sumpah Palapa.",
    "Kerajaan Hindu tertua di Indonesia adalah Kutai di Kalimantan Timur.",
    "Kerajaan Sriwijaya adalah kerajaan maritim Buddha yang berpusat di Palembang.",
    "Kerajaan Islam pertama di Indonesia adalah Samudera Pasai di Aceh.",
    "Kerajaan Islam pertama di Pulau Jawa adalah Demak.",
    "Konferensi Asia Afrika (KAA) diadakan di Bandung pada 1955.",
    "Pertempuran Surabaya terjadi pada 10 November 1945, dengan tokoh Bung Tomo.",
    "Peristiwa Bandung Lautan Api terjadi pada 24 Maret 1946.",
    "Serangan Umum 1 Maret 1949 terjadi di Yogyakarta.",
    "Peristiwa Rengasdengklok: pada 16 Agustus 1945 golongan muda membawa Soekarno dan Hatta ke Rengasdengklok.",
    "Supersemar (Surat Perintah Sebelas Maret) dikeluarkan pada 11 Maret 1966.",
    "Reformasi terjadi pada 1998; Presiden Soeharto mundur pada 21 Mei 1998.",
    "Perang Diponegoro (Perang Jawa) berlangsung pada 1825-1830, dipimpin Pangeran Diponegoro.",
    "Perang Padri di Sumatera Barat dipimpin Tuanku Imam Bonjol.",
    "ASEAN didirikan pada 8 Agustus 1967 di Bangkok.",
    "Tsunami Aceh terjadi pada 26 Desember 2004.",
    "Gunung Krakatau meletus dahsyat pada 1883.",
    "Gunung Tambora meletus dahsyat pada 1815.",
    "Bapak Pendidikan Indonesia adalah Ki Hadjar Dewantara.",
    "Bapak Koperasi Indonesia adalah Mohammad Hatta.",
    "Bapak Pembangunan Indonesia adalah Soeharto.",
    "Bapak Teknologi (Bapak Dirgantara) Indonesia adalah B.J. Habibie.",
    "Bapak Pramuka Indonesia adalah Sultan Hamengkubuwono IX.",
    "Bapak TNI (Panglima Besar pertama) adalah Jenderal Soedirman.",
    "Pahlawan emansipasi wanita Indonesia adalah R.A. Kartini dari Jepara, dengan buku surat-suratnya Habis Gelap Terbitlah Terang.",
    "Cut Nyak Dien adalah pahlawan wanita dari Aceh.",
    "Sultan Hasanuddin dari Gowa, Sulawesi Selatan, dijuluki Ayam Jantan dari Timur.",
    "Kapitan Pattimura adalah pahlawan dari Maluku.",
    "Dewi Sartika adalah pahlawan pendidikan perempuan dari Jawa Barat.",
    "Chairil Anwar adalah penyair Angkatan 45, penulis puisi Aku.",
    "Emas Olimpiade pertama Indonesia diraih Susi Susanti dan Alan Budikusuma dari bulu tangkis di Barcelona 1992.",

    "Sila pertama Pancasila adalah Ketuhanan Yang Maha Esa.",
    "Sila kedua Pancasila adalah Kemanusiaan yang adil dan beradab.",
    "Sila ketiga Pancasila adalah Persatuan Indonesia.",
    "Sila keempat Pancasila adalah Kerakyatan yang dipimpin oleh hikmat kebijaksanaan dalam permusyawaratan/perwakilan.",
    "Sila kelima Pancasila adalah Keadilan sosial bagi seluruh rakyat Indonesia.",
    "Wakil presiden Soekarno adalah Mohammad Hatta.",
    "Wakil presiden Gus Dur (Abdurrahman Wahid) adalah Megawati Soekarnoputri.",
    "Wakil presiden Megawati adalah Hamzah Haz.",
    "Wakil presiden SBY (Susilo Bambang Yudhoyono) periode pertama adalah Jusuf Kalla, periode kedua Boediono.",
    "Wakil presiden Jokowi (Joko Widodo) periode pertama adalah Jusuf Kalla, periode kedua Ma'ruf Amin.",
    "Wakil presiden Prabowo adalah Gibran Rakabuming Raka.",
    "Kota Pahlawan adalah Surabaya.",
    "Kota Kembang adalah Bandung.",
    "Kota Hujan adalah Bogor.",
    "Kota Gudeg (Kota Pelajar) adalah Yogyakarta.",
    "Kota Apel adalah Malang.",
    "Kota Atlas adalah Semarang.",
    "Kota Daeng adalah Makassar.",
    "Serambi Mekkah adalah julukan Aceh.",
    "Pulau Dewata adalah julukan Bali.",
    "Bumi Cenderawasih adalah julukan Papua.",
    "Negeri Sakura (Negeri Matahari Terbit) adalah julukan Jepang.",
    "Negeri Gajah Putih adalah julukan Thailand.",
    "Negeri Tirai Bambu adalah julukan Tiongkok.",
    "Negeri Paman Sam adalah julukan Amerika Serikat.",
    "Negeri Kincir Angin adalah julukan Belanda.",
    "Negeri Ginseng adalah julukan Korea Selatan.",
    "Benua Hitam adalah julukan Afrika.",
    "Bandara di Bali adalah Bandara I Gusti Ngurah Rai.",
    "Bandara utama Jakarta adalah Bandara Soekarno-Hatta di Tangerang.",
    "Bandara di Surabaya adalah Bandara Juanda.",
    "Bandara di Medan adalah Bandara Kualanamu.",
    "Bandara di Makassar adalah Bandara Sultan Hasanuddin.",
    "Pelabuhan utama Jakarta adalah Tanjung Priok.",
    "Pencipta lagu Ibu Kita Kartini adalah W.R. Supratman.",
    "Pencipta lagu Hari Merdeka adalah H. Mutahar.",
    "Pencipta lagu Syukur adalah H. Mutahar.",
    "Pencipta lagu Tanah Airku adalah Ibu Sud (Saridjah Niung).",
    "Pencipta lagu Maju Tak Gentar adalah Cornel Simanjuntak.",
    "Pencipta lagu Satu Nusa Satu Bangsa adalah L. Manik.",
    "Pencipta lagu Indonesia Pusaka adalah Ismail Marzuki.",
    "Pencipta lagu Gugur Bunga adalah Ismail Marzuki.",
    "Pencipta lagu Bagimu Negeri adalah Kusbini.",
    "Suku terbesar di Indonesia adalah suku Jawa.",
    "Agama mayoritas di Indonesia adalah Islam.",
    "Provinsi termuda di Indonesia adalah Papua Barat Daya (2022).",
    "Provinsi dengan penduduk terbanyak di Indonesia adalah Jawa Barat.",
    "Gunung tertinggi di Sumatera adalah Gunung Kerinci.",
    "Gunung tertinggi di Jawa Barat adalah Gunung Ciremai.",
    "Gunung tertinggi di Sulawesi adalah Gunung Latimojong.",
    "Gunung tertinggi di Bali adalah Gunung Agung.",

    # the world
    "Manusia pertama di Bulan adalah Neil Armstrong, 1969.",
    "Presiden pertama Amerika Serikat adalah George Washington.",
    "Samudra terkecil di dunia adalah Samudra Arktik.",
    "Sungai terpanjang di Asia adalah Sungai Yangtze.",
    "Gunung tertinggi di Afrika adalah Gunung Kilimanjaro.",
    "Negara penghasil kopi terbesar di dunia adalah Brasil.",
    "Gedung tertinggi di dunia adalah Burj Khalifa di Dubai.",
    "Ratu Inggris terlama adalah Ratu Elizabeth II.",
    "Hewan tertinggi di dunia adalah jerapah.",
    "Organ terbesar tubuh manusia adalah kulit.",
    "Gunung tertinggi di dunia adalah Gunung Everest, 8.849 meter.",
    "Sungai terpanjang di dunia adalah Sungai Nil, sekitar 6.650 km.",
    "Samudra terluas (terbesar) di dunia adalah Samudra Pasifik.",
    "Benua terbesar (terluas) di dunia adalah Asia; benua terkecil adalah Australia.",
    "Jumlah benua di dunia ada 7: Asia, Afrika, Amerika Utara, Amerika Selatan, Antartika, Eropa, Australia.",
    "Negara terluas (terbesar) di dunia adalah Rusia.",
    "Negara terkecil di dunia adalah Vatikan.",
    "Negara dengan penduduk terbanyak di dunia adalah India, disusul Tiongkok.",
    "Gurun panas terluas di dunia adalah Sahara di Afrika.",
    "Danau terdalam di dunia adalah Danau Baikal di Rusia.",
    "Jumlah negara anggota PBB ada 193.",

    # science
    "Jumlah planet di tata surya ada 8: Merkurius, Venus, Bumi, Mars, Jupiter, Saturnus, Uranus, Neptunus.",
    "Planet terbesar di tata surya adalah Jupiter; planet terkecil adalah Merkurius.",
    "Planet terdekat dari matahari adalah Merkurius; planet terjauh adalah Neptunus.",
    "Planet terpanas di tata surya adalah Venus.",
    "Planet bercincin paling jelas adalah Saturnus.",
    "Planet merah adalah Mars.",
    "Bintang kejora (bintang timur, bintang fajar) sebenarnya adalah planet Venus.",
    "Galaksi tempat tata surya kita adalah Bima Sakti.",
    "Raja hutan adalah singa.",
    "Satelit alami Bumi adalah Bulan.",
    "Hewan terbesar di dunia adalah paus biru.",
    "Hewan darat terbesar di dunia adalah gajah afrika.",
    "Hewan tercepat di darat adalah cheetah.",
    "Burung terbesar di dunia adalah burung unta.",
    "Logam yang cair pada suhu kamar adalah raksa (merkuri).",
    "Unsur paling ringan adalah hidrogen.",
    "Rumus kimia air adalah H2O; garam dapur NaCl; karbon dioksida CO2.",
    "Gas terbanyak di udara adalah nitrogen, sekitar 78 persen; oksigen sekitar 21 persen.",
    "Kecepatan cahaya sekitar 300.000 km per detik.",
    "Kecepatan suara di udara sekitar 343 meter per detik.",
    "Air mendidih pada 100 derajat Celsius dan membeku pada 0 derajat Celsius.",
    "Jumlah tulang manusia dewasa ada 206.",
    "Organ yang memompa darah adalah jantung.",
    "Laba-laba punya 8 kaki; serangga punya 6 kaki.",
    "Pelukis Mona Lisa adalah Leonardo da Vinci; pelukis The Starry Night adalah Vincent van Gogh.",
    "Satuan gaya adalah newton; satuan energi joule; satuan daya watt; satuan tegangan volt; satuan arus listrik ampere.",
    "Dalam setahun ada 365 hari, 366 pada tahun kabisat.",
    "Satu jam ada 3.600 detik; satu hari ada 24 jam.",
]

_SAME = {"ibukota": "ibu_kota", "pengarang": "penulis", "karangan": "penulis", "karya": "penulis", "ciptaan": "penemu", "menjahit": "dijahit", "penjahit": "dijahit", "ditulis": "penulis", "menulis": "penulis", "pencipta": "penemu", "menciptakan": "penemu", "menemukan": "penemu", "ditemukan": "penemu",
         "penciptanya": "penemu", "diciptakan": "penemu", "mendirikan": "pendiri", "didirikan": "pendiri", "wapres": "wakil presiden", "singkatan": "kepanjangan", "kepanjangannya": "kepanjangan", "kepanjangan": "kepanjangan", "paling": "", "dari": "", "tanggal": "", "terkenal": "", "terbesar": "terluas", "cewek": "wanita", "perempuan": "wanita",
         "cina": "tiongkok", "china": "tiongkok", "sumatra": "sumatera", "amerika": "amerika serikat", "as": "amerika serikat", "usa": "amerika serikat", "korea": "korea selatan", "merdeka": "kemerdekaan",
         "terletak": "ada", "letak": "ada", "berada": "ada", "asal": "berasal", "jogja": "yogyakarta", "yogya": "yogyakarta", "jateng": "jawa tengah", "jabar": "jawa barat", "jatim": "jawa timur", "sumut": "sumatera utara", "sumbar": "sumatera barat",
         "sumsel": "sumatera selatan", "kaltim": "kalimantan timur", "kalbar": "kalimantan barat", "kalsel": "kalimantan selatan", "kalteng": "kalimantan tengah", "sulsel": "sulawesi selatan", "sulut": "sulawesi utara", "ntb": "nusa tenggara barat", "ntt": "nusa tenggara timur",
         "sukarno": "soekarno", "suharto": "soeharto", "sudirman": "soedirman", "hajar": "hadjar", "habibi": "habibie", "ke1": "pertama", "ke2": "kedua", "ke3": "ketiga", "ke4": "keempat", "ke5": "kelima", "ke6": "keenam", "ke7": "ketujuh", "ke8": "kedelapan"}
_STOP = {"apa", "apakah", "siapa", "siapakah", "berapa", "berapakah", "kapan", "kapankah", "dimana", "mana", "di", "ke", "yang", "itu", "ini", "adalah", "ialah", "nama", "sih", "dong", "kah", "ya", "dan", "atau", "pada", "dalam", "dengan", "tolong", "coba",
         "bisa", "beri", "tahu", "kasih", "sebutkan", "sebutin", "jawab", "setiap", "ada", "kamu", "tuan", "digidaw", "punya", "jumlah", "banyak", "terus", "kalau", "kalo", "lalu", "tentang", "kira", "sekitar", "bagian", "orang", "diperingati", "hari", "antara", "novel"}
ASKS_RE = re.compile(r"\b(?:apa|siapa|siapakah|berapa|kapan|mana|dimana|karya|ciptaan)\b", re.IGNORECASE)
# not what this sheet is for: the question is about the user, or wants an explanation (facts about who found gravity don't say what it is)
NOT_A_FACT_RE = re.compile(r"\b(?:saya|aku|ku|gue|gua)\b|^(?:apa (?:itu|arti|artinya|maksud)|arti|kenapa|mengapa|bagaimana|gimana|jelaskan|ceritakan)\b", re.IGNORECASE)
_SUBJECT_END = re.compile(r"\b(?:adalah|ada|berasal|diperingati|terjadi|diikrarkan|didirikan|berdiri|memisahkan|menghubungkan|dibacakan|dijahit|mencapai|punya)\b")


def _words(text: str) -> set[str]:
    out = re.sub(r"\bke[- ](\d)\b", r"ke\1", text.lower()).replace("ibu kota", "ibukota")
    found = []
    for word in re.findall(r"[a-z0-9']+", out):
        word = re.sub(r"(?:nya|kah)$", "", word) if len(word) > 5 else word
        word = _SAME.get(word, word)
        found += word.split()
    return {w for w in found if w not in _STOP}


def _subject(fact: str) -> set[str]:
    """What the fact is about: the words before its verb ("Wakil presiden ketiga ... adalah ...")."""
    start = fact.split(";")[0]
    return _words(start[:_SUBJECT_END.search(start.lower()).start()] if _SUBJECT_END.search(start.lower()) else start)


_FACT_WORDS = [_words(f) for f in FACTS]
_FACT_SUBJECTS = [_subject(f) for f in FACTS]


def wants_it_told(text: str) -> bool:
    """A story, a poem, a joke asked of an assistant that talks is said out loud. "Coba buat cerita" became a file, "berikan saya cerita" a status report. Named as a file or a
    document it stays a file."""
    said = text.strip()
    if FILED_WORK_RE.search(said):
        return False
    return bool(GIVES_SOMETHING_RE.search(said) or (MAKES_SOMETHING_RE.search(said) and TOLD_KIND_RE.search(said)))


def find(question: str, limit: int = 3) -> list[str]:
    """The facts holding every word of this question (most fitting first). One word they don't have and the question is
    about something else: "gunung tertinggi kedua" isn't answered by the highest mountain."""
    if NOT_A_FACT_RE.search(question.strip()):
        return []
    asked = _words(question)
    if not asked:
        return []
    fitting = sorted((len(subject - asked), len(words), fact) for fact, words, subject in zip(FACTS, _FACT_WORDS, _FACT_SUBJECTS) if asked <= words)
    # only the equally fitting: "presiden keempat" isn't helped by "wakil presiden keempat". Many of them: "siapa penemu" isn't a question yet
    best = [fact for extra, _, fact in fitting if extra == fitting[0][0]]
    return best if len(best) <= limit and (len(fitting) <= limit or fitting[0][0] == 0) else []


# Asked the same thing three times with some randomness, a model that knows says the same thing and one that guesses
# doesn't: the only sign of doubt this model gives (its own "yakin" field and its token probabilities told nothing).
# Measured on 116 questions: 83% right overall, 93% right when all three agree, 62% when they don't. Then asked whether the
# answer is right (5 of 9 "no" were false alarms, but it caught half of the wrong ones): 97% on questions neither the sheet nor a rule took.
MAKES_SOMETHING_RE = re.compile(r"^(?:tolong |coba )?(?:buatkan|buatin|bikinkan|bikinin|bikin|buat|tuliskan|tulisin|karang|karangkan)\b.*\b(?:puisi|pantun|cerita|lagu|lirik|kata|ucapan|caption|surat|contoh|ide|doa|esai|artikel)\b", re.IGNORECASE)
GIVES_SOMETHING_RE = re.compile(r"^(?:tolong |coba )?(?:berikan|beri|kasih|ceritain|ceritakan|bacakan)(?: (?:saya|aku|gue|dong|lagi|sebuah|satu|sebait))* "
                                r"(?:cerita|dongeng|pantun|puisi|lelucon|jokes?|candaan|teka-teki|tebak-tebakan|kata-kata|motivasi|quotes?)\b", re.IGNORECASE)
TOLD_KIND_RE = re.compile(r"\b(?:puisi|pantun|cerita|dongeng|lagu|lirik|kata-kata|kata(?! sandi)|ucapan|doa|lelucon|jokes?|candaan|teka-teki|tebak-tebakan|motivasi|quotes?|ide|caption)\b", re.IGNORECASE)
FILED_WORK_RE = re.compile(r"\b(?:file|berkas|dokumen|word|excel|powerpoint|ppt|pptx|pdf|docx|csv|txt|presentasi|simpan|slide|surat|esai|artikel|makalah|laporan)\b", re.IGNORECASE)
EXPLAINS_RE = re.compile(r"^(?:apa (?:itu|arti|artinya|maksud|bedanya|perbedaan)|arti\b|kenapa|mengapa|bagaimana|gimana|jelaskan|ceritakan)\b", re.IGNORECASE)
_NOISE = {"adalah", "dan", "yang", "di", "ke", "dari", "itu", "ini", "the", "of", "kota", "jawaban", "sungai", "gunung", "pulau", "danau", "negara", "provinsi", "jawab", "planet"}
# ...and two kinds where it agrees with itself and is wrong: the biggest of anything ("organ terbesar": jantung, 3 times out of 3)
# and regional languages or English into Indonesian ("bahasa jawa makan": makan; "tomorrow": hari depan). The sheet has the common superlatives; for the rest the online AI answers.
AGREES_WRONGLY_RE = re.compile(r"\bter(?:besar|kecil|tinggi|rendah|panjang|pendek|luas|sempit|dalam|cepat|lambat|banyak|sedikit|kaya|miskin|tua|muda|dekat|jauh|panas|dingin|berat|ringan|mahal|murah|kuat|lama|baik|populer|terkenal)\b|"
                               r"\bpaling\b|\bbahasa (?:jawa|sunda|batak|bali|minang|madura|bugis|betawi|aceh|banjar|indonesia(?:nya)?)\b|\bbahasa indonesianya\b", re.IGNORECASE)
GAVE_UP_RE = re.compile(r"\b(?:tidak (?:ada|tahu|diketahui|memiliki|punya|bisa|dapat)|maaf|belum (?:ada|diketahui)|unknown)\b", re.IGNORECASE)


def _answer_words(answer: str) -> set[str]:
    return {w for w in re.findall(r"[^\W_]+", answer.split("\n")[0].lower()) if w not in _NOISE}


def agreed(answers: list[str]) -> str:
    """What all these short answers say ("Bandar Lampung", "Kota Bandar Lampung"), or '' when they differ."""
    if not answers or any(GAVE_UP_RE.search(a) for a in answers):
        return ""
    first = _answer_words(answers[0])
    if not first or not all(first <= _answer_words(a) or _answer_words(a) <= first for a in answers[1:]):
        return ""
    return min(answers, key=len).split("\n")[0].strip()


def said_in(short: str, sentence: str) -> bool:
    """Does the sentence still give this short answer? (Told "0", the model wrote "Tidak ada jawaban yang benar".)"""
    return _answer_words(short) <= _answer_words(sentence.replace("\n", " "))
