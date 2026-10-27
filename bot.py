"""Регулярний новинний випуск: Telegram + DJ-стрічки -> Gemini -> озвучення + джингли -> GitHub Pages (RSS)."""
import asyncio
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
import numpy as np
from telethon import TelegramClient
from telethon.sessions import StringSession

import feed
import radio
import weather

try:  # httpx ставиться разом із google-genai; ловимо обриви з'єднання і таймаути
    import httpx
    TRANSPORT_ERRORS = (httpx.TransportError,)
except ImportError:
    TRANSPORT_ERRORS = ()

def _load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print("config.json не прочитано, беру типові значення:", repr(e)[:150])
        return {}


CFG = _load_config(os.environ.get("CONFIG_PATH") or "config.json")


def opt(key: str, env: str, default):
    """Значення з панелі (config.json) > змінна середовища > типове."""
    v = CFG.get(key)
    if v is None or v == "":
        v = os.environ.get(env)
    if v is None or v == "":
        return default
    if isinstance(default, bool):
        return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    try:
        return type(default)(v)
    except (TypeError, ValueError):
        return default


ENABLED = opt("enabled", "BOT_ENABLED", True)
CHANNELS = opt("channels", "TG_CHANNELS", "@kexxx")  # основний канал-джерело
RESERVE_CHANNELS = opt("reserve_channels", "RESERVE_CHANNELS", "@stranaua")  # резервний: читаємо, якщо в основному немає нового
TARGET_POSTS = opt("target_posts", "TARGET_POSTS", 8)  # скільки постів у випуску: основний канал — завжди весь, решту добираємо з резервного
GEMINI_MODEL = opt("gemini_model", "GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_FALLBACK_MODEL = opt("gemini_fallback_model", "GEMINI_FALLBACK_MODEL",
                           "gemini-3.5-flash-lite,gemini-3.6-flash")  # запасні моделі через кому
NEWS_WINDOW_MIN = opt("news_window_min", "NEWS_WINDOW_MIN", 360)  # новини за останні 6 годин
DEDUPE = opt("dedupe", "DEDUPE", True)  # не повторювати пости, що вже звучали в останніх випусках
MAX_POSTS = opt("max_posts", "MAX_POSTS", 40)
MIN_SENTENCES = opt("min_sentences", "MIN_SENTENCES", 1)
KEEP_EPISODES = opt("keep_episodes", "KEEP_EPISODES", 24)
# Озвучка ЛИШЕ Gemini TTS. Запасного «поганого» голосу немає: якщо Gemini не відповідає, бот довго й терпляче
# повторює запит (зростаючі паузи, обидві найкращі моделі), а якщо не вийшло — повторює ПОПЕРЕДНІЙ випуск.
TTS_MODELS = [m.strip() for m in opt("tts_models", "TTS_MODELS",
              "gemini-2.5-flash-preview-tts,gemini-2.5-pro-preview-tts").split(",") if m.strip()]
TTS_RETRY_MIN = opt("tts_retry_min", "TTS_RETRY_MIN", 40)      # скільки хвилин боротися за якісну озвучку
TTS_MAX_ATTEMPTS = opt("tts_max_attempts", "TTS_MAX_ATTEMPTS", 20)  # стеля спроб за один випуск (береже квоту)
TTS_AIR_MARGIN_MIN = opt("tts_air_margin_min", "TTS_AIR_MARGIN_MIN", 8)  # не боротися за озвучку пізніше, ніж за стільки хвилин до ефіру
TTS_DELAYS = [20, 30, 45, 60, 90, 120]  # зростаючі паузи між спробами, далі по 120 с
TTS_MAX_BYTES = opt("tts_max_bytes", "TTS_MAX_BYTES", 12000)  # стеля тексту одного запиту (стиль + текст); в API Gemini ліміту 4000 байт немає
ASSET_DIR = opt("asset_dir", "ASSET_DIR", "assets")
MP3_BITRATE = opt("mp3_bitrate", "MP3_BITRATE", "192k")  # стерео-mp3, кбіт/с
BED_ENABLED = opt("bed_enabled", "BED_ENABLED", True)
BED_UNDER_DB = opt("bed_under_db", "BED_UNDER_DB", -20.0)  # фон під голосом, дБ відносно голосу
BED_GAP_DB = opt("bed_gap_db", "BED_GAP_DB", -12.0)        # фон у паузах, дБ відносно голосу
JINGLES_ENABLED = opt("jingles_enabled", "JINGLES_ENABLED", True)
JINGLE_GAIN_DB = opt("jingle_gain_db", "JINGLE_GAIN_DB", 0.0)
STING_GAIN_DB = opt("sting_gain_db", "STING_GAIN_DB", 0.0)
# Звукові пакети за часом доби ефіру (назви — у soundpacks.py)
PACK_BY_PART = {
    "ранок": opt("pack_morning", "PACK_MORNING", "tech_house"),
    "день": opt("pack_day", "PACK_DAY", "neutral_house"),
    "вечір": opt("pack_evening", "PACK_EVENING", "deep_house"),
    "ніч": opt("pack_night", "PACK_NIGHT", "night_chill"),
}
PAUSE_BEFORE_MS = opt("pause_before_ms", "PAUSE_BEFORE_MS", 300)  # тиша після новини перед перебивкою
PAUSE_AFTER_MS = opt("pause_after_ms", "PAUSE_AFTER_MS", 150)     # тиша після перебивки перед наступною новиною
PAUSE_PLAIN_MS = opt("pause_plain_ms", "PAUSE_PLAIN_MS", 500)     # пауза там, де перебивки немає
WEATHER_ENABLED = opt("weather_enabled", "WEATHER_ENABLED", True)
WEATHER_WHERE = opt("weather_where", "WEATHER_WHERE", "у Києві")
WEATHER_LAT = opt("lat", "WEATHER_LAT", 50.45)
WEATHER_LON = opt("lon", "WEATHER_LON", 30.52)
SQUALL_MS = opt("squall_ms", "SQUALL_MS", float(weather.SQUALL_MS))  # пориви, від яких згадуємо шквал
DONATE_URL = opt("donate_url", "DONATE_URL", "https://keksfm.kiev.ua")
DONATE_ENABLED = opt("donate_enabled", "DONATE_ENABLED", True)  # в кінці випуску ведуча вариативно говорить про донат
DONATE_SPOKEN = opt("donate_spoken", "DONATE_SPOKEN", "кекс еф ем крапка кієв крапка ю а")  # як вимовляється keksfm.kiev.ua
TELEGRAM_URL = opt("telegram_url", "TELEGRAM_URL", "https://t.me/kexxxfm")  # лише в описі випуску, вголос не звучить
WEATHER_LEAD = opt("weather_lead", "WEATHER_LEAD",
                   "І про погоду в Києві.\nА тепер про погоду в Києві.\nІ нарешті про погоду в Києві.")
WEATHER_LEADS = [x.strip() for x in WEATHER_LEAD.splitlines() if x.strip()]  # явний вступ до погоди (по одному в рядку)
FEED_EXPLICIT = opt("rss_explicit", "RSS_EXPLICIT", False)  # позначка «explicit» у RSS: за замовчуванням вимкнена
EXTRA_INSTRUCTIONS = opt("extra_instructions", "EXTRA_INSTRUCTIONS", "")
VOICE_DB = -19.0
# Ведучі (дві жінки), чергуються: завжди береться «наступна» після тієї, що вела попередній випуск.
STATION_SPOKEN = opt("station_spoken", "STATION_SPOKEN", "Служба новин кекс рАдіо-кОмпані")  # велика голосна = наголос
_STRESS = "(a capitalized vowel in the middle of a word marks the stressed vowel)"
_SERIOUS = ("Our country is at war: for attacks, explosions, casualties and deaths speak gravely, with a "
            "heavy restrained tone. Crisp Ukrainian diction, pronounce every ending in full, correct word stress "
            + _STRESS + ", and leave a long, clear pause of about two seconds after each separate item:")
NAME_1 = opt("host1_name", "HOST1_NAME", "Ангеліна")
VOICE_1 = opt("host1_voice", "HOST1_VOICE", "Zephyr")  # тимчасово; остаточний вибір — за прослуховуванням voice_samples.py
SPEED_1 = opt("host1_speed", "HOST1_SPEED", 1.0)
PITCH_1 = opt("host1_pitch", "HOST1_PITCH", 2.0)     # півтони: плюс = вище (дзвінкіший, «дівочий» голос)
STYLE_1 = opt("host1_style", "HOST1_STYLE",
              "Read as Angelina, a 19-year-old young woman, radio news host: a feminine, clear, bright, HIGH-pitched "
              "voice in a high female register, but mature, confident and NOT childish, not a girl's voice; a SERIOUS, "
              "composed, firm newsroom delivery, no smiling and no softness. " + _SERIOUS)
NAME_2 = opt("host2_name", "HOST2_NAME", "Лєра")
VOICE_2 = opt("host2_voice", "HOST2_VOICE", "Kore")  # Firm — рівний голос; низький тембр дає зниження висоти нижче
SPEED_2 = opt("host2_speed", "HOST2_SPEED", 1.0)
PITCH_2 = opt("host2_pitch", "HOST2_PITCH", -2.0)    # півтони: мінус = нижче (глибший, «грудний» голос)
STYLE_2 = opt("host2_style", "HOST2_STYLE",
              "Read as Lyera, a young woman, radio news host: a LOW, deep, smooth female voice with a warm chest "
              "resonance, confident and NOT childish; a SERIOUS, composed, firm newsroom delivery, no smiling and "
              "no softness. " + _SERIOUS)
STATION = opt("station", "STATION", "Служба новин Кекс-радіокомпані")  # у тексті; вимовляється як STATION_SPOKEN

PROMPT = """Ти — редакторка і ведуча випуску новин молодіжного танцювального радіо Кекс ФМ.
Україна у стані війни, тому випуск СЕРЙОЗНИЙ. Говориш УКРАЇНСЬКОЮ мовою. Нижче пости з новинних каналів
(можуть бути російською або українською).

ЗАВДАННЯ 1 — новини. Для кожної окремої новини напиши ОДНЕ коротке речення українською,
без вигадування фактів. Дублікати об'єднуй, але НЕ пропускай жодної окремої події: речень має бути стільки, скільки різних подій у постах. Позначки [ОСН] і [РЕЗ] на початку постів — це основний і резервний канали (у відповіді позначок не пиши). ПРІОРИТЕТ — [ОСН]: кожну подію з них включи обов'язково й постав на початок випуску, а події з [РЕЗ] додай після них. Пропускай рекламу, заклики підписатися,
надіслати новину, донатити, службові підписи та пости без новинного змісту.

МОВА (обов'язково): якщо пост російською — переклади його зміст на українську.
Уся відповідь має бути ЛИШЕ українською: жодного російського слова і жодних літер ы, э, ъ, ё.
Власні назви пиши за українською нормою (Київ, а не Киев; Харків, а не Харьков).

ГРАМАТИКА І ЧИСЛА (обов'язково): літературна українська, граматично бездоганно. ВСІ числа пиши СЛОВАМИ
у правильному відмінку й роді, узгоджуючи з іменником (наприклад: «двоє загиблих», «п'ятнадцять поранених»,
«до двох тисяч осіб», «з двадцяти дронів збито дев'ятнадцять»). Жодних цифр у тексті.

ПОДАЧА: серйозна, зібрана, чітка; БЕЗ жартів, іронії, каламбурів і сленгу. Новини про обстріли, вибухи,
ракетні й дронові атаки, загиблих і поранених пиши прямо, точно й стримано, без пом'якшень і евфемізмів
(не «прилетіло», не «бавовна»): що сталося, де, скільки загиблих і поранених. Без емоційних оцінок.

ЗАВДАННЯ 2 — привітання (intro). Ведуча цього випуску: {host} (жінка), узгоджуй рід дієслів.
Напиши 2 короткі речення в такому ПОРЯДКУ. (1) СПОЧАТКУ скажи, що це «{station}» (ДОСЛІВНО, без відмінювання
і без змін у написанні) і що це найкоротші новини — щоразу інакше: «найкоротші новини», «найкоротший випуск
новин», «новини коротко» (слово «найкоротші» або «найкоротший» обов'язкове). (2) ПОТІМ назви себе у формі
на кшталт «З вами знову {host}», «Для вас працює {host}», «В ефірі {host}», «{host} знову з вами»,
«Новини для вас читає {host}» — щоразу інакше. Привітання за часом доби (зараз {part}) можна додати одним
словом на самому початку. НІКОЛИ не починай речення зі слова «Це» і не пиши «Це — {host}»; не вживай
«мене звати», «моє ім'я». Манера цього разу: {vibe}. НЕ називай конкретний час, години, хвилини, дату чи
день тижня. Без жартів, без штампів на кшталт «шановні слухачі».

ЗАВДАННЯ 3 — прощання (outro). 1–2 короткі речення в такому ПОРЯДКУ. (1) СПОЧАТКУ скажи, що найкоротші
новини «Кекс ФМ» завершено (щоразу інакше; слова «Кекс ФМ» обов'язкові). (2) ПОТІМ назви себе у формі
«З вами була {host}». (3) НАПРИКІНЦІ обов'язково побажай «{wish}» (саме цими словами). Без жартів і без
конкретного часу. Заклик до донату НЕ додавай: він звучить окремо перед прощанням.
{closing_task}{extra}
Відповідь — лише JSON-об'єкт з ключами {keys}.

Пости:
{posts}
"""


URL_RE = re.compile(r"https?://\S+|www\.\S+|t\.me/\S+", re.I)
SENT_SPLIT = re.compile(r"[.!?\u2026]+(?:\s|$)|\n+")
# Заклики підписатися тощо (навмисно вузький шаблон, щоб не зачепити "підписав закон")
PROMO_RE = re.compile(
    r"(під|под)пис(уйт\w*|уйся|уйсь|ись|атись|атися|аться|ывайт\w*|ывайся)"
    r"|subscribe"
    r"|наш(ого|ому)?\s+(канал|чат|бот|телеграм)\w*"
    r"|(надішл|надсил|присл|пришл|отправ)\w*\s+(нам\s+)?(новин|новост)\w*"
    r"|чита(й|йте)\s+нас",
    re.I,
)


def strip_promo(text: str) -> str:
    """Видаляє речення/рядки-заклики (\"підписуйтесь на наш канал\" тощо)."""
    out = []
    for line in text.splitlines():
        sents = re.split(r"(?<=[.!?\u2026])\s+", line)
        kept = [x for x in sents if not PROMO_RE.search(x)]
        if kept:
            out.append(" ".join(kept))
    return "\n".join(out).strip()


def clean_post(text: str) -> str:
    t = URL_RE.sub(" ", text)
    t = strip_promo(t)
    t = re.sub(r"[#@]\S+", " ", t)
    return re.sub(r"[ \t]+", " ", t).strip()


def is_substantial(clean: str) -> bool:
    """False, якщо після очищення нічого не лишилось або лишилось менше MIN_SENTENCES речень."""
    if not re.search(r"\w", clean):
        return False
    parts = [p for p in SENT_SPLIT.split(clean) if len(p.split()) >= 3]
    return len(parts) >= MIN_SENTENCES


def _split_channels(value: str) -> list[str]:
    return [c.strip() for c in value.split(",") if c.strip()]


async def _read_channels(client, channels: list[str]):
    cand, skipped = [], 0
    for channel in channels:
        async for msg in client.iter_messages(channel, limit=80):
            if not msg.text:  # пост без тексту (самі картинка/відео)
                continue
            if msg.text.lstrip().startswith("###"):  # пости, що починаються з «###», у випуск не беремо
                skipped += 1
                continue
            clean = clean_post(msg.text)
            if not is_substantial(clean):
                skipped += 1
                continue
            cand.append({"key": f"{channel}:{msg.id}", "date": msg.date, "text": clean[:900]})
    return cand, skipped


async def fetch_posts(covered: frozenset = frozenset()):
    """Повертає (тексти з позначками [ОСН]/[РЕЗ], ключі, режим, дата найновішого посту).
    ПРІОРИТЕТ — основний канал: беремо ВСІ його нові пости за вікно (навіть 1–2). Якщо їх менше за TARGET_POSTS,
    добираємо свіжі пости резервного. Режим: fresh — лише основний; mixed — основний + резервний;
    reserve — основний порожній, лише резервний; none — нового немає ніде (випуск пропускається)."""
    client = TelegramClient(
        StringSession(os.environ["TG_SESSION"]),
        int(os.environ["TG_API_ID"]),
        os.environ["TG_API_HASH"],
    )
    primary, reserve = _split_channels(CHANNELS), _split_channels(RESERVE_CHANNELS)
    if not primary:
        raise SystemExit("Не задано основний канал-джерело (панель: Источники, або секрет TG_CHANNELS)")
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=NEWS_WINDOW_MIN)

    def fresh(c):
        return c["date"] >= cutoff and not (DEDUPE and c["key"] in covered)

    async with client:
        pcand, skipped = await _read_channels(client, primary)
        chosen = sorted([dict(c, src="ОСН") for c in pcand if fresh(c)], key=lambda c: c["date"], reverse=True)
        n_primary = len(chosen)
        print(f"Основний канал {', '.join(primary)}: нових за {NEWS_WINDOW_MIN} хв — {n_primary}")
        extra = []
        if n_primary < TARGET_POSTS and reserve:
            rcand, rskipped = await _read_channels(client, reserve)
            skipped += rskipped
            pool = sorted([dict(c, src="РЕЗ") for c in rcand if fresh(c)], key=lambda c: c["date"], reverse=True)
            extra = pool[: TARGET_POSTS - n_primary]
            print(f"Резервний канал {', '.join(reserve)}: додаю {len(extra)} (нових {len(pool)}), щоб було до {TARGET_POSTS} постів")
    chosen = (chosen + extra)[:MAX_POSTS]
    mode = "none" if not chosen else ("fresh" if not extra else ("mixed" if n_primary else "reserve"))
    newest_dt = max((c["date"] for c in chosen), default=None)
    print(f"Новини: режим={mode}, відібрано {len(chosen)} (основних {n_primary}), відсіяно порожніх/коротких/###: {skipped}"
          + (f", найновіший пост: {newest_dt:%d.%m %H:%M} UTC" if newest_dt else ""))
    return [f"[{c['src']}] {c['text']}" for c in chosen], [c["key"] for c in chosen], mode, newest_dt


RU_LETTERS = re.compile(r"[ыэъёЫЭЪЁ]")  # літер немає в українській абетці
UA_LETTERS = re.compile(r"[іїєґІЇЄҐ]")  # у російській їх немає


def looks_russian(line: str) -> bool:
    """Підозра на російську: літери ы/э/ъ/ё, або довге речення без жодної і/ї/є/ґ."""
    return bool(RU_LETTERS.search(line)) or (len(line) > 40 and not UA_LETTERS.search(line))


# Слова, яких в українській немає. Для привітання, прощання і погоди (текст з нашою лексикою)
# евристика «довге речення без і/ї/є/ґ» дає хибні спрацьовування, тому там перевіряємо лише літери й ці слова.
RU_WORDS = re.compile(r"\b(что|это|как|был[аио]?|быть|или|будет|будут|сегодня|нет|его|есть|где|когда|после|"
                      r"только|если|также|более|очень|можно|может)\b", re.I)


def has_russian(line: str) -> bool:
    return bool(RU_LETTERS.search(line) or RU_WORDS.search(line))


STRICT_NOTE = ("\n\nУВАГА: у попередній відповіді були російські слова. Перевір кожне речення: "
               "лише українська мова, жодних літер ы, э, ъ, ё.")


RETRY_DELAYS = [15, 30, 60]  # секунди між повторами при 503/429/обриві з'єднання (потім — наступна модель)


HTTP_TIMEOUT_S = opt("http_timeout", "HTTP_TIMEOUT", 150)  # таймаут одного запиту до Gemini, секунди


def _client():
    try:
        opts = genai_types.HttpOptions(timeout=int(HTTP_TIMEOUT_S * 1000))
    except Exception:  # стара версія SDK без цього параметра
        return genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=opts)


def _parse_json(text: str):
    """JSON із відповіді моделі; терпимо до огорожі ```json і сміття навколо."""
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise


def _model_chain() -> list[str]:
    chain = [GEMINI_MODEL]
    for m in GEMINI_FALLBACK_MODEL.split(","):
        m = m.strip()
        if m and m not in chain:
            chain.append(m)
    return chain


def _generate(client, prompt: str):
    """Запит до Gemini. 503/429 (перевантаження, ліміт) — повтори з паузами; якщо модель так і не
    відповіла або недоступна (403/404) — наступна модель із ланцюжка."""
    errors = []
    for model in _model_chain():
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config={"response_mime_type": "application/json"},
                )
                print(f"Gemini: відповіла модель {model}")
                return resp
            except (genai_errors.ServerError, genai_errors.ClientError, *TRANSPORT_ERRORS) as e:
                code = getattr(e, "code", None) or type(e).__name__
                if isinstance(e, genai_errors.ClientError) and code != 429:
                    errors.append(f"{model}: {code}")
                    print(f"{model}: помилка {code}, ця модель недоступна")
                    break  # повтором не лікується -> наступна модель
                if attempt < len(RETRY_DELAYS):
                    print(f"{model}: помилка {code}, чекаю {RETRY_DELAYS[attempt]} с і пробую знову")
                    time.sleep(RETRY_DELAYS[attempt])
                else:
                    errors.append(f"{model}: {code} після {len(RETRY_DELAYS) + 1} спроб")
                    print(f"{model}: не відповідає після {len(RETRY_DELAYS) + 1} спроб")
    raise RuntimeError("Жодна модель Gemini не відповіла: " + "; ".join(errors))


VIBES = [
    "зібрано й серйозно",
    "спокійно й твердо",
    "стримано й діловито",
    "рівно й зосереджено",
    "чітко й по-діловому",
    "серйозно й упевнено",
]
INTRO_FALLBACKS = [
    "{station} — найкоротші новини. З вами знову {host}.",
    "{station}: найкоротший випуск новин. Для вас працює {host}.",
    "В ефірі {station}, найкоротші новини. {host} знову з вами.",
    "{station}, новини коротко: найкоротший випуск. Новини для вас читає {host}.",
]
OUTRO_FALLBACKS = [
    "Найкоротші новини Кекс ФМ завершено. З вами була {host}. {Wish}!",
    "Кекс ФМ: найкоротші новини позаду. З вами була {host}. {Wish}!",
    "Найкоротший випуск новин Кекс ФМ завершено. З вами була {host}. {Wish}!",
]


def host_for(now: datetime, last_id: str | None = None) -> dict:
    """Дві ведучі чергуються: беремо «наступну» після тієї, що вела попередній випуск
    (за записом у стрічці). Без запису — за парністю години ефіру."""
    hosts = {
        "1": {"name": NAME_1, "gemini": VOICE_1, "style": STYLE_1, "speed": SPEED_1, "pitch": PITCH_1},
        "2": {"name": NAME_2, "gemini": VOICE_2, "style": STYLE_2, "speed": SPEED_2, "pitch": PITCH_2},
    }
    if last_id == "1":
        hid = "2"
    elif last_id == "2":
        hid = "1"
    else:
        hid = "1" if (now.hour // 2) % 2 == 0 else "2"
    return {**hosts[hid], "id": hid, "gender": "жінка"}


def day_part(now: datetime) -> tuple[str, str]:
    """(частина доби, побажання на прощання). Після 22:00 — ніч, після 17:00 — вечір."""
    h = now.hour
    if h >= 22 or h < 5:
        return "ніч", "тихої ночі"
    if h >= 17:
        return "вечір", "гарного вечора"
    if h >= 12:
        return "день", "гарного дня"
    return "ранок", "гарного дня"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def make_context(now: datetime, host: dict, wsum: dict | None = None) -> dict:
    part, wish = day_part(now)
    ctx = {"host": host["name"], "gender": host["gender"], "station": STATION,
           "time": f"{now:%H:%M}", "part": part, "wish": wish, "vibe": random.choice(VIBES),
           "keys": '"intro" (рядок), "items" (масив рядків-новин), "outro" (рядок)',
           "closing_task": "", "extra": ""}
    if EXTRA_INSTRUCTIONS.strip():
        ctx["extra"] = ("\nДодаткові вказівки редактора (виконуй, якщо не суперечать правилам вище): "
                        + EXTRA_INSTRUCTIONS.strip() + "\n")
    return ctx


def _ask(client, posts: list[str], ctx: dict, strict: bool, note: str = "") -> dict:
    prompt = PROMPT.format(posts="\n---\n".join(posts), **ctx)
    if strict:
        prompt += STRICT_NOTE
    if note:
        prompt += note
    last = None
    for _ in range(2):
        resp = _generate(client, prompt)
        try:
            obj = _parse_json(resp.text)
            break
        except (json.JSONDecodeError, ValueError, AttributeError) as e:
            last = e
            print("Відповідь Gemini не схожа на JSON, повторюю запит")
    else:
        raise last
    if isinstance(obj, list):  # на випадок, якщо модель повернула лише масив новин
        obj = {"items": obj}
    items = [x.strip() for x in obj.get("items", []) if isinstance(x, str) and x.strip()]
    return {"intro": str(obj.get("intro") or "").strip(), "items": items,
            "outro": str(obj.get("outro") or "").strip()}


DIGITS_NOTE = ("\n\nУВАГА: у новинах були цифри. Запиши ВСІ числа словами, у правильному відмінку й роді, "
               "узгоджуючи з іменником (наприклад: «двоє загиблих», «п'ятнадцять поранених», «з двадцяти дронів»).")
NO_TIME_RE = re.compile(r"\d|\bгодин|\bхвилин", re.I)  # у привітанні/прощанні часу не називаємо
NO_NAME_RE = re.compile(r"мене\s+(звати|кличуть)|моє\s+ім['’]?я|my\s+name", re.I)  # «мене звати» — ніколи
NO_ADDR_RE = re.compile(r"keksfm|kiev\.ua|\.ua\b|https?:|www\.|крапк", re.I)  # адресу сайту в ефірі не називаємо


def _first_name(host: str) -> str:
    return host.split()[0].lower()


def _this_is(text: str, host: str) -> bool:
    """«Це — Ангеліна» / «Це Ангеліна» і будь-який вступ, що починається зі слова «Це», заборонені."""
    t = text.strip().lower()
    return t.startswith("це ") or t.startswith("це—") or bool(
        re.search(r"(?i)\bце\b\s*[—–\-]?\s*" + re.escape(_first_name(host)), text))


def valid_intro(text: str, ctx: dict) -> bool:
    """Спочатку «Служба новин… найкоротші новини», і лише потім ім'я ведучої."""
    low = text.lower()
    name, st = _first_name(ctx["host"]), STATION.lower()
    return (bool(text) and name in low and st in low and low.index(st) < low.index(name)
            and "найкоротш" in low
            and not has_russian(text) and not NO_TIME_RE.search(text) and not NO_NAME_RE.search(text)
            and not _this_is(text, ctx["host"]))


def valid_outro(text: str, ctx: dict) -> bool:
    """Порядок: «Кекс ФМ… найкоротші новини завершено» -> ім'я ведучої -> побажання."""
    low = text.lower()
    name, wish = _first_name(ctx["host"]), ctx["wish"]
    return (bool(text) and "кекс фм" in low and name in low and wish in low
            and low.index("кекс фм") < low.index(name) < low.index(wish)
            and not has_russian(text) and not NO_TIME_RE.search(text) and not NO_NAME_RE.search(text))


# Донат у кінці випуску: вариативно, з адресою сайту (вимовляється як DONATE_SPOKEN)
DONATE_VARIANTS = [
    "А якщо у вас є бажання підтримати розвиток нашої станції, донат можна зробити за посиланням на нашому сайті {addr}.",
    "Якщо ви хочете підтримати розвиток нашої станції, донат можна зробити за посиланням на нашому сайті {addr}.",
    "А підтримати розвиток нашої станції донатом можна за посиланням на нашому сайті {addr}.",
    "Якщо вам подобається наша робота й є бажання підтримати розвиток станції, донат можна зробити за посиланням на нашому сайті {addr}.",
    "Бажаєте допомогти розвитку нашої станції? Донат можна зробити за посиланням на нашому сайті {addr}.",
]


def pick_donate(last_variant) -> tuple[str, int]:
    """Випадкова формула, але не та сама, що в попередньому випуску; повертає (текст, номер)."""
    choices = [i for i in range(len(DONATE_VARIANTS)) if i != last_variant]
    v = random.choice(choices)
    return DONATE_VARIANTS[v].format(addr=DONATE_SPOKEN), v


def spoken(text: str) -> str:
    """Текст для озвучки: назву станції пишемо з наголосами (велика голосна = наголос)."""
    return re.sub(re.escape(STATION), STATION_SPOKEN, text, flags=re.I)


def summarize(posts: list[str], ctx: dict, wsum: dict | None = None):
    """Повертає (привітання, новини, прощання, текст погоди з явним вступом або None)."""
    client = _client()
    data = _ask(client, posts, ctx, strict=False)
    own = [data["intro"], data["outro"]]
    strict = any(looks_russian(x) for x in data["items"]) or any(has_russian(x) for x in own if x)
    note = DIGITS_NOTE if any(re.search(r"\d", x) for x in data["items"]) else ""
    if strict or note:
        print("Повторюю запит суворіше:" + (" російські слова;" if strict else "") + (" цифри в новинах;" if note else ""))
        data = _ask(client, posts, ctx, strict=strict, note=note)
    items = [x for x in data["items"] if not RU_LETTERS.search(x)]
    if any(re.search(r"\d", x) for x in items):
        print("Увага: у новинах лишилися цифри (модель не записала їх словами).")
    intro, outro = data["intro"], data["outro"]
    if not valid_intro(intro, ctx):
        print("Привітання не пройшло перевірку, беру запасне")
        intro = random.choice(INTRO_FALLBACKS).format(host=ctx["host"], station=STATION)
    if not valid_outro(outro, ctx):
        print("Прощання не пройшло перевірку, беру запасне")
        outro = random.choice(OUTRO_FALLBACKS).format(host=ctx["host"], Wish=_cap(ctx["wish"]))
    wtext = None
    if wsum:  # текст погоди складає код: усі числа словами в правильних відмінках, без швидкості вітру
        wtext = weather.compose_text(wsum)
        if WEATHER_LEADS:  # погоду оголошуємо явно: «І про погоду в Києві.»
            wtext = f"{random.choice(WEATHER_LEADS)} {wtext}"
    return intro, items, outro, wtext


def fit_bytes(texts: list[str], kinds: list[str], limit: int):
    """Привітання, оголошення, погоду і прощання лишаємо завжди; новини додаємо, поки є місце."""
    used = sum(len(t.encode()) + 2 for t, k in zip(texts, kinds) if k != "news")
    keep = []
    for t, k in zip(texts, kinds):
        if k != "news":
            keep.append(True)
            continue
        n = len(t.encode()) + 2
        ok = used + n <= limit
        used += n if ok else 0
        keep.append(ok)
    if not all(keep):
        print(f"Текст завеликий для одного запиту озвучення: озвучено {keep.count(True)} з {len(keep)} фраз")
    return [t for t, k in zip(texts, keep) if k], [x for x, k in zip(kinds, keep) if k]


class TTSUnavailable(Exception):
    """Gemini TTS так і не віддав якісну озвучку за відведений час."""


def _retry_after(e) -> float | None:
    m = re.search(r"retry\w*\D{0,14}(\d+(?:\.\d+)?)\s*s", str(e), re.I)
    return float(m.group(1)) if m else None


MAX_INNER_SILENCE_S = 6.0  # довша внутрішня тиша в озвученні — це збій Gemini, а не пауза


def _audio_problem(pcm: bytes, used: list[str]) -> str | None:
    """Чи не збійне озвучення? Повертає причину або None. Ловить: надто коротке (обрізане), надто довге
    для такого тексту (напр. 351 с на півтори хвилини тексту) і довгі «мертві» тиші всередині."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    dur = len(x) / 24000
    chars, n_items = sum(len(t) for t in used), len(used)
    if dur < 3:
        return f"аудіо підозріло коротке: {dur:.1f} с"
    if chars > 200 and dur < 0.55 * chars / 16.0:
        return f"аудіо обрізане: {dur:.0f} с на {chars} символів"
    max_dur = chars / 7.0 + 4.0 * n_items + 10  # дуже щедра межа: ~7 символів/с + 4 с на фразу
    if chars > 100 and dur > max_dur:
        return f"аудіо задовге: {dur:.0f} с на {chars} символів (очікується не більше {max_dur:.0f} с)"
    fr = 240  # кадри по 10 мс
    nf = len(x) // fr
    rms = np.sqrt((x[: nf * fr].reshape(nf, fr) ** 2).mean(axis=1))
    active = rms[rms > 10 ** (-50 / 20)]
    if len(active) == 0:
        return "суцільна тиша"
    ref = 20 * np.log10(np.sqrt(np.mean(active ** 2)) + 1e-12)
    loud = np.where(rms >= 10 ** ((ref - 28) / 20))[0]
    inner = rms[loud[0]: loud[-1] + 1] < 10 ** ((ref - 28) / 20)
    longest = run = 0
    for q in inner:
        run = run + 1 if q else 0
        longest = max(longest, run)
    if longest * 0.01 > MAX_INNER_SILENCE_S:
        return f"у озвученні тиша {longest * 0.01:.0f} с"
    return None


def gemini_tts_pcm(texts: list[str], kinds: list[str], voice: str, style: str, budget_s: float | None = None):
    """Озвучення всього випуску одним запитом. Запасного голосу НЕМАЄ: при збоях бот повторює запит із
    зростаючими паузами (20 → 120 с), чергуючи найкращі моделі, доки не мине бюджет часу або не буде
    TTS_MAX_ATTEMPTS спроб. 429 — чекаємо щонайменше хвилину; добова квота моделі вичерпана — модель відкладаємо;
    400 «завеликий текст» — скорочуємо запит; інші 4xx — модель недоступна; обрізане аудіо — повторюємо.
    Повертає (PCM 24 кГц 16 біт моно, озвучені фрази, їхні типи)."""
    cap = TTS_MAX_BYTES

    def prepare(limit):
        used, used_kinds = fit_bytes(texts, kinds, limit - len(style.encode()) - 4)
        return used, used_kinds, style + "\n\n" + "\n\n".join(used)

    used, used_kinds, script = prepare(cap)
    client = _client()
    cfg = genai_types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=genai_types.SpeechConfig(
            voice_config=genai_types.VoiceConfig(
                prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
    )
    deadline = time.monotonic() + (TTS_RETRY_MIN * 60 if budget_s is None else budget_s)
    dead: set[str] = set()
    attempt, shrinks, errors = 0, 0, []
    while attempt < TTS_MAX_ATTEMPTS and (attempt == 0 or time.monotonic() < deadline):  # перша спроба — завжди
        models = [m for m in TTS_MODELS if m not in dead]
        if not models:
            break
        model = models[attempt % len(models)]
        attempt += 1
        try:
            resp = client.models.generate_content(model=model, contents=script, config=cfg)
            pcm = resp.candidates[0].content.parts[0].inline_data.data
            if not pcm:
                raise ValueError("порожня відповідь без аудіо")
            if len(pcm) / (24000 * 2) < 3:
                raise ValueError(f"аудіо підозріло коротке: {len(pcm) / 48000:.1f} с")
            problem = _audio_problem(pcm, used)
            if problem:
                raise ValueError(problem)
            print(f"Gemini TTS: готово ({model}, спроба {attempt}, {len(pcm) / 48000:.0f} с на {sum(len(t) for t in used)} символів)")
            return pcm, used, used_kinds
        except (genai_errors.ServerError, genai_errors.ClientError, *TRANSPORT_ERRORS,
                ValueError, IndexError, AttributeError) as e:
            code = getattr(e, "code", None) or type(e).__name__
            errors.append(f"{model}:{code}")
            if (isinstance(e, genai_errors.ClientError) and code == 400 and shrinks < 3
                    and re.search(r"too long|exceed|token|length|size|limit|maximum|large", str(e), re.I)):
                shrinks += 1
                cap = int(min(cap, len(script.encode())) * 0.6)  # від фактичного розміру запиту, а не від стелі
                used, used_kinds, script = prepare(cap)
                print(f"Gemini TTS {model}: текст завеликий для запиту, скорочую до {cap} байт і повторюю")
                continue
            if isinstance(e, genai_errors.ClientError) and code != 429:
                dead.add(model)
                print(f"Gemini TTS {model}: помилка {code}, модель недоступна")
                continue
            wait = TTS_DELAYS[min(attempt - 1, len(TTS_DELAYS) - 1)]
            if code == 429:
                if re.search(r"PerDay|per day|daily", str(e), re.I):
                    dead.add(model)
                    print(f"Gemini TTS {model}: добова квота вичерпана, відкладаю модель")
                    continue
                wait = max(wait, (_retry_after(e) or 0) + 2, 60)
            wait *= random.uniform(0.9, 1.15)  # невеликий розкид, щоб не бити в ліміт рівними інтервалами
            left = deadline - time.monotonic()
            if left <= 0:
                break
            wait = min(wait, left)
            print(f"Gemini TTS {model}: помилка {code}, спроба {attempt}, чекаю {wait:.0f} с")
            time.sleep(wait)
    raise TTSUnavailable(f"спроб: {attempt}, помилки: {', '.join(errors[-6:]) or 'немає'}")


async def make_audio(texts: list[str], kinds: list[str], host: dict, out_path: str, pack: str | None = None,
                     budget_s: float | None = None):
    """texts/kinds: привітання, новини..., погода, телеграм, прощання. Повертає (секунди, озвучені фрази,
    їхні типи) або None, якщо якісної озвучки отримати не вдалося (тоді main повторить попередній випуск)."""
    try:
        pcm, used, used_kinds = await asyncio.to_thread(gemini_tts_pcm, texts, kinds, host["gemini"], host["style"], budget_s)
    except TTSUnavailable as e:
        print(f"::warning::Якісної озвучки Gemini отримати не вдалося ({e}). Запасного голосу немає.")
        return None
    voice = radio.normalize(radio.decode_pcm16(pcm, 24000, host["speed"], host["pitch"]), VOICE_DB)
    before = len(voice) / radio.SR
    voice = radio.compress_silences(voice)  # довгі «зависання» Gemini стискаємо до півтори секунди
    if before - len(voice) / radio.SR > 1:
        print(f"Скорочено довгі тиші в озвученні: {before:.0f} с -> {len(voice) / radio.SR:.0f} с")
    parts, gaps = radio.split_by_pauses(voice, len(used))
    method = "за паузами"
    if parts is None:  # паузи Gemini короткі чи нерівні — розкладаємо за довжиною тексту кожної фрази
        parts, gaps = radio.split_by_text(voice, used)
        method = "за довжиною тексту"
    if parts is None:
        print(f"Озвучення не вдалося розрізати на {len(used)} частин; перебивок між новинами не буде")
        parts = [voice]
    else:
        print(f"Озвучення розрізано на {len(parts)} частин ({method}), паузи між ними (мс): {gaps}")
    print(f"Озвучення: Gemini TTS (голос {host['gemini']}, ведуча {host['name']})")
    parts = [radio.trim_silence(p) for p in parts]

    try:
        assets = radio.load_assets(ASSET_DIR, pack=pack)
        print(f"Звуковий пакет: {pack or 'вбудований'}")
    except Exception as e:
        print("Не вдалося підготувати звуки, випуск буде без них:", repr(e)[:200])
        assets = {"open": [], "close": [], "sting": [], "bed": []}
    opener = random.choice(assets["open"]) if JINGLES_ENABLED and assets["open"] else None
    closer = random.choice(assets["close"]) if JINGLES_ENABLED and assets["close"] else None
    stings = assets["sting"] if JINGLES_ENABLED else []
    bed = assets["bed"][0] if BED_ENABLED and assets.get("bed") else None
    wsting = random.choice(assets["weather"]) if JINGLES_ENABLED and assets.get("weather") else None
    print(f"Звук: джингли={'так' if opener is not None else 'ні'}, перебивок={len(stings)}, "
          f"перебивка погоди={'так' if wsting is not None else 'ні'}, "
          f"фон={'так' if bed is not None else 'ні'} (під голосом {BED_UNDER_DB} дБ, у паузах {BED_GAP_DB} дБ)")
    track = radio.assemble(parts, used_kinds, opener, closer, stings, bed,
                           assets.get("bed_seamless", True), BED_UNDER_DB, BED_GAP_DB,
                           JINGLE_GAIN_DB, STING_GAIN_DB, gap_ms=PAUSE_PLAIN_MS, weather_sting=wsting,
                           pre_ms=PAUSE_BEFORE_MS, post_ms=PAUSE_AFTER_MS)
    radio.export_mp3(track, out_path, MP3_BITRATE)
    return len(track) / radio.SR, used, used_kinds


def air_time(now: datetime) -> datetime:
    """Випуск виходить в ефір на найближчу повну годину (збираємо його заздалегідь)."""
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


async def main() -> None:
    if not ENABLED:
        print("Випуски призупинено в панелі керування (enabled = false).")
        return
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    air = air_time(now)  # привітання, ведуча, пакет звуків і побажання — за часом ефіру, а не збирання
    site_dir = os.environ.get("SITE_DIR", "site")
    last = feed.last_meta(site_dir)
    host = host_for(air, last.get("host"))  # завжди «наступна» ведуча після попереднього випуску
    part, _ = day_part(air)
    pack = PACK_BY_PART.get(part)
    wsum = None
    if WEATHER_ENABLED:
        try:
            wsum = weather.summarize(weather.fetch(WEATHER_LAT, WEATHER_LON), squall_ms=SQUALL_MS)
        except Exception as e:
            print("Погода недоступна, випуск буде без неї:", repr(e)[:200])
    posts, keys, mode, newest = await fetch_posts(frozenset(feed.covered_keys(site_dir)))
    if not posts:
        print("Нових матеріалів немає ні в основному, ні в резервному каналі: випуск пропущено (повторів не робимо).")
        return
    ctx = make_context(air, host, wsum)
    intro, items, outro, wtext = summarize(posts, ctx, wsum)
    if not items:
        print("Gemini не повернув новин, пропускаю.")
        return
    texts, kinds = [intro], ["intro"]
    texts += items; kinds += ["news"] * len(items)
    if wtext:
        texts.append(wtext); kinds.append("weather")
    dn, dn_variant = pick_donate(last.get("donate")) if DONATE_ENABLED else (None, None)
    if dn:  # наприкінці випуску: вариативний заклик підтримати станцію (з адресою сайту), потім прощання
        texts.append(dn); kinds.append("donate")
    texts.append(outro); kinds.append("outro")
    print(f"Ведуча: {host['name']} (попередній випуск вела: №{last.get('host', '—')}); пакет звуків: {pack}")
    print(f"Привітання: {intro}")
    print(f"Погода: {wtext}")
    print(f"Донат (формула №{dn_variant}): {dn}")
    print(f"Прощання: {outro}")
    title = f"Новини {air:%d.%m %H:00}"
    cfg = {
        "base_url": os.environ["SITE_BASE_URL"],
        "title": opt("feed_title", "FEED_TITLE", "KEXXX NEWS"),
        "description": opt("feed_desc", "FEED_DESC", "Новини коротко і без хєрні"),
        "email": opt("feed_email", "FEED_EMAIL", "keksfm.kiev@gmail.com"),
        "explicit": FEED_EXPLICIT,
        "donate_url": DONATE_URL,
        "weather_credit": bool(wtext),
    }
    # бюджет повторів до Gemini: не довше TTS_RETRY_MIN і не пізніше ніж за TTS_AIR_MARGIN_MIN хвилин до ефіру
    to_air = (air - datetime.now(ZoneInfo("Europe/Kyiv"))).total_seconds()
    budget = max(0.0, min(TTS_RETRY_MIN * 60, to_air - TTS_AIR_MARGIN_MIN * 60))
    print(f"Бюджет часу на озвучку Gemini: {budget / 60:.0f} хв (до ефіру {to_air / 60:.0f} хв)")
    out = "episode.mp3"
    result = await make_audio([spoken(t) for t in texts], kinds, host, out, pack, budget)
    gh_out = os.environ.get("GITHUB_OUTPUT")

    def mark_published(fname: str):
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as f:
                f.write(f"published=true\naudio_file={fname}\n")

    if result is None:  # якісної озвучки немає — краще повторити попередній випуск, ніж пустити поганий голос
        src = feed.latest_episode(site_dir)
        if not src:
            print("::error::Озвучки немає, а попереднього випуску для повтору теж немає.")
            raise SystemExit(1)
        base = src.get("description", "").rstrip()  # позначка про повтор — лише в самому кінці опису
        tail = "Повтор попереднього випуску."
        if tail not in base:
            base += ("\n" if "Ведуча:" in base.split("\n\n")[-1] else "\n\n") + tail
        fname = feed.publish(site_dir, os.path.join(site_dir, src["file"]), f"{title} (повтор)", base,
                             int(src.get("duration", 0)), now, KEEP_EPISODES, cfg,
                             meta={**(src.get("meta") or {}), "reused": True, "mode": "reuse"}, name_suffix="_r")
        print(f"Опубліковано повтор попереднього випуску ({src['file']}).")
        mark_published(fname)
        return
    dur, used, used_kinds = result
    # В описі — лише те, що справді прозвучало (раніше опис будувався з усіх новин, навіть якщо озвучка їх не вмістила)
    voiced_news = [t for t, k in zip(used, used_kinds) if k == "news"]
    if len(voiced_news) < len(items):
        print(f"::warning::Озвучено {len(voiced_news)} новин із {len(items)}: решта не вмістилась у запит і в опис не потрапила.")
    # Опис: новини, посилання; службові рядки (ведуча, джерела) — в самому кінці, з нового абзацу. Без погоди.
    notes = [f"• {l}" for l in voiced_news]
    if TELEGRAM_URL:
        notes.append(f"Наш телеграм-канал новин {TELEGRAM_URL}")
    if DONATE_URL:
        notes.append(f"Підтримати розвиток станції: {DONATE_URL}")
    service = [f"Ведуча: {host['name']}"]
    if mode == "mixed":
        service.append("Джерела новин: основний та резервний канали.")
    elif mode == "reserve":
        service.append("Джерело новин: резервний канал.")
    description = "\n".join(notes) + "\n\n" + "\n".join(service)
    fname = feed.publish(site_dir, out, title, description, int(dur), now, KEEP_EPISODES, cfg,
                         meta={"host": host["id"], "posts": keys, "mode": mode, "pack": pack, "donate": dn_variant})
    print(f"Готово: {len(voiced_news)} новин ({mode}), {dur:.0f} с, файл {fname}")
    mark_published(fname)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print("ПОМИЛКА:", repr(e), file=sys.stderr)
        raise
