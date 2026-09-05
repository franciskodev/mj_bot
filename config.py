import os

from dotenv import load_dotenv


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHATGPT_API_KEY = os.getenv("CHATGPT_API_KEY")
CHATGPT_MODEL = os.getenv("CHATGPT_MODEL", "GPT-Terra")
CHATGPT_VISION_MODEL = os.getenv("CHATGPT_VISION_MODEL", CHATGPT_MODEL)
CHATGPT_BASE_URL = os.getenv(
    "CHATGPT_BASE_URL", "https://roooo11.alwaysdata.net/v1"
).rstrip("/")
CHAT_ERROR_CHANNEL_ID = 1534278088748306533

GUILD_ID = 1278169762140258367
GUILD_IDS = [GUILD_ID]
ALLOWED_GUILD_IDS = frozenset(GUILD_IDS)
BROADCAST_GUILD_ID = 1522843375840268338
BROADCAST_GUILD_IDS = [GUILD_ID, BROADCAST_GUILD_ID]
AUTHORIZED_GUILD_IDS = frozenset(BROADCAST_GUILD_IDS)
FORWARD_CHANNEL_ID = 1533059718543577118
ADMIN_LOG_CHANNEL_ID = 1533059717415309322
APPLICATION_LOG_CHANNEL_ID = ADMIN_LOG_CHANNEL_ID
MANAGEMENT_LOG_CHANNEL_ID = 1534272945537548350
PROMOTION_LOG_CHANNEL_ID = 1534273288954707989
APPEAL_LOG_CHANNEL_ID = 1534273566319837326
WARNING_WORKOFF_LOG_CHANNEL_ID = 1534273529778798665
WARNING_LOG_CHANNEL_ID = 1534274636290719786
SERVER_TAG_LOG_CHANNEL_ID = 1534298855741456437
BLACKLIST_CHANNEL_ID = 1535946089973157978
GIVEAWAY_CHANNEL_ID = 1545784491900014652
DEVELOPER_USER_ID = 811292492481298432
SERVER_ROLE_IDS: dict[str, int] = {
    "Seattle": 1533059448237461504,
    "Orlando": 1533059447214182531,
    "Denver": 1538394081196118047,
}
DEFAULT_MEMBER_ROLE_ID: int | None = 1533059454591959040
LEAVE_ROLE_ID = 1533059441778229382
TARGET_ROLE_IDS: set[int] = {1533059444076970135, 1533059442277613651}
FAMILY_MEMBER_TRIGGER_ROLE_IDS: frozenset[int] = frozenset(
    {
        1533059446308339782,
        1533059442277613651,
        1533270195123126342,
        1533059426053914827,
    }
)
FAMILY_NAME = "INTERPOLOV & INTOLERANCE"
FAMILY_MEMBER_ROLE_ID = 1534271834638389399
APPLICATION_EXTRA_ACCEPTED_ROLE_IDS: frozenset[int] = frozenset(
    {1533059446308339782}
)
BROADCAST_ROLE_IDS: dict[int, set[int]] = {
    BROADCAST_GUILD_ID: {1522843375965831271, 1528511540779811046},
}

BROADCAST_TEXT = """# Привет!

Нужно, пожалуйста, ответить на 2 вопроса:

> Ты остаешься мейнить Seattle или идешь вместе с нами на Orlando?
> Сможешь ли ты зайти на Orlando и ввести наш семейный промокод: INRP? Нам это нужно для развития и улучшения семьи, в первую очередь для комфорта ее членов.

Даже если ты остаешься на Seattle, очень просим активировать именно наш промокод. Это сильно поможет семье в развитии нового сервера.

**Напомню, что даже если часть состава перейдет на Orlando, семья на Seattle никуда не исчезает. Мы будем развивать сразу два сервера, поэтому место и активность найдутся для каждого.**

*Если у тебя уже активирован другой промокод - тоже обязательно сообщи об этом.*

Жду ответ на оба вопроса в личные сообщения с ботом. Спасибо! <3 """
