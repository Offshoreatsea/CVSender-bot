"""
Единая логика подбора вакансий по должностям — используется откликами по
email (email_apply.py). Адаптировано под архитектуру CVSender, где одна и
та же должность (например Master) выбирается НЕЗАВИСИМО в каждом из трёх
флотов (Merchant/Offshore/Tanker) — поэтому тег здесь всегда пара
"Должность|Флот" в виде одной строки ("Master|Offshore"), а не голый тег,
как в исходной (однофлотной) версии этого модуля.

Рассылка вакансий подписчикам в самом боте (main.py) НЕ использует этот
модуль — там уже есть своя, отдельно проверенная логика матчинга
(TAG_MATCH_ALIASES, get_subscribers_for_tag с fleet_tag). Этот модуль нужен
только для откликов по email, чтобы не трогать то, что уже работает.

Правила:
  1. Семья должностей: Master и MasterSDPO в офшоре — это одна и та же
     воронка кандидатов (SDPO — просто уточнение про DP-допуск на того же
     Master). Больше семей нет — Chief/Second/Third Officer в офшоре у
     CVSender уже не имеют отдельного DPO-тега вообще (см. main.py: эти
     варианты схлопываются в базовый тег ещё на этапе разбора текста
     вакансии Claude, до сохранения в базу).
  2. Синонимы в названии должности (Mate/OOW, EOOW и т.п.) тоже уже
     резолвятся Claude на этапе разбора (см. промпт в main.py) — сюда
     дублировать регулярки не нужно, vacancy_tags берёт готовый position_tag.
  3. Все остальные должности — точное совпадение тега В ТОМ ЖЕ ФЛОТЕ.
"""

RANK_FAMILIES = [
    {"Master|Offshore", "MasterSDPO|Offshore"},
]

EXTRA_LABELS: dict[str, str] = {}  # для совместимости интерфейса с email_apply.py


def tag_key(position_tag: str, fleet_tag: str) -> str:
    return f"{position_tag}|{fleet_tag}"


def family(tag: str) -> set[str]:
    """Вся семья должности (или сама должность, если семьи нет)."""
    for fam in RANK_FAMILIES:
        if tag in fam:
            return set(fam)
    return {tag}


def expand(tags) -> set[str]:
    """Должности человека -> все теги вакансий, которые ему подходят."""
    out: set[str] = set()
    for t in tags:
        if t:
            out |= family(t)
    return out


def vacancy_tags(fields: dict) -> set[str]:
    """Все теги, под которые подходит вакансия — её (должность, флот), с учётом семьи."""
    position_tag = fields.get("position_tag")
    fleet_tag = fields.get("fleet_tag")
    if not position_tag or position_tag == "Other" or not fleet_tag:
        return set()
    return expand({tag_key(position_tag, fleet_tag)})


def matches(person_tags, fields: dict) -> bool:
    """Подходит ли вакансия человеку с этими тегами (формат "Должность|Флот")."""
    return bool(expand(person_tags) & vacancy_tags(fields))
