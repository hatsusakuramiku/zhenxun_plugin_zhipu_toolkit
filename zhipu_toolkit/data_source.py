import asyncio
import datetime
import os
from pathlib import Path
import random
import re
from typing import ClassVar
import uuid

import aiofiles
from nonebot.adapters import Bot
from nonebot_plugin_alconna import At, Image, Text, UniMsg, Video
from nonebot_plugin_uninfo import Session
import ujson
from zhipuai import ZhipuAI

from zhenxun.configs.config import BotConfig, Config
from zhenxun.configs.path_config import DATA_PATH, IMAGE_PATH
from zhenxun.models.ban_console import BanConsole
from zhenxun.services.log import logger
from zhenxun.utils.rules import ensure_group

from .config import ChatConfig, GroupMessageModel

GROUP_MSG_CACHE: dict[str, list[GroupMessageModel]] = {}


async def __split_text(text: str, pattern: str, maxsplit: int) -> list[str]:
    """辅助函数，用于分割文本"""
    return re.split(pattern, text, maxsplit)


async def split_text(text: str) -> list[tuple[list, float]]:
    """文本切割"""
    results = []
    split_list = [
        s for s in await __split_text(text, r"(?<!\?)[。？！\n](?!\?)", 3) if s.strip()
    ]
    for r in split_list:
        next_char_index = text.find(r) + len(r)
        if next_char_index < len(text) and text[next_char_index] == "？":
            r += "？"
        results.append((await parse_at(r), min(len(r) * 0.2, 3.0)))
    return results


async def cache_group_message(message: UniMsg, session: Session, self=None) -> None:
    """
    异步缓存群组消息函数。

    该函数用于将接收到的群组消息缓存到内存中，以便后续处理。
    如果self参数不为空，则表示消息来自机器人自身，否则消息来自其他用户。
    使用GroupMessageModel模型来封装消息信息。

    参数:
    - message: UniMsg类型，表示接收到的消息。
    - session: Session类型，表示当前会话，包含消息上下文信息。
    - self: 可选参数，表示如果消息来自机器人自身，该参数不为空。

    返回值:
    无返回值。
    """
    if self is not None:
        msg = GroupMessageModel(
            uid=session.self_id,
            nickname=self["nickname"],
            msg=self["msg"],
        )
    else:
        msg = GroupMessageModel(
            uid=session.user.id,
            nickname=await ChatManager.get_user_nickname(session),
            msg=await ChatManager.parse_msg(message),
        )

    gid = session.scene.id
    logger.debug(f"GROUP {gid} 成功缓存聊天记录: {msg}", "zhipu_toolkit")
    if gid in GROUP_MSG_CACHE:
        if len(GROUP_MSG_CACHE[gid]) >= 20:
            GROUP_MSG_CACHE[gid].pop(0)
            logger.debug(f"GROUP {gid} 缓存已满，自动清理最早的记录", "zhipu_toolkit")

        GROUP_MSG_CACHE[gid].append(msg)
    else:
        GROUP_MSG_CACHE[gid] = [msg]


async def parse_at(message: str) -> list:
    """
    将字符串消息转换为消息段列表。

    该函数解析输入的字符串消息，将其中的 `@` 转换为对应的消息段，并将文本分割成每句话。

    :param message: 输入的字符串消息。
    :return: 包含消息段的列表，每个消息段为 MessageSegment 实例。
    """
    segments = []
    message = message.removesuffix("。")
    at_pattern = r"@(\d+)"
    last_pos = 0

    for match in re.finditer(at_pattern, message, re.DOTALL):
        if match.start() > last_pos:
            segments.append(Text(message[last_pos : match.start()]))
        uid = match.group(1)
        segments.append(At("user", uid))
        last_pos = match.end()
    if last_pos < len(message):
        segments.append(Text(message[last_pos:]))

    return segments


async def submit_task_to_zhipuai(message: str):
    """
    异步提交视频生成任务到ZhipuAI。

    该函数使用聊天配置中的API密钥初始化ZhipuAI客户端，
    然后使用指定的视频模型和提示生成视频。

    参数:
    - message: str - 视频生成的提示。

    返回:
    - 无
    """
    client = ZhipuAI(api_key=ChatConfig.get("API_KEY"))
    return client.videos.generations(
        model=ChatConfig.get("VIDEO_MODEL"),
        prompt=message,
        with_audio=True,
    )


async def hello() -> list:
    """一些打招呼的内容"""
    result = random.choice(
        [
            "哦豁？！",
            "你好！Ov<",
            f"库库库，呼唤{BotConfig.self_nickname}做什么呢",
            "我在呢！",
            "呼呼，叫俺干嘛",
        ]
    )
    img = random.choice(os.listdir(IMAGE_PATH / "zai"))
    return [result, IMAGE_PATH / "zai" / img]


async def check_task_status_periodically(task_id: str, action) -> None:
    """
    定期检查任务状态的异步函数。

    参数:
    - task_id (str): 任务的唯一标识符。
    - action: 执行动作的对象，用于发送消息。

    返回:
    - None
    """
    while True:
        try:
            response = await check_task_status_from_zhipuai(task_id)
        except Exception as e:
            await action.send(Text(str(e)), reply_to=True)
            break
        else:
            if response.task_status == "SUCCESS":
                await action.send(Video(url=response.video_result[0].url))
                break
            elif response.task_status == "FAIL":
                await action.send(Text("生成失败了.: ."), reply_to=True)
                break
            await asyncio.sleep(2)


async def check_task_status_from_zhipuai(task_id: str):
    """
    异步获取指定任务的处理状态。

    本函数通过调用ZhipuAI的API来查询给定任务ID的任务处理状态，主要用于视频处理任务的查询。

    参数:
    task_id (str): 需要查询处理状态的任务ID。

    返回:
    返回ZhipuAI的API调用结果，包含任务的详细处理状态信息。
    """
    client = ZhipuAI(api_key=ChatConfig.get("API_KEY"))
    return client.videos.retrieve_videos_result(id=task_id)


class ChatManager:
    chat_history: ClassVar[dict] = {}
    DATA_FILE_PATH: Path = DATA_PATH / "zhipu_toolkit"
    chat_history_token: ClassVar[dict] = {}
    impersonation_group: ClassVar[dict] = {}

    @classmethod
    async def initialize(cls) -> None:
        """
        初始化变量
        """
        os.makedirs(cls.DATA_FILE_PATH, exist_ok=True)
        cls.chat_history = await cls.load_data()

    @classmethod
    async def load_data(cls) -> dict:
        """
        从 JSON 文件中加载对话数据
        """
        if not os.path.exists(cls.DATA_FILE_PATH / "chat_history.json"):
            return {}

        async with aiofiles.open(
            cls.DATA_FILE_PATH / "chat_history.json", encoding="utf-8"
        ) as file:
            data = ujson.loads(await file.read())

        return data

    @classmethod
    async def save(cls) -> None:
        """
        将对话数据保存到 JSON 文件中。
        """
        async with aiofiles.open(
            cls.DATA_FILE_PATH / "chat_history.json", mode="w", encoding="utf-8"
        ) as file:
            await file.write(
                ujson.dumps(cls.chat_history, ensure_ascii=False, indent=4)
            )

    @classmethod
    async def check_token(cls, uid: str, token_len: int):
        return  # 暂时没用，文档似乎说是单条token最大4095
        # if cls.chat_history_token.get(uid) is None:
        #     cls.chat_history_token[uid] = 0
        # cls.chat_history_token[uid] += token_len

        # user_history = cls.chat_history.get(uid, [])
        # while cls.chat_history_token[uid] > 4095 and len(user_history) > 1:
        #     removed_token_len = len(user_history[1]["content"])
        #     user_history = user_history[1:]
        #     cls.chat_history_token[uid] -= removed_token_len

        # cls.chat_history[uid] = user_history

    @classmethod
    async def normal_chat_result(cls, msg: UniMsg, session: Session) -> str:
        match ChatConfig.get("CHAT_MODE"):
            case "user":
                uid = session.user.id
            case "group":
                uid = "g-" + (
                    session.scene.id if ensure_group(session) else session.user.id
                )
            case "all":
                uid = "mix_mode"
            case _:
                raise ValueError("CHAT_MODE must be 'user', 'group' or 'all'")
        nickname = await cls.get_user_nickname(session)
        await cls.add_system_message(ChatConfig.get("SOUL"), uid)
        message = await cls.parse_msg(msg)
        words = f"[发送于 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} from {nickname}]:{message}"
        if len(words) > 4095:
            logger.warning(
                f"USER {uid} NICKNAME {nickname} 问题: {words} ---- 超出最大token限制: 4095",  # noqa: E501
                "zhipu_toolkit",
                session=session,
            )
            return "超出最大token限制: 4095"
        await cls.add_message(words, uid)
        result = await cls.get_zhipu_result(
            uid, ChatConfig.get("CHAT_MODEL"), cls.chat_history[uid], session
        )
        if result[1] is False:
            logger.info(
                f"NICKNAME `{nickname}` 问题: {words} ---- 触发内容审查",
                "zhipu_toolkit",
                session=session,
            )
            return result[0]
        await cls.add_message(result[0], uid, role="assistant")
        logger.info(
            f"NICKNAME `{nickname}` 问题：{words} ---- 回答：{result[0]}",
            "zhipu_toolkit",
            session=session,
        )
        return result[0]

    @classmethod
    async def add_message(cls, words: str, uid: str, role="user") -> None:
        cls.chat_history[uid].append({"role": role, "content": words})
        await cls.check_token(uid, len(words))

    @classmethod
    async def add_system_message(cls, soul: str, uid: str) -> None:
        if cls.chat_history.get(uid) is None:
            cls.chat_history[uid] = [{"role": "system", "content": soul}]

    @classmethod
    async def clear_history(cls, uid: str | None = None) -> int:
        if uid is None:
            count = len(cls.chat_history)
            cls.chat_history = {}
        elif cls.chat_history.get(uid) is None:
            count = 0
        else:
            count = len(cls.chat_history[uid])
            del cls.chat_history[uid]
        return count

    @classmethod
    async def parse_msg(cls, msg: UniMsg) -> str:
        message = ""
        for segment in msg:
            if isinstance(segment, At):
                message += f"@{segment.target} "
            elif isinstance(segment, Image):
                assert segment.url is not None
                url = segment.url.replace(
                    "https://multimedia.nt.qq.com.cn", "http://multimedia.nt.qq.com.cn"
                )
                message += (
                    f"\n![{await cls.__generate_image_description(url)}]\n({url})"
                )
            elif isinstance(segment, Text):
                message += segment.text
        return message

    @classmethod
    async def get_user_nickname(cls, session: Session) -> str:
        if (
            hasattr(session.member, "nick")
            and session.member is not None
            and session.member.nick != ""
            and session.member.nick is not None
        ):
            return session.member.nick
        return session.user.name if session.user.name is not None else "未知"

    @classmethod
    async def impersonation_result(
        cls, msg: UniMsg, session: Session, bot: Bot
    ) -> str | None:
        gid = session.scene.id
        if not (group_msg := GROUP_MSG_CACHE[gid]):
            return

        content = "".join(
            f"{msg.nickname} ({msg.uid})说:\n{msg.msg}\n\n" for msg in group_msg
        )
        my_info = await bot.get_group_member_info(group_id=gid, user_id=session.self_id)
        my_name = my_info["card"] or my_info["nickname"]
        head = f"你在一个QQ群里，你的QQ是`{session.self_id}`，你的名字是`{my_name}`。请你结合该群的聊天记录作出回应，要求表现得随性一点，需要参与讨论，混入其中。不要过分插科打诨，不要提起无关的话题，不知道说什么可以复读群友的话。不允许包含聊天记录的格式。如果觉得此时不需要自己说话，请只回复`<EMPTY>`。下面是群组的聊天记录：\n\n"  # noqa: E501
        foot = (
            "\n\n你的回复应该尽可能简练,一次只说一句话，像人类一样随意，不允许有emoji。"
        )
        soul = (
            ChatConfig.get("SOUL")
            if ChatConfig.get("IMPERSONATION_SOUL") is False
            else ChatConfig.get("IMPERSONATION_SOUL")
        )
        result = await cls.get_zhipu_result(
            str(uuid.uuid4()),
            ChatConfig.get("IMPERSONATION_MODEL"),
            [
                {
                    "role": "system",
                    "content": (
                        f"你需要遵循以下要求，同时保证回应中不包含聊天记录格式。{soul}"
                    ),
                },
                {
                    "role": "user",
                    "content": head + content + foot,
                },
            ],
            session,
            True,
        )
        if result[1] is False:
            logger.warning("伪人触发内容审查", "zhipu_toolkit", session=session)
            return
        result = result[0]
        if ":" in result:
            result = result.split(":")[-1].strip("\n")
        if "<EMPTY>" in result:
            logger.info("伪人不需要回复，已被跳过", "zhipu_toolkit", session=session)
            return
        logger.info(f"伪人回复: {result}", "zhipu_toolkit", session=session)
        await cache_group_message(
            msg,
            session,
            {
                "uid": my_info["user_id"],
                "nickname": my_name,
                "msg": result,
            },
        )
        return result

    @classmethod
    async def get_zhipu_result(
        cls,
        uid: str,
        model: str,
        messages: list,
        session: Session,
        impersonation: bool = False,
    ) -> tuple[str, bool]:
        loop = asyncio.get_event_loop()
        client = ZhipuAI(api_key=ChatConfig.get("API_KEY"))
        try:
            response = await loop.run_in_executor(
                None,
                lambda: client.chat.completions.create(
                    model=model,
                    messages=messages,
                    user_id=uid,
                ),
            )
        except Exception as e:
            error = str(e)
            if "assistant" in error:
                await asyncio.sleep(0.5)
                logger.warning(
                    f"UID {uid} AI回复内容触发内容审查: 执行自动重试",
                    "zhipu_toolkit",
                    session=session,
                )
                return await cls.get_zhipu_result(
                    uid, model, messages, session, impersonation
                )
            elif "user" in error:
                if not impersonation:
                    logger.warning(
                        f"UID {uid} 用户输入内容触发内容审查: 封禁用户 {session.user.id} 5 分钟",  # noqa: E501
                        "zhipu_toolkit",
                        session=session,
                    )
                    await BanConsole.ban(
                        session.user.id,
                        None,
                        9999,
                        300,
                    )

                return "输入内容包含不安全或敏感内容，你已被封禁5分钟", False
            else:  # history
                logger.warning(
                    f"UID {uid} 对话历史记录触发内容审查: 清理历史记录",
                    "zhipu_toolkit",
                    session=session,
                )
                await cls.clear_history(uid)
                return "历史记录包含违规内已被清除，请重新开始对话", False
        return response.choices[0].message.content, True  # type: ignore

    @classmethod
    async def __generate_image_description(cls, url: str):
        loop = asyncio.get_event_loop()
        client = ZhipuAI(api_key=ChatConfig.get("API_KEY"))
        try:
            response = await loop.run_in_executor(
                None,
                lambda: client.chat.completions.create(
                    model=ChatConfig.get("IMAGE_UNDERSTANDING_MODEL"),
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "描述图片"},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": url},
                                },
                            ],
                        }
                    ],
                    user_id=str(uuid.uuid4()),
                ),
            )
            result = response.choices[0].message.content  # type: ignore
        except Exception:
            result = ""
        assert isinstance(result, str)
        return result.replace("\n", "\\n")


class ImpersonationStatus:
    @classmethod
    async def check(cls, session: Session) -> bool:
        return ChatConfig.get(
            "IMPERSONATION_MODE"
        ) is True and session.scene.id not in ChatConfig.get("IMPERSONATION_BAN_GROUP")

    @classmethod
    async def get(cls) -> list[int | str]:
        return ChatConfig.get("IMPERSONATION_BAN_GROUP")

    @classmethod
    async def ban(cls, group_id: int | str) -> bool:
        origin = await cls.get()
        if group_id in origin:
            return False
        origin.append(group_id)
        Config.set_config("zhipu_toolkit", "IMPERSONATION_BAN_GROUP", origin, True)
        return True

    @classmethod
    async def unban(cls, group_id: int | str) -> bool:
        origin = await cls.get()
        if group_id not in origin:
            return False
        origin.remove(group_id)
        Config.set_config("zhipu_toolkit", "IMPERSONATION_BAN_GROUP", origin, True)
        return True

    @classmethod
    async def action(cls, action: str, group_id: int | str) -> bool:
        if action == "禁用":
            return await cls.ban(group_id)
        elif action == "启用":
            return await cls.unban(group_id)
        return False
