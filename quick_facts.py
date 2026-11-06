"""
Prices and the time elsewhere, straight from public sources (no key): "harga emas hari ini" went through a web search and an AI, 13 s at the median and 40 s at
worst (log, Nov 2026), and could come back with an old number. Here it takes a moment, and when a source can't be reached it says so instead of guessing.
"""
import datetime
import time
import zoneinfo

import requests

import config

T = config.USER_TITLE
CACHE_SECONDS = 300
TIMEOUT = 4
OUNCE_GRAMS = 31.1035
COINS = {"bitcoin": ("bitcoin", "Bitcoin"), "btc": ("bitcoin", "Bitcoin"), "ethereum": ("ethereum", "Ethereum"), "eth": ("ethereum", "Ethereum"),
         "solana": ("solana", "Solana"), "bnb": ("binancecoin", "BNB"), "dogecoin": ("dogecoin", "Dogecoin"), "doge": ("dogecoin", "Dogecoin"),
         "xrp": ("ripple", "XRP"), "tether": ("tether", "Tether"), "usdt": ("tether", "Tether")}
CURRENCIES = {"dolar singapura": ("SGD", "dolar Singapura"), "sgd": ("SGD", "dolar Singapura"), "dolar australia": ("AUD", "dolar Australia"), "aud": ("AUD", "dolar Australia"),
              "dolar amerika": ("USD", "dolar"), "dolar": ("USD", "dolar"), "dollar": ("USD", "dolar"), "usd": ("USD", "dolar"), "euro": ("EUR", "euro"), "eur": ("EUR", "euro"),
              "yen": ("JPY", "yen"), "jpy": ("JPY", "yen"), "ringgit": ("MYR", "ringgit"), "myr": ("MYR", "ringgit"), "pound": ("GBP", "pound"), "gbp": ("GBP", "pound"),
              "won": ("KRW", "won"), "krw": ("KRW", "won"), "yuan": ("CNY", "yuan"), "cny": ("CNY", "yuan"), "riyal": ("SAR", "riyal"), "sar": ("SAR", "riyal"),
              "baht": ("THB", "baht"), "thb": ("THB", "baht")}
PLACES = {"tokyo": ("Asia/Tokyo", "Tokyo"), "jepang": ("Asia/Tokyo", "Jepang"), "seoul": ("Asia/Seoul", "Seoul"), "korea": ("Asia/Seoul", "Korea"), "beijing": ("Asia/Shanghai", "Beijing"),
          "china": ("Asia/Shanghai", "China"), "cina": ("Asia/Shanghai", "Cina"), "singapura": ("Asia/Singapore", "Singapura"), "singapore": ("Asia/Singapore", "Singapura"),
          "malaysia": ("Asia/Kuala_Lumpur", "Malaysia"), "kuala lumpur": ("Asia/Kuala_Lumpur", "Kuala Lumpur"), "bangkok": ("Asia/Bangkok", "Bangkok"), "thailand": ("Asia/Bangkok", "Thailand"),
          "india": ("Asia/Kolkata", "India"), "dubai": ("Asia/Dubai", "Dubai"), "mekkah": ("Asia/Riyadh", "Mekkah"), "makkah": ("Asia/Riyadh", "Mekkah"), "madinah": ("Asia/Riyadh", "Madinah"),
          "arab saudi": ("Asia/Riyadh", "Arab Saudi"), "london": ("Europe/London", "London"), "inggris": ("Europe/London", "Inggris"), "paris": ("Europe/Paris", "Paris"),
          "prancis": ("Europe/Paris", "Prancis"), "berlin": ("Europe/Berlin", "Berlin"), "jerman": ("Europe/Berlin", "Jerman"), "belanda": ("Europe/Amsterdam", "Belanda"),
          "amsterdam": ("Europe/Amsterdam", "Amsterdam"), "moskow": ("Europe/Moscow", "Moskow"), "rusia": ("Europe/Moscow", "Rusia"), "new york": ("America/New_York", "New York"),
          "amerika": ("America/New_York", "New York"), "los angeles": ("America/Los_Angeles", "Los Angeles"), "sydney": ("Australia/Sydney", "Sydney"), "australia": ("Australia/Sydney", "Sydney"),
          "jakarta": ("Asia/Jakarta", "Jakarta"), "wib": ("Asia/Jakarta", "WIB"), "bali": ("Asia/Makassar", "Bali"), "makassar": ("Asia/Makassar", "Makassar"), "wita": ("Asia/Makassar", "WITA"),
          "jayapura": ("Asia/Jayapura", "Jayapura"), "papua": ("Asia/Jayapura", "Papua"), "wit": ("Asia/Jayapura", "WIT")}
HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

_cache: dict[str, tuple[float, dict]] = {}


def _get(url: str) -> dict:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    answer = requests.get(url, timeout=TIMEOUT)
    answer.raise_for_status()
    data = answer.json()
    _cache[url] = (time.time(), data)
    return data


def _rupiah(value: float) -> str:
    """Words for big amounts, as a person says them: "1,37 miliar rupiah", "2,35 juta rupiah", "18.080 rupiah"."""
    for size, word in ((1e12, "triliun"), (1e9, "miliar"), (1e6, "juta")):
        if value >= size:
            return f"{value / size:.2f}".replace(".", ",").rstrip("0").rstrip(",") + f" {word} rupiah"
    return f"{value:,.0f}".replace(",", ".") + " rupiah"


def coin_price(coin: str = "bitcoin") -> str:
    """The price of a crypto coin now ('harga bitcoin', 'ethereum berapa'): bitcoin, ethereum, solana, bnb, dogecoin, xrp, tether."""
    key, name = COINS.get(coin.lower().strip(), (coin.lower().strip(), coin.strip().capitalize()))
    try:
        price = _get(f"https://api.coingecko.com/api/v3/simple/price?ids={key}&vs_currencies=idr,usd")[key]
        dollars = f"{price['usd']:,.0f}".replace(",", ".")
        return f"{name} sekarang sekitar {_rupiah(price['idr'])}, atau {dollars} dolar, {T}."
    except (requests.RequestException, KeyError, ValueError):
        return f"Harga {name} tidak bisa diambil sekarang, {T}. Coba sebentar lagi."


def _rates() -> dict:
    data = _get("https://open.er-api.com/v6/latest/USD")
    if data.get("result") != "success":
        raise ValueError("no rates")
    return data["rates"]


def exchange_rate(currency: str = "dolar", amount: float = 1) -> str:
    """What a foreign currency is worth in rupiah ('kurs dolar', '100 dolar berapa rupiah'). currency: dolar, euro, yen, ringgit, pound, won, yuan, riyal, baht, dolar singapura, dolar australia."""
    code, name = CURRENCIES.get(currency.lower().strip(), (currency.upper().strip(), currency.strip()))
    try:
        rates = _rates()
        value = rates["IDR"] / rates[code] * float(amount)
    except (requests.RequestException, KeyError, ValueError, ZeroDivisionError):
        return f"Kurs {name} tidak bisa diambil sekarang, {T}. Coba sebentar lagi."
    said = f"{amount:g}".replace(".", ",")
    return f"{said} {name} sekarang sekitar {_rupiah(value)}, {T}."


def gold_price() -> str:
    """The gold price per gram now ('harga emas hari ini'): the world price in rupiah; Antam's own price is a little higher."""
    try:
        ounce = float(_get("https://api.gold-api.com/price/XAU")["price"])
        value = ounce / OUNCE_GRAMS * _rates()["IDR"]
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return f"Harga emas tidak bisa diambil sekarang, {T}. Coba sebentar lagi."
    return f"Harga emas dunia sekarang sekitar {_rupiah(value)} per gram, {T}. Emas Antam biasanya sedikit lebih mahal."


def knows_place(place: str) -> bool:
    return place.lower().strip() in PLACES


def world_time(place: str) -> str:
    """The time in another city or country ('jam berapa di Tokyo')."""
    zone, name = PLACES.get(place.lower().strip(), ("", place.strip()))
    if not zone:
        return f"Zona waktu {name} belum saya kenal, {T}."
    now = datetime.datetime.now(zoneinfo.ZoneInfo(zone))
    return f"Di {name} sekarang pukul {now:%H.%M}, hari {HARI[now.weekday()]}, {T}."


AI_TOOLS = [coin_price, exchange_rate, gold_price, world_time]
