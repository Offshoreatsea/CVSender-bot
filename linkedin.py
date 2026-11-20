"""Сбор вакансий из постов LinkedIn (через Apify) для CV Sender.

Только торговый флот, танкеры и газовозы (LNG/LPG). Офшор НЕ берём.

Модуль без привязки к Telegram: только поиск, оценка поста и запуск Apify.
Разбор на поля (должность/судно/флот), дедупликация, публикация и маршрутизация
по каналам делаются в main.py — тем же путём, что и у остальных источников.
Флот (Merchant / Offshore / Tanker) определяется там же по типу судна, здесь
только отбор «похоже ли это на живую морскую вакансию с корпоративной почтой».
"""
import asyncio
import os
import re

import httpx

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_ACTOR = os.getenv("APIFY_ACTOR", "harvestapi~linkedin-post-search")
APIFY_TASK_ID = os.getenv("APIFY_TASK_ID", "")
POSTS_PER_QUERY = int(os.getenv("LINKEDIN_POSTS_PER_QUERY", "10"))
POSTED_LIMIT = os.getenv("LINKEDIN_POSTED_LIMIT", "24h")
POLL_TIMEOUT_SEC = int(os.getenv("LINKEDIN_POLL_TIMEOUT_SEC", "1800"))

# ---------------------------------------------------------------- запросы
# Только торговый флот, танкеры и газовозы. Запросы собираются из блоков:
# должности (с синонимами), типы судов, формулировки крюингов, регионы/страны
# крюинга, условия контракта и хэштеги. Свои фразы можно добавить без правки кода:
# переменная LINKEDIN_EXTRA_QUERIES в Railway (через запятую или с новой строки).
POSITIONS_DECK = [
    "Master", "Captain", "Chief Officer", "Chief Mate", "Second Officer", "Second Mate",
    "Third Officer", "Third Mate", "Junior Officer", "OOW", "Deck Cadet", "Able Seaman",
    "Ordinary Seaman", "Bosun", "Deck Fitter", "Pumpman", "Safety Officer",
]
POSITIONS_ENGINE = [
    "Chief Engineer", "Second Engineer", "Third Engineer", "Fourth Engineer", "Junior Engineer",
    "Engine Cadet", "ETO", "Electro Technical Officer", "Electrician", "Motorman", "Oiler", "Wiper",
    "Fitter", "Welder", "Reefer Engineer", "Gas Engineer",
]
POSITIONS_CATERING = ["Cook", "Chief Cook", "Steward", "Messman"]

VESSELS_MERCHANT = [
    "bulk carrier", "bulker", "capesize", "panamax", "supramax", "ultramax", "handysize", "handymax",
    "container ship", "feeder vessel", "general cargo", "multipurpose vessel", "heavy lift", "ro-ro",
    "ro-pax", "car carrier", "PCTC", "reefer vessel", "timber carrier", "coaster", "cement carrier",
    "ore carrier", "cruise ship", "passenger ship",
]
VESSELS_TANKER = [
    "oil tanker", "crude oil tanker", "product tanker", "chemical tanker", "VLCC", "suezmax", "aframax",
    "LR2 tanker", "MR tanker", "LNG carrier", "LPG carrier", "VLGC", "gas carrier", "ethylene carrier",
    "ammonia carrier", "bitumen tanker", "shuttle tanker", "bunker tanker", "asphalt tanker",
]

CREWING_PHRASES = [
    "crewing agency vacancy", "seafarers wanted", "urgent crew required", "send CV crewing",
    "vacancy onboard vessel", "joining date vessel", "crew change vacancy", "manning agency hiring",
    "we are hiring seafarers", "join our crew", "ship management company vacancies", "crew required urgently",
    "seafarer recruitment", "officers required", "ratings required", "immediate joining vessel",
    "relief required vessel", "sign on vessel", "crewing manager seeking officers", "apply seafarer position",
    "email your CV seafarer", "marine crew opportunity", "new vacancies vessel crew",
]
CONTRACT_PHRASES = [
    "vessel vacancy rotation 4 months", "vessel vacancy rotation 3 months", "vessel vacancy salary USD",
    "seafarer vacancy contract 6 months", "joining Rotterdam vessel", "joining Singapore vessel",
    "joining Fujairah vessel", "joining Dubai vessel", "joining Istanbul vessel", "joining Houston vessel",
    "joining Manila vessel", "joining Mumbai vessel", "joining Odessa vessel", "joining Piraeus vessel",
    "joining Gdansk vessel", "joining Port Said vessel", "joining Antwerp vessel",
]
NATIONALITY_PHRASES = [
    "Ukrainian seafarers vacancy", "Filipino seafarers vacancy", "Indian seafarers vacancy",
    "Russian speaking seafarers vacancy", "Georgian seafarers vacancy", "Polish seafarers vacancy",
    "Romanian seafarers vacancy", "Bulgarian seafarers vacancy", "Croatian seafarers vacancy",
    "Turkish seafarers vacancy", "Burmese seafarers vacancy", "Indonesian seafarers vacancy",
    "Eastern European officers vacancy", "any nationality seafarers vacancy",
]
HASHTAGS = [
    "#seafarerjobs", "#maritimejobs", "#crewing", "#seafarers", "#shippingjobs", "#vesselvacancy",
    "#tankerjobs", "#lngcarrier", "#bulkcarrier", "#marinejobs", "#crewchange", "#seamanjobs",
]


def default_queries() -> list[str]:
    q = []
    for pos in POSITIONS_DECK + POSITIONS_ENGINE + POSITIONS_CATERING:
        q.append(f"{pos} vacancy vessel")
    # ключевые должности — ещё и в паре с флотом: чаще попадают именно в нужные посты
    for pos in ("Master", "Chief Officer", "Chief Engineer", "Second Engineer", "Second Officer",
                "Third Officer", "Third Engineer", "ETO", "Bosun", "AB", "Oiler", "Cook"):
        q.append(f"{pos} required tanker")
        q.append(f"{pos} required bulk carrier container ship")
    q += [f"{v} vacancy crew" for v in VESSELS_MERCHANT + VESSELS_TANKER]
    q += CREWING_PHRASES + CONTRACT_PHRASES + NATIONALITY_PHRASES + HASHTAGS
    extra = os.getenv("LINKEDIN_EXTRA_QUERIES", "")
    q += [x.strip() for x in re.split(r"[,\n]", extra) if x.strip()]
    seen, out = set(), []
    for x in q:  # без дублей, порядок сохраняем
        if x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


# ------------------------------------------------------------- оценка поста
HIRE_GROUPS = {
    "ищем": r"\b(we\s+are\s+looking\s+for|looking\s+for|we\s+are\s+hiring|hiring|required|wanted|vacanc(y|ies)|"
            r"needed|recruiting|opening|requires?|seeking\s+(a|an)\b)",
    "срочно": r"\b(urgent(ly)?|asap|immediate(ly)?)\b",
    "посадка": r"\b(joining|join\s+date|embark|sign[- ]?on|date\s+of\s+joining|doj|departure)\b",
    "ротация": r"\b(rotation|contract\s+(duration|length)|months?\s+on|\d+\s*(m|months?)\s*(contract|on)|"
               r"salary|wage|rate|usd|eur|\$|€|per\s+(day|month))\b",
    "cv": r"\b(send\s+(your\s+)?(cv|resume)|forward\s+(your\s+)?cv|apply\s+(to|at|via|by)|"
          r"interested\s+candidates|cv\s+to|resume\s+to)\b",
    "позиция": r"\b(position|rank|post)\s*[:\-]",
    "почта": r"[\w.+-]+@[\w-]+\.[\w.-]+",
    "crew": r"\b(join\s+our\s+crew|our\s+vessel|onboard|on\s+board|our\s+fleet|crewing)\b",
}

POSITION_RE = re.compile(
    r"\b(master|captain|chief\s+officer|chief\s+mate|c/?o|first\s+mate|second\s+(officer|mate)|2/?o|"
    r"third\s+(officer|mate)|3/?o|oow|dpo|sdpo|chief\s+engineer|c/?e|second\s+engineer|2/?e|"
    r"third\s+engineer|3/?e|fourth\s+engineer|4/?e|eto|electr(o)?[- ]?technical\s+officer|electrician|"
    r"bosun|able\s+seaman|a\.?b\.?|ordinary\s+seaman|o\.?s\.?|oiler|motorman|wiper|fitter|welder|"
    r"cook|messman|steward|roustabout|crane\s+operator|rigger|deck\s+cadet|engine\s+cadet|gas\s+engineer|"
    r"reefer\s+engineer|rov\s+(pilot|supervisor)|hlo)\b", re.IGNORECASE)

SEA_RE = re.compile(
    r"\b(vessel|ship|tanker|bulk|container|carrier|offshore|psv|ahts|osv|tug|rig|fpso|dp[12]?|"
    r"lng|lpg|vlcc|crew\s+boat|rotation|onboard|on\s+board|seafarer|seaman|marine)\b", re.IGNORECASE)

# «опыт работы 2/O» и «worked as» — это не набор на должность
EXPERIENCE_LINE_RE = re.compile(
    r"(experience|exp\.?|worked|working|served|previously|currently)\s+(as|in|on)\b|years?\s+(as|of\s+experience)",
    re.IGNORECASE)

SEEKER_RE = re.compile(
    r"\b(i\s+am\s+(a|an|currently)|i'?m\s+(a|an|looking|seeking)|open\s+to\s+work|looking\s+for\s+(a\s+)?(job|work|"
    r"new\s+opportunit|employment|ship|vessel\s+to\s+join)|seeking\s+(a\s+)?(job|employment|new\s+opportunit)|"
    r"available\s+for\s+(joining|employment)|my\s+cv\b|hire\s+me)", re.IGNORECASE)

NEWS_RE = re.compile(
    r"\b(congratulat|happy\s+(new\s+year|birthday|anniversary)|webinar|conference|exhibition|award|"
    r"market\s+report|freight\s+rates?|newsletter|press\s+release|launch(ed)?\s+of|christening|delivered\s+to)\b",
    re.IGNORECASE)

# береговые должности — не для CV Sender (он про моряков на судах)
SHORE_RE = re.compile(
    r"\b(shore[- ]?based|onshore|on[- ]shore|office[- ]based|head\s+office|superintendent|marine\s+(surveyor|"
    r"manager)|technical\s+(manager|superintendent)|crewing\s+(manager|officer|coordinator)|operations?\s+"
    r"(manager|executive|coordinator)|port\s+(captain|engineer)|fleet\s+manager|dpa\b|hse\s+(manager|advisor)|"
    r"ship\s*broker|chartering\s+(manager|executive)|sales|accountant|hr\s+(manager|officer)|internship|"
    r"lecturer|instructor|trainer|surveyor|inspector|pilot\s+station|harbou?r\s+pilot|vts)\b", re.IGNORECASE)

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "outlook.com", "hotmail.com",
    "hotmail.co.uk", "live.com", "msn.com", "icloud.com", "me.com", "mail.ru", "bk.ru", "inbox.ru", "list.ru",
    "yandex.ru", "yandex.com", "ya.ru", "rambler.ru", "ukr.net", "i.ua", "meta.ua", "email.ua", "gmx.com",
    "gmx.net", "aol.com", "proton.me", "protonmail.com", "zoho.com", "mail.com", "qq.com", "163.com",
    "rediffmail.com", "yahoo.in", "yahoo.fr", "libero.it", "wp.pl", "o2.pl", "abv.bg", "seznam.cz",
}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_BAD_EMAIL_PART = re.compile(r"(noreply|no-reply|donotreply|@2x|@3x|\.(png|jpe?g|gif|svg|webp)$|example\.)", re.I)
_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)


def corporate_emails(text: str) -> list[str]:
    """Только корпоративные почты (не бесплатные почтовики), без дублей и мусора."""
    seen, out = set(), []
    for m in EMAIL_RE.findall(text or ""):
        e = m.strip(".,;:()<>[]").lower()
        domain = e.rsplit("@", 1)[-1]
        if domain in FREE_MAIL or _BAD_EMAIL_PART.search(e):
            continue
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def free_mail_only(text: str) -> bool:
    found = EMAIL_RE.findall(text or "")
    return bool(found) and not corporate_emails(text)


def strip_post_links(text: str) -> str:
    """Ссылки из текста вакансии вырезаем целиком (почта остаётся)."""
    text = _URL_RE.sub("", text or "")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def hiring_positions(text: str) -> list[str]:
    """Должности, на которые именно НАБИРАЮТ: строки вида «опыт работы 2/O» не считаются."""
    lines = [ln for ln in (text or "").split("\n") if not EXPERIENCE_LINE_RE.search(ln)]
    found = {m.group(0).lower() for m in POSITION_RE.finditer("\n".join(lines))}
    return sorted(found)


def score_post(text: str) -> tuple[int, list[str], str | None]:
    """(баллы, причины, причина_отказа). Отказ — None, если пост проходит.

    Берём пост целиком: 8 групп признаков найма + должность + морской контекст.
    Шум (моряк ищет работу, новости, поздравления) и береговые вакансии отсеиваем.
    """
    text = text or ""
    if len(text) < 40:
        return 0, [], "слишком короткий"
    reasons = [name for name, rx in HIRE_GROUPS.items() if re.search(rx, text, re.IGNORECASE)]
    score = len(reasons)
    emails = corporate_emails(text)
    if not EMAIL_RE.search(text):
        return score, reasons, "нет e-mail"
    if not emails:
        return score, reasons, "только бесплатная почта (gmail, mail.ru и т.п.)"
    if SEEKER_RE.search(text) and score < 5:
        return score, reasons, "моряк ищет работу"
    if NEWS_RE.search(text) and score < 4:
        return score, reasons, "новость/поздравление"
    positions = hiring_positions(text)
    if not positions:
        return score, reasons, "нет должности, на которую набирают"
    if not SEA_RE.search(text):
        return score, reasons, "нет морского контекста"
    # береговая вакансия: береговые слова в строках с должностью и нет морских должностей без них
    shore_hits = SHORE_RE.findall(text)
    if shore_hits and not re.search(r"\b(vessel|onboard|on\s+board|rotation|tanker|offshore|bulk|container)\b", text, re.I):
        return score, reasons, "береговая вакансия"
    if score < 3:
        return score, reasons, f"мало признаков найма ({score})"
    return score + 1, reasons + ["должность"], None


def is_shore_title(position_text: str | None) -> bool:
    return bool(position_text and SHORE_RE.search(position_text))


# ------------------------------------------------------------- данные поста
def post_id(item: dict) -> str:
    return str(item.get("id") or item.get("entityId") or item.get("urn") or item.get("linkedinUrl")
               or item.get("url") or "")


def post_text(item: dict) -> str:
    for k in ("content", "text", "commentary", "postText"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict) and isinstance(v.get("text"), str):
            return v["text"]
    return ""


def post_url(item: dict) -> str:
    return str(item.get("linkedinUrl") or item.get("url") or item.get("postUrl") or "")


def post_author(item: dict) -> str:
    a = item.get("author")
    if isinstance(a, dict):
        return str(a.get("name") or a.get("publicIdentifier") or "")
    return str(a or "")


def post_images(item: dict) -> list[str]:
    """Картинки поста; аватарки и логотипы пропускаем."""
    urls = []
    for key in ("postImages", "images", "image", "media"):
        v = item.get(key)
        if isinstance(v, str):
            v = [v]
        for el in (v or []):
            u = el.get("url") if isinstance(el, dict) else el
            if isinstance(u, str) and u.startswith("http"):
                urls.append(u)
    skip = ("profile-displayphoto", "profile-framedphoto", "company-logo", "logo", "avatar", "profile-")
    return [u for u in urls if not any(s in u.lower() for s in skip)][:3]


def read_image_text(claude_client, urls: list[str]) -> str:
    """Текст вакансии с картинки (скриншот поста/объявления) — через Claude."""
    if not urls:
        return ""
    content = [{"type": "image", "source": {"type": "url", "url": u}} for u in urls]
    content.append({"type": "text", "text": (
        "Перепиши дословно весь текст вакансии с этих картинок (должность, судно, даты, зарплата, "
        "требования, e-mail). Если на картинке нет вакансии (логотип, фото, портрет) — ответь одним "
        "словом: NONE.")})
    try:
        resp = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=1500,
            messages=[{"role": "user", "content": content}])
        out = resp.content[0].text.strip()
        return "" if out.upper().startswith("NONE") else out
    except Exception as e:
        print(f"[linkedin] чтение картинки не удалось: {e}")
        return ""


# ------------------------------------------------------------------ Apify
def build_input(queries: list[str]) -> dict:
    return {
        "searchQueries": queries,
        "maxPosts": POSTS_PER_QUERY,
        "postedLimit": POSTED_LIMIT,
        "sortBy": "date",
    }


async def run_apify(queries: list[str] | None = None, progress=None) -> list[dict]:
    """Запускает актор/сохранённую задачу Apify и возвращает посты.

    Сначала запуск с ожиданием ответа (до ~5 мин), при долгом выполнении —
    асинхронный опрос до POLL_TIMEOUT_SEC. Если задан APIFY_TASK_ID — запускается
    ваша сохранённая задача со своими запросами (queries тогда не передаются).
    """
    if not APIFY_TOKEN:
        raise RuntimeError("не задан APIFY_TOKEN в Railway")
    base = "https://api.apify.com/v2"
    params = {"token": APIFY_TOKEN}
    async with httpx.AsyncClient(timeout=60) as c:
        if APIFY_TASK_ID:
            start_url, body = f"{base}/actor-tasks/{APIFY_TASK_ID}/runs", None
        else:
            start_url, body = f"{base}/acts/{APIFY_ACTOR}/runs", build_input(queries or default_queries())
        r = await c.post(start_url, params=params, json=body) if body is not None else await c.post(start_url, params=params)
        r.raise_for_status()
        data = r.json()["data"]
        run_id, dataset_id = data["id"], data["defaultDatasetId"]
        waited = 0
        while waited < POLL_TIMEOUT_SEC:
            await asyncio.sleep(15)
            waited += 15
            s = await c.get(f"{base}/actor-runs/{run_id}", params=params)
            s.raise_for_status()
            status = s.json()["data"]["status"]
            if progress and waited % 60 == 0:
                try:
                    await progress(f"Apify: {status}, жду {waited // 60} мин")
                except Exception:
                    pass
            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                raise RuntimeError(f"Apify завершился со статусом {status}")
        else:
            raise RuntimeError("Apify не уложился в отведённое время")
        items = await c.get(f"{base}/datasets/{dataset_id}/items", params={**params, "format": "json", "clean": "1"},
                            timeout=120)
        items.raise_for_status()
        out = items.json()
        return out if isinstance(out, list) else []
