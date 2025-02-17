from nonebot.adapters import Event
from nonebot_plugin_uninfo import Uninfo

async def is_to_me(event: Event) -> bool:
    msg = event.get_message().extract_plain_text()
    for nickname in nicknames:
        if nickname in msg:
            return True
    return event.is_tome()
