import asyncio
import random
import re

from arclet.alconna import Alconna, AllParam, Args, CommandMeta
from nonebot import get_driver, on_message, on_regex, require
from zhipuai import ZhipuAI

from zhenxun.services.log import logger
from zhenxun.utils.rules import ensure_group

require("nonebot_plugin_alconna")
from nonebot.adapters import Bot, Event
from nonebot.permission import SUPERUSER
from nonebot_plugin_alconna import Image, Match, Text, UniMessage, UniMsg, on_alconna
from nonebot_plugin_uninfo import ADMIN, Session, UniSession

from .config import ChatConfig
from .data_source import (
    ChatManager,
    ImpersonationStatus,
    cache_group_message,
    check_task_status_periodically,
    hello,
    split_text,
    submit_task_to_zhipuai,
)
from .rule import is_to_me

driver = get_driver()


@driver.on_startup
async def handle_connect():
    await ChatManager.initialize()


@driver.on_shutdown
async def handle_disconnect():
    await ChatManager.save()


draw_pic = on_alconna(
    Alconna("生成图片", Args["msg?", AllParam], meta=CommandMeta(compact=True)),
    priority=5,
    block=True,
)

draw_video = on_alconna(
    Alconna("生成视频", Args["message?", AllParam], meta=CommandMeta(compact=True)),
    priority=5,
    block=True,
)

byd_mode = on_regex(
    r"(启用|禁用)伪人模式\s*(?:/\d+)?",
    priority=5,
    permission=ADMIN(),
    block=True,
)

normal_chat = on_message(rule=is_to_me, priority=998, block=True)

byd_chat = on_message(rule=ensure_group, priority=999, block=False)

clear_my_chat = on_alconna(Alconna("清理我的会话"), priority=5, block=True)

clear_all_chat = on_alconna(
    Alconna("清理全部会话"), permission=SUPERUSER, priority=5, block=True
)

clear_group_chat = on_alconna(
    Alconna("清理群会话"),
    rule=ensure_group,
    permission=ADMIN(),
    priority=5,
    block=True,
)


@draw_pic.handle()
async def _(msg: Match[str]):
    if msg.available:
        draw_pic.set_path_arg("msg", str(msg.result))


@draw_video.handle()
async def _(msg: Match[str]):
    if msg.available:
        draw_video.set_path_arg("msg", str(msg.result))


@byd_mode.handle()
async def _(bot: Bot, event: Event, session: Session = UniSession()):
    command = event.get_plaintext()
    if match := re.match(r"(启用|禁用)伪人模式(?:\s*(\d+))?", command):
        action = match[1]
        if group_id := match[2]:
            if session.user.id not in bot.config.superusers:
                return

            if await ImpersonationStatus.action(action, int(group_id)):
                await UniMessage(Text(f"群聊 {group_id} 已 {action} 伪人模式")).send(
                    reply_to=True
                )
                logger.info(f"{action} 伪人模式", "zhipu_toolkit", session=session)
            else:
                await UniMessage(
                    Text(f"群聊 {group_id} 伪人模式不可重复 {action}")
                ).send(reply_to=True)
        elif ensure_group(session):
            if await ImpersonationStatus.action(action, session.scene.id):
                await UniMessage(Text(f"当前群聊已 {action} 伪人模式")).send(
                    reply_to=True
                )
                logger.info(f"{action} 伪人模式", "zhipu_toolkit", session=session)
            else:
                await UniMessage(Text(f"当前群聊伪人模式不可重复 {action}")).send(
                    reply_to=True
                )


@normal_chat.handle()
async def _(msg: UniMsg, session: Session = UniSession()):
    if msg.extract_plain_text() == "":
        result = await hello()
        await UniMessage([Text(result[0]), Image(path=result[1])]).finish(reply_to=True)
    if ChatConfig.get("API_KEY") == "":
        await UniMessage(Text("请先设置智谱AI的APIKEY!")).send(reply_to=True)
    else:
        result = await ChatManager.normal_chat_result(msg, session)
        for r, delay in await split_text(result):
            await UniMessage(r).send()
            await asyncio.sleep(delay)


@byd_chat.handle()
async def _(msg: UniMsg, bot: Bot, session: Session = UniSession()):
    if await ImpersonationStatus.check(session):
        if ChatConfig.get("API_KEY") == "":
            return
        await cache_group_message(msg, session)
        if random.random() * 100 < ChatConfig.get("IMPERSONATION_TRIGGER_FREQUENCY"):
            result = await ChatManager.impersonation_result(msg, session, bot)
            if result:
                await UniMessage(result).send()
    else:
        logger.debug("伪人模式被禁用.skip...", "zhipu_toolkit", session=session)


@clear_my_chat.handle()
async def _(session: Session = UniSession()):
    uid = session.user.id
    await clear_my_chat.send(
        Text(f"已清理 {uid} 的 {await ChatManager.clear_history(uid)} 条数据"),
        reply_to=True,
    )


@clear_all_chat.handle()
async def _():
    await clear_all_chat.send(
        Text(f"已清理 {await ChatManager.clear_history()} 条用户数据"),
        reply_to=True,
    )


@clear_group_chat.handle()
async def _(session: Session = UniSession()):
    count = await ChatManager.clear_history(f"g-{session.scene.id}")
    await clear_group_chat.send(
        Text(f"已清理 {count} 条用户数据"),
        reply_to=True,
    )


@draw_pic.got_path("msg", prompt="你要画什么呢")
async def handle_check(msg: str):
    if ChatConfig.get("API_KEY") == "":
        await draw_pic.send(Text("请先设置智谱AI的APIKEY!"), reply_to=True)
    else:
        try:
            loop = asyncio.get_event_loop()
            client = ZhipuAI(api_key=ChatConfig.get("API_KEY"))
            response = await loop.run_in_executor(
                None,
                lambda: client.images.generations(
                    model=ChatConfig.get("PIC_MODEL"), prompt=msg, size="1440x720"
                ),
            )
            await draw_pic.send(Image(url=response.data[0].url), reply_to=True)
        except Exception as e:
            await draw_pic.send(Text(f"错了：{e}"), reply_to=True)


@draw_video.got_path("message", prompt="你要制作什么视频呢")
async def submit_task(msg: str):
    if ChatConfig.get("API_KEY") == "":
        await draw_pic.send(Text("请先设置智谱AI的APIKEY!"), reply_to=True)
    else:
        try:
            response = await submit_task_to_zhipuai(msg)
        except Exception as e:
            await draw_video.send(Text(str(e)))
        else:
            if response.task_status != "FAIL":
                await draw_video.send(
                    Text(f"任务已提交,id: {response.id}"), reply_to=True
                )
                asyncio.create_task(  # noqa: RUF006
                    check_task_status_periodically(response.id, draw_video)  # type: ignore
                )
            else:
                await draw_video.send(
                    Text(f"任务提交失败，e:{response}"), reply_to=True
                )
