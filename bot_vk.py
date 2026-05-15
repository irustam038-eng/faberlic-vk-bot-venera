import asyncio
import csv
import ctypes
import gc
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
from vkbottle import API, PhotoMessageUploader, Keyboard, KeyboardButtonColor, Text, OpenLink
from vkbottle.bot import Bot, Message

import bot_db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot_vk.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("vkbot")

VK_TOKEN = os.getenv("VK_TOKEN")
if not VK_TOKEN:
    raise RuntimeError("VK_TOKEN не задан в .env")

VK_GROUP_ID = os.getenv("VK_GROUP_ID", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

BASE_DIR = Path(__file__).parent
LINKS_FILE = BASE_DIR / "links.json"
ADMINS_FILE = BASE_DIR / "admins.json"
TEXTS_FILE = BASE_DIR / "texts.json"
MEDIA_DIR = BASE_DIR / "media"

DEFAULT_LINKS = {
    "reg": "https://faberlic.com/register?sponsornumber=739945401&lang=ru&r=1000034210371",
    "catalog": "https://faberlic.com/ru/ru/catalogs/1087?sponsornumber=739945401",
    "venera_vk": "https://vk.com/id443815960",
}

DEFAULT_TEXTS = {
    "gift_promo": (
        "Акция для новых покупателей\n"
        "с 4 по 24 мая 2026 года\n\n"
        "Набор в ПОДАРОК за 1 руб.!\n\n"
        "ШАГ 1: Зарегистрируйся на faberlic.com — получи скидку 20%\n"
        "ШАГ 2: Сделай заказ от 1500 руб. (цены каталога)\n"
        "ШАГ 3: Получи в подарок набор:\n"
        "- Beauty Collagen (арт. 15955)\n"
        "- Крем для век Elasty Eye Filler (арт. 1383)\n"
        "- Ночной крем Skin-Plumping Cream (арт. 1382) ИЛИ\n"
        "  Дневной крем-флюид Firming Fluid Cream (арт. 1381)\n\n"
        "Цена набора в каталоге: 1997 руб.\n"
        "Ты платишь: всего 1 руб.\n\n"
        "Регистрация бесплатная, занимает 2 минуты"
    ),
    "welcome_text": (
        "Здесь я делюсь лайфхаками Faberlic, которые экономят время и деньги\n\n"
        "Ты здесь, потому что хочешь знать секреты чистоты?\n"
        "Или ищешь легендарную кислородную косметику?\n"
        "Может хочешь легко похудеть?\n\n"
        "Выбирай, что тебе прислать прямо сейчас"
    ),
}


# ─── Кэши (сбрасываются при сохранении, не читаем файлы на каждый запрос) ────

_links_cache: dict | None = None
_texts_cache: dict | None = None
_admin_ids_cache: list[int] | None = None
_uploader: PhotoMessageUploader | None = None


def load_links() -> dict:
    global _links_cache
    if _links_cache is not None:
        return _links_cache
    if not LINKS_FILE.exists():
        _links_cache = DEFAULT_LINKS.copy()
        _flush_links(_links_cache)
        return _links_cache
    with open(LINKS_FILE, encoding="utf-8") as f:
        _links_cache = json.load(f)
    return _links_cache


def save_links(data: dict) -> None:
    global _links_cache
    _links_cache = data
    _flush_links(data)


def _flush_links(data: dict) -> None:
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_texts() -> dict:
    global _texts_cache
    if _texts_cache is not None:
        return _texts_cache
    if not TEXTS_FILE.exists():
        _texts_cache = DEFAULT_TEXTS.copy()
        _flush_texts(_texts_cache)
        return _texts_cache
    with open(TEXTS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    for k, v in DEFAULT_TEXTS.items():
        data.setdefault(k, v)
    _texts_cache = data
    return _texts_cache


def save_texts(data: dict) -> None:
    global _texts_cache
    _texts_cache = data
    _flush_texts(data)


def _flush_texts(data: dict) -> None:
    with open(TEXTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_admin_ids() -> list[int]:
    global _admin_ids_cache
    if _admin_ids_cache is not None:
        return _admin_ids_cache
    extra: list[int] = []
    if ADMINS_FILE.exists():
        with open(ADMINS_FILE, encoding="utf-8") as f:
            extra = json.load(f)
    _admin_ids_cache = list(set(ADMIN_IDS + extra))
    return _admin_ids_cache


def save_admin_ids_extra(ids: list[int]) -> None:
    global _admin_ids_cache
    _admin_ids_cache = None  # сбросить кэш — пересчитается при следующем вызове
    with open(ADMINS_FILE, "w", encoding="utf-8") as f:
        json.dump(ids, f, ensure_ascii=False, indent=2)


def get_uploader() -> PhotoMessageUploader:
    global _uploader
    if _uploader is None:
        _uploader = PhotoMessageUploader(bot.api)
    return _uploader


# ─── Фоновый сборщик мусора — каждые 5 минут возвращает память ОС ────────────

async def _memory_trimmer():
    await asyncio.sleep(60)
    while True:
        gc.collect()
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
        await asyncio.sleep(300)


# FSM хранится в SQLite
get_state = bot_db.get_state
set_state = bot_db.set_state
get_state_data = bot_db.get_state_data
clear_state = bot_db.clear_state


# ─── Keyboard builder ──────────────────────────────────────────────────────────

def build_keyboard(*rows) -> str:
    kb = Keyboard(one_time=False, inline=False)
    for row_idx, row in enumerate(rows):
        for item in row:
            label = item[0]
            payload = item[1]
            color = item[2] if len(item) > 2 else KeyboardButtonColor.PRIMARY
            if payload.startswith("url:"):
                kb.add(OpenLink(payload[4:], label))
            else:
                kb.add(Text(label, payload={"cmd": payload}), color)
        if row_idx < len(rows) - 1:
            kb.row()
    return kb.get_json()


W = KeyboardButtonColor.PRIMARY
S = KeyboardButtonColor.SECONDARY
N = KeyboardButtonColor.NEGATIVE
P = KeyboardButtonColor.POSITIVE


def _venera_btn_rows(links: dict) -> list:
    url = links.get("venera_vk", "")
    if url:
        return [[("Написать Венере", f"url:{url}", W)]]
    return []


def main_keyboard(is_admin: bool = False) -> str:
    rows = [
        [("Гайд по чистоте", "clean_main", W), ("Уход за собой", "care_main", W)],
        [("Здоровье", "health_main", W), ("Подарок -20%", "gift_btn", P)],
        [("Каталог", "catalog_btn", S), ("Задать вопрос", "ask_btn", S)],
    ]
    if is_admin:
        rows.append([("Настройки бота", "open_admin", N)])
    return build_keyboard(*rows)


def admin_keyboard() -> str:
    return build_keyboard(
        [("Мои клиенты", "adm_leads", S), ("Статистика", "adm_stats", S)],
        [("Тексты", "adm_texts", S), ("Ссылки", "adm_links", S)],
        [("Рассылка", "adm_broadcast", W), ("Помощники", "adm_admins", S)],
        [("Как пользоваться", "adm_help", S)],
    )


def stage_label(stage: str) -> str:
    return {
        "started": "только зашёл",
        "greeted": "познакомились",
        "showed_link": "смотрел регистрацию",
        "completed": "зарегистрировался",
        "registered": "зарегистрировался",
    }.get(stage, stage)


# ─── Отправка с фото (используем синглтон uploader) ──────────────────────────

async def send_with_photo(bot: Bot, peer_id: int, photo_name: str, text: str, keyboard: str = None):
    path = MEDIA_DIR / f"{photo_name}.jpg"
    kwargs = {"peer_id": peer_id, "message": text}
    if keyboard:
        kwargs["keyboard"] = keyboard
    if path.exists():
        try:
            photo = await get_uploader().upload(str(path))
            kwargs["attachment"] = photo
        except Exception as e:
            log.warning("Photo upload failed (%s): %s", photo_name, e)
    await bot.api.messages.send(random_id=int(time.time() * 1000) % 2147483647, **kwargs)


# ─── Инициализация бота ───────────────────────────────────────────────────────

bot = Bot(token=VK_TOKEN)


@bot.on.startup()
async def on_startup():
    asyncio.create_task(_memory_trimmer())
    log.info("Memory trimmer started")


# ─── /start и приветствие ─────────────────────────────────────────────────────

@bot.on.message(text=["начать", "start", "/start", "привет", "Начать", "Start"])
async def cmd_start(message: Message):
    vk_id = message.from_id
    user_info = await bot.api.users.get(user_ids=[vk_id])
    first_name = user_info[0].first_name if user_info else "друг"
    last_name = user_info[0].last_name if user_info else ""

    is_new = bot_db.get_user(vk_id) is None
    bot_db.upsert_user(vk_id, first_name, last_name, "direct")
    bot_db.update_stage(vk_id, "greeted", name=first_name)
    bot_db.log_event(vk_id, "start")
    clear_state(vk_id)

    if is_new:
        for admin_id in load_admin_ids():
            try:
                await bot.api.messages.send(
                    peer_id=admin_id,
                    message=f"Новый пользователь: {first_name} {last_name}".strip() + f" (id{vk_id})",
                    random_id=int(time.time() * 1000) % 2147483647,
                )
            except Exception:
                pass

    await _send_main_menu(vk_id, first_name)


async def _send_main_menu(vk_id: int, name: str):
    welcome = load_texts().get("welcome_text", DEFAULT_TEXTS["welcome_text"])
    text = (
        f"Привет, {name}!\n\n"
        f"Я твой гид по чистоте и уходу от Faberlic\n\n"
        f"{welcome}"
    )
    is_admin = vk_id in load_admin_ids()
    await send_with_photo(bot, vk_id, "venera", text, main_keyboard(is_admin))


# ─── Основной роутер ──────────────────────────────────────────────────────────

@bot.on.message()
async def message_router(message: Message):
    vk_id = message.from_id
    payload = message.payload
    text_raw = (message.text or "").strip()

    cmd = None
    if payload:
        if isinstance(payload, dict):
            cmd = payload.get("cmd")
        elif isinstance(payload, str):
            try:
                cmd = json.loads(payload).get("cmd")
            except Exception:
                pass

    state = get_state(vk_id)

    # ── FSM ──────────────────────────────────────────────────────────────────

    if state == "adm_waiting_broadcast":
        if vk_id not in load_admin_ids():
            clear_state(vk_id)
            return
        set_state(vk_id, "adm_waiting_broadcast_confirm", broadcast_text=text_raw)
        count = len(bot_db.get_all_vk_ids())
        confirm_kb = build_keyboard(
            [("Да, отправить всем", "adm_broadcast_confirm", P), ("Отменить", "adm_back", N)],
        )
        await message.answer(
            f"Предпросмотр:\n---\n{text_raw}\n---\n\n"
            f"Получат: {count} человек.\nОтправляем?",
            keyboard=confirm_kb,
        )
        return

    if state == "adm_waiting_link_value":
        if vk_id not in load_admin_ids():
            clear_state(vk_id)
            return
        data = get_state_data(vk_id)
        key = data.get("link_key")
        links = load_links()
        links[key] = text_raw
        save_links(links)
        clear_state(vk_id)
        await message.answer(
            f"Ссылка обновлена!\n\nТеперь в боте стоит:\n{text_raw}",
            keyboard=build_keyboard([("Назад в настройки", "adm_back", S)]),
        )
        return

    if state == "adm_waiting_text_value":
        if vk_id not in load_admin_ids():
            clear_state(vk_id)
            return
        data = get_state_data(vk_id)
        text_key = data.get("text_key")
        texts = load_texts()
        texts[text_key] = text_raw
        save_texts(texts)
        clear_state(vk_id)
        await message.answer(
            "Текст обновлён! Проверь — отправь боту 'начать'.",
            keyboard=build_keyboard([("Назад в настройки", "adm_back", S)]),
        )
        return

    if state == "adm_waiting_new_admin":
        if vk_id not in load_admin_ids():
            clear_state(vk_id)
            return
        if not text_raw.lstrip("-").isdigit():
            await message.answer("Нужно прислать только цифры — это VK ID.")
            return
        new_id = int(text_raw)
        extra: list[int] = []
        if ADMINS_FILE.exists():
            with open(ADMINS_FILE, encoding="utf-8") as f:
                extra = json.load(f)
        if new_id not in extra:
            extra.append(new_id)
        save_admin_ids_extra(extra)
        clear_state(vk_id)
        await message.answer(
            "Помощник добавлен! Теперь у него есть доступ к настройкам бота.",
            keyboard=build_keyboard([("Назад в настройки", "adm_back", S)]),
        )
        return

    # ── Payload ───────────────────────────────────────────────────────────────

    if cmd:
        await _handle_cmd(vk_id, cmd, message)
        return

    # ── Текстовые команды ─────────────────────────────────────────────────────

    low = text_raw.lower()
    if low in ("/admin", "admin", "настройки"):
        if vk_id in load_admin_ids():
            await message.answer("Настройки бота. Выбери действие:", keyboard=admin_keyboard())
        return

    user = bot_db.get_user(vk_id)
    name = (user or {}).get("name") or "друг"
    welcome = load_texts().get("welcome_text", DEFAULT_TEXTS["welcome_text"])
    is_admin = vk_id in load_admin_ids()
    await message.answer(
        f"Привет, {name}!\n\nЯ твой гид по чистоте и уходу от Faberlic\n\n{welcome}",
        keyboard=main_keyboard(is_admin),
    )


# ─── Центральный обработчик команд ────────────────────────────────────────────

async def _handle_cmd(vk_id: int, cmd: str, message: Message):
    links = load_links()

    if cmd == "back_main":
        user = bot_db.get_user(vk_id)
        name = (user or {}).get("name") or "друг"
        await _send_main_menu(vk_id, name)
        return

    if cmd == "open_admin":
        if vk_id in load_admin_ids():
            await message.answer("Настройки бота:", keyboard=admin_keyboard())
        return

    # back_* алиасы указывают на те же экраны — не дублируем код
    if cmd in ("clean_main", "back_clean"):
        text = (
            "Косметика для дома Faberlic\n\n"
            "Средства для стирки, уборки кухни, ванной.\n"
            "Экологичные составы, концентраты.\n\n"
            f"Посмотреть каталог:\n{links.get('catalog_home', links.get('catalog', ''))}"
        )
        await send_with_photo(
            bot, vk_id, "clean_main", text,
            build_keyboard(
                [("Хочу зарегистрироваться", f"url:{links['reg']}", P)],
                [("Назад", "back_main", S)],
            ),
        )
        return

    if cmd in ("care_main", "back_care"):
        text = (
            "Уход за собой Faberlic\n\n"
            "Уход за лицом, телом, волосами — всё в одном месте.\n"
            "Кремы, сыворотки, маски, гели для душа.\n\n"
            f"Посмотреть каталог:\n{links.get('catalog_care', links.get('catalog', ''))}"
        )
        await send_with_photo(
            bot, vk_id, "care_main", text,
            build_keyboard(
                [("Хочу зарегистрироваться", f"url:{links['reg']}", P)],
                [("Назад", "back_main", S)],
            ),
        )
        return

    if cmd in ("health_main", "back_health"):
        text = (
            "Здоровье и стройность Faberlic\n\n"
            "Wellness-коктейли, БАДы, программы стройности.\n\n"
            f"◾ Здоровье и стройность:\n{links.get('catalog_health', links.get('catalog', ''))}\n\n"
            f"◾ Восточный секрет (японская медицина, добавки):\n{links.get('catalog_eastern', links.get('catalog', ''))}"
        )
        await send_with_photo(
            bot, vk_id, "health_main", text,
            build_keyboard(
                [("Хочу зарегистрироваться", f"url:{links['reg']}", P)],
                [("Назад", "back_main", S)],
            ),
        )
        return

    if cmd == "gift_btn":
        text = load_texts().get("gift_promo", DEFAULT_TEXTS["gift_promo"])
        await send_with_photo(
            bot, vk_id, "gift_promo", text,
            build_keyboard(
                [("Зарегистрироваться", f"url:{links['reg']}", P)],
                *_venera_btn_rows(links),
                [("Главное меню", "back_main", S)],
            ),
        )
        return

    if cmd == "catalog_btn":
        catalog_text = (
            "Выбери раздел каталога:\n\n"
            f"◾ УХОД ЗА СОБОЙ (косметика, кремы, уход):\n{links.get('catalog_care', links.get('catalog', ''))}\n\n"
            f"◾ КОСМЕТИКА ДЛЯ ДОМА:\n{links.get('catalog_home', links.get('catalog', ''))}\n\n"
            f"◾ ЗДОРОВЬЕ И СТРОЙНОСТЬ:\n{links.get('catalog_health', links.get('catalog', ''))}\n\n"
            f"◾ ПАРФЮМЕРИЯ И АРОМАТЫ:\n{links.get('catalog_perfume', links.get('catalog', ''))}\n\n"
            f"◾ ВСЁ ДЛЯ МАКИЯЖА:\n{links.get('catalog_makeup', links.get('catalog', ''))}\n\n"
            f"◾ НОВИНКИ FABERLIC:\n{links.get('catalog_sets', links.get('catalog', ''))}\n\n"
            f"📖 КАТАЛОГ FABERLIC:\n{links.get('catalog', '')}\n\n"
            f"📖 КАТАЛОГ AVON:\n{links.get('catalog_avon', links.get('catalog', ''))}"
        )
        await message.answer(
            catalog_text,
            keyboard=build_keyboard([("Назад", "back_main", S)]),
        )
        return

    if cmd in ("ask_btn", "ask_venera"):
        await message.answer(
            "Напишу лично — отвечу на все вопросы!\n\n"
            "Работаю с Faberlic уже несколько лет и знаю продукцию как свои пять пальцев",
            keyboard=build_keyboard(
                *_venera_btn_rows(links),
                [("Главное меню", "back_main", S)],
            ),
        )
        return

    want_cmds = {
        "want_laundry", "want_kitchen", "want_bath",
        "want_face", "want_body", "want_hair", "want_hygiene",
        "want_wellness", "want_bads",
    }
    if cmd in want_cmds:
        bot_db.log_event(vk_id, "want", cmd)
        bot_db.mark_link_shown(vk_id)
        bot_db.update_stage(vk_id, "showed_link")
        await message.answer(
            "Отлично! Твои ключики от личного кабинета\n\n"
            "Посмотри — это как Wildberries или Ozon, только с подарками и скидками -20%\n\n"
            "Заказывай любимые средства и забирай их в комфортной обстановке.\n"
            "Наши пункты выдачи оборудованы всем необходимым, вежливые сотрудники всегда помогут.\n\n"
            "Я искренне рада каждому новому другу! Давай дружить!\n\n"
            "Ну что, открываем кабинет?",
            keyboard=build_keyboard(
                [("Да, открываю!", f"url:{links['reg']}", P)],
                [("Позже", "reg_later", S), ("Не сейчас", "reg_no", N)],
            ),
        )
        return

    if cmd == "reg_later":
        await message.answer(
            "Хорошо, сохрани ссылку — она не временная.\nКогда будешь готова — я здесь!",
            keyboard=build_keyboard([("Главное меню", "back_main", S)]),
        )
        return

    if cmd == "reg_no":
        await message.answer(
            "Понимаю, не каждый день меняют привычки.\n"
            "Если захочешь вернуться — просто напиши 'начать'.\n"
            "Буду ждать!",
        )
        return

    # ── Админ-панель ──────────────────────────────────────────────────────────

    if cmd == "adm_back":
        if vk_id not in load_admin_ids():
            return
        clear_state(vk_id)
        await message.answer("Настройки бота:", keyboard=admin_keyboard())
        return

    if cmd == "adm_leads":
        if vk_id not in load_admin_ids():
            return
        await message.answer(
            "Мои клиенты\n\nСписок людей, которые написали твоему боту.",
            keyboard=build_keyboard(
                [("Последние 10 клиентов", "adm_leads_view", S)],
                [("Скачать всех (CSV)", "adm_export", S)],
                [("Назад", "adm_back", S)],
            ),
        )
        return

    if cmd == "adm_leads_view":
        if vk_id not in load_admin_ids():
            return
        leads = bot_db.get_recent_leads(10)
        if not leads:
            await message.answer(
                "Пока никто не писал боту.",
                keyboard=build_keyboard([("Назад", "adm_leads", S)]),
            )
            return
        lines = ["Последние клиенты:\n"]
        for i, u in enumerate(leads, 1):
            name = u.get("name") or u.get("first_name") or "Без имени"
            label = stage_label(u.get("funnel_stage", ""))
            lines.append(f"{i}. {name} (vk_id: {u['vk_id']}) — {label}")
        await message.answer(
            "\n".join(lines),
            keyboard=build_keyboard([("Назад", "adm_leads", S)]),
        )
        return

    if cmd == "adm_export":
        if vk_id not in load_admin_ids():
            return
        await message.answer("Формирую таблицу с клиентами...")
        fields = ["vk_id", "first_name", "last_name", "name", "source",
                  "funnel_stage", "first_seen", "last_seen", "phone", "completed_at"]
        with bot_db.conn() as c:
            rows = c.execute(
                "SELECT vk_id, first_name, last_name, name, source, "
                "funnel_stage, first_seen, last_seen, phone, completed_at "
                "FROM users ORDER BY first_seen DESC"
            ).fetchall()
        tmp = Path(tempfile.NamedTemporaryFile(delete=False, suffix=".csv").name)
        with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row[k] for k in fields})
        await message.answer(
            f"CSV-файл сохранён на сервере:\n{tmp}\n\n"
            f"Всего клиентов: {len(rows)}\n\n"
            "(Скачай файл вручную с машины или настрой выгрузку через облако)",
            keyboard=build_keyboard([("Назад", "adm_leads", S)]),
        )
        return

    if cmd == "adm_stats":
        if vk_id not in load_admin_ids():
            return
        stats = bot_db.funnel_stats()
        stages = {s["funnel_stage"]: s["n"] for s in stats["stages"]}
        total = sum(stages.values())
        today = datetime.now(timezone.utc).date().isoformat()
        with bot_db.conn() as c:
            today_count = c.execute(
                f"SELECT COUNT(*) AS n FROM users WHERE first_seen LIKE '{today}%'"
            ).fetchone()["n"]
            want_count = c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE funnel_stage = 'showed_link'"
            ).fetchone()["n"]
            reg_count = c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE funnel_stage IN ('completed','registered')"
            ).fetchone()["n"]
        conv_pct = round(reg_count / total * 100) if total else 0
        source_lines = [f"- {s['source'] or 'неизвестно'}: {s['total']} чел." for s in stats["sources"]]
        sources_text = "\n".join(source_lines) if source_lines else "- нет данных"
        await message.answer(
            f"Как дела у бота\n\n"
            f"Всего пришли в бот: {total}\n"
            f"Пришли сегодня: {today_count}\n"
            f"Заинтересовались товарами: {want_count}\n"
            f"Открыли регистрацию: {reg_count}\n\n"
            f"Из каждых 100 гостей примерно {conv_pct} открыли кабинет.\n\n"
            f"Источники (откуда люди узнали):\n{sources_text}",
            keyboard=build_keyboard([("Назад", "adm_back", S)]),
        )
        return

    if cmd == "adm_texts":
        if vk_id not in load_admin_ids():
            return
        await message.answer(
            "Тексты в боте\n\nТы можешь поменять что бот говорит клиентам.",
            keyboard=build_keyboard(
                [("Изменить текст приветствия", "adm_edit_welcome", S)],
                [("Изменить текст акции/подарка", "adm_edit_gift", S)],
                [("Назад", "adm_back", S)],
            ),
        )
        return

    if cmd in ("adm_edit_gift", "adm_edit_welcome"):
        if vk_id not in load_admin_ids():
            return
        key_map = {"adm_edit_gift": "gift_promo", "adm_edit_welcome": "welcome_text"}
        text_key = key_map[cmd]
        texts = load_texts()
        current = texts.get(text_key, "")
        set_state(vk_id, "adm_waiting_text_value", text_key=text_key)
        label = "приветствия" if cmd == "adm_edit_welcome" else "акции/подарка"
        await message.answer(
            f"Напиши новый текст {label}.\n\n"
            f"Сейчас написано:\n---\n{current}\n---\n\n"
            "Отправь новый текст:",
        )
        return

    if cmd == "adm_links":
        if vk_id not in load_admin_ids():
            return
        await message.answer(
            "Ссылки в боте\n\nКаждый месяц Faberlic обновляет каталог — меняй ссылку здесь.",
            keyboard=build_keyboard(
                [("Ссылка на каталог", "adm_set_catalog", S)],
                [("Ссылка на регистрацию", "adm_set_reg", S)],
                [("Ссылка на мой ВКонтакте", "adm_set_vk", S)],
                [("Назад", "adm_back", S)],
            ),
        )
        return

    if cmd in ("adm_set_catalog", "adm_set_reg", "adm_set_vk"):
        if vk_id not in load_admin_ids():
            return
        key_map = {
            "adm_set_catalog": "catalog",
            "adm_set_reg": "reg",
            "adm_set_vk": "venera_vk",
        }
        key = key_map[cmd]
        links_data = load_links()
        current_val = links_data.get(key, "не задана")
        set_state(vk_id, "adm_waiting_link_value", link_key=key)
        await message.answer(
            f"Пришли мне новую ссылку.\n\n"
            f"Текущая ссылка:\n{current_val}\n\n"
            "Просто скопируй из браузера и отправь:",
        )
        return

    if cmd == "adm_broadcast":
        if vk_id not in load_admin_ids():
            return
        count = len(bot_db.get_all_vk_ids())
        set_state(vk_id, "adm_waiting_broadcast")
        await message.answer(
            f"Написать всем клиентам\n\n"
            f"Сейчас в базе: {count} человек\n\n"
            "Напиши текст сообщения и отправь:",
        )
        return

    if cmd == "adm_broadcast_confirm":
        if vk_id not in load_admin_ids():
            return
        data = get_state_data(vk_id)
        broadcast_text = data.get("broadcast_text", "")
        clear_state(vk_id)
        ids = bot_db.get_all_vk_ids()

        await message.answer(
            f"Рассылка запущена для {len(ids)} пользователей. Результат придёт отдельным сообщением.",
            keyboard=build_keyboard([("Назад в настройки", "adm_back", S)]),
        )

        async def _do_broadcast():
            ok = 0
            fail = 0
            for uid in ids:
                try:
                    await bot.api.messages.send(
                        peer_id=uid,
                        message=broadcast_text,
                        random_id=int(time.time() * 1000) % 2147483647,
                    )
                    ok += 1
                except Exception as e:
                    code = getattr(e, "error_code", None)
                    if code in (901, 902):
                        bot_db.mark_blocked(uid)
                        log.info("Broadcast: user %s blocked (code %s)", uid, code)
                    else:
                        log.warning("Broadcast error for %s: %s", uid, e)
                    fail += 1
                await asyncio.sleep(0.05)
            # После рассылки — принудительная очистка памяти
            gc.collect()
            try:
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            except Exception:
                pass
            try:
                await bot.api.messages.send(
                    peer_id=vk_id,
                    message=f"Рассылка завершена!\n\nОтправлено: {ok}\nНе доставлено: {fail}",
                    random_id=int(time.time() * 1000) % 2147483647,
                )
            except Exception:
                pass

        asyncio.create_task(_do_broadcast())
        return

    if cmd == "adm_admins":
        if vk_id not in load_admin_ids():
            return
        all_ids = load_admin_ids()
        ids_text = "\n".join(
            f"- {aid}" + (" (ты)" if aid == vk_id else "")
            for aid in all_ids
        )
        await message.answer(
            f"Мои помощники\n\nСейчас имеют доступ:\n{ids_text}",
            keyboard=build_keyboard(
                [("Добавить помощника", "adm_add_admin", S)],
                [("Убрать помощника", "adm_remove_admin", S)],
                [("Назад", "adm_back", S)],
            ),
        )
        return

    if cmd == "adm_add_admin":
        if vk_id not in load_admin_ids():
            return
        set_state(vk_id, "adm_waiting_new_admin")
        await message.answer(
            "Как добавить помощника:\n\n"
            "1. Попроси человека зайти на vk.com — его ID виден в адресной строке профиля\n"
            "2. Скопируй число (без 'id') и отправь мне\n\n"
            "Пришли VK ID нового помощника:",
        )
        return

    if cmd == "adm_remove_admin":
        if vk_id not in load_admin_ids():
            return
        extra: list[int] = []
        if ADMINS_FILE.exists():
            with open(ADMINS_FILE, encoding="utf-8") as f:
                extra = json.load(f)
        if not extra:
            await message.answer(
                "Помощников пока нет — некого убирать.",
                keyboard=build_keyboard([("Назад", "adm_admins", S)]),
            )
            return
        rows_kb = [[("Убрать: " + str(aid), f"adm_del_admin_{aid}", N)] for aid in extra]
        rows_kb.append([("Назад", "adm_admins", S)])
        await message.answer("Выбери кого убрать:", keyboard=build_keyboard(*rows_kb))
        return

    if cmd.startswith("adm_del_admin_"):
        if vk_id not in load_admin_ids():
            return
        try:
            del_id = int(cmd.removeprefix("adm_del_admin_"))
        except ValueError:
            return
        extra: list[int] = []
        if ADMINS_FILE.exists():
            with open(ADMINS_FILE, encoding="utf-8") as f:
                extra = json.load(f)
        extra = [x for x in extra if x != del_id]
        save_admin_ids_extra(extra)
        await message.answer(
            f"Помощник {del_id} убран. Теперь у него нет доступа к настройкам.",
            keyboard=build_keyboard([("Назад в настройки", "adm_back", S)]),
        )
        return

    if cmd == "adm_help":
        if vk_id not in load_admin_ids():
            return
        await message.answer(
            "Краткая инструкция\n\n"
            "Каталог меняется каждый месяц\n"
            "-> Зайди в Ссылки -> Ссылка на каталог\n"
            "-> Вставь новую ссылку из Faberlic\n\n"
            "Хочешь написать клиентам про акцию\n"
            "-> Зайди в Рассылка\n"
            "-> Напиши текст -> Отправь всем\n\n"
            "Изменились условия акции/подарка\n"
            "-> Зайди в Тексты -> Изменить текст акции\n\n"
            "Посмотреть кто написал\n"
            "-> Зайди в Мои клиенты\n\n"
            "Если что-то непонятно — обратись к своему менеджеру",
            keyboard=build_keyboard([("Назад в настройки", "adm_back", S)]),
        )
        return


# ─── Запуск ───────────────────────────────────────────────────────────────────

bot_db.init()
load_links()
MEDIA_DIR.mkdir(exist_ok=True)
log.info("VK Bot starting...")

if __name__ == "__main__":
    bot.run_forever()
