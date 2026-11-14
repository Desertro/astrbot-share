import datetime
import random
import uuid
from collections import defaultdict

from astrbot import logger
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Plain
from astrbot.api.platform import MessageType
from astrbot.api.provider import Provider, ProviderRequest
from astrbot.core.astrbot_config_mgr import AstrBotConfigManager

"""
聊天记忆增强
"""


class LongTermMemory:
    def __init__(self, acm: AstrBotConfigManager, context: star.Context):
        self.acm = acm
        self.context = context
        self.session_chats = defaultdict(list)
        """记录群成员的群聊记录"""
        self.session_struct_chats = defaultdict(list)
        """记录带消息ID的结构化群聊记录: {umo: [{id,sender_id,sender_name,content,timestamp,role,at_self}], ...}"""

    def cfg(self, event: AstrMessageEvent):
        cfg = self.context.get_config(umo=event.unified_msg_origin)
        try:
            max_cnt = int(cfg["provider_ltm_settings"]["group_message_max_cnt"])
        except BaseException as e:
            logger.error(e)
            max_cnt = 300
        image_caption = (
            True
            if cfg["provider_settings"]["default_image_caption_provider_id"]
            and cfg["provider_ltm_settings"]["image_caption"]
            else False
        )
        image_caption_prompt = cfg["provider_settings"]["image_caption_prompt"]
        image_caption_provider_id = cfg["provider_settings"][
            "default_image_caption_provider_id"
        ]
        active_reply = cfg["provider_ltm_settings"]["active_reply"]
        enable_active_reply = active_reply.get("enable", False)
        ar_method = active_reply["method"]
        ar_possibility = active_reply["possibility_reply"]
        ar_prompt = active_reply.get("prompt", "")
        ar_whitelist = active_reply.get("whitelist", [])
        ret = {
            "max_cnt": max_cnt,
            "image_caption": image_caption,
            "image_caption_prompt": image_caption_prompt,
            "image_caption_provider_id": image_caption_provider_id,
            "enable_active_reply": enable_active_reply,
            "ar_method": ar_method,
            "ar_possibility": ar_possibility,
            "ar_prompt": ar_prompt,
            "ar_whitelist": ar_whitelist,
        }
        return ret

    async def remove_session(self, event: AstrMessageEvent) -> int:
        cnt = 0
        if event.unified_msg_origin in self.session_chats:
            cnt = len(self.session_chats[event.unified_msg_origin])
            del self.session_chats[event.unified_msg_origin]
        return cnt

    async def get_image_caption(
        self,
        image_url: str,
        image_caption_provider_id: str,
        image_caption_prompt: str,
    ) -> str:
        if not image_caption_provider_id:
            provider = self.context.get_using_provider()
        else:
            provider = self.context.get_provider_by_id(image_caption_provider_id)
            if not provider:
                raise Exception(f"没有找到 ID 为 {image_caption_provider_id} 的提供商")
        if not isinstance(provider, Provider):
            raise Exception(f"提供商类型错误({type(provider)})，无法获取图片描述")
        response = await provider.text_chat(
            prompt=image_caption_prompt,
            session_id=uuid.uuid4().hex,
            image_urls=[image_url],
            persist=False,
        )
        return response.completion_text

    async def need_active_reply(self, event: AstrMessageEvent) -> bool:
        cfg = self.cfg(event)
        if not cfg["enable_active_reply"]:
            return False
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False

        if event.is_at_or_wake_command:
            # if the message is a command, let it pass
            return False

        if cfg["ar_whitelist"] and (
            event.unified_msg_origin not in cfg["ar_whitelist"]
            and (
                event.get_group_id() and event.get_group_id() not in cfg["ar_whitelist"]
            )
        ):
            return False

        match cfg["ar_method"]:
            case "possibility_reply":
                trig = random.random() < cfg["ar_possibility"]
                return trig

        return False

    async def handle_message(self, event: AstrMessageEvent):
        """仅支持群聊"""
        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            datetime_str = datetime.datetime.now().strftime("%H:%M:%S")

            parts = [f"[{event.message_obj.sender.nickname}/{datetime_str}]: "]

            cfg = self.cfg(event)
            at_self = False
            txt_acc = []

            for comp in event.get_messages():
                if isinstance(comp, Plain):
                    parts.append(f" {comp.text}")
                    txt_acc.append(comp.text)
                elif isinstance(comp, Image):
                    if cfg["image_caption"]:
                        try:
                            url = comp.url if comp.url else comp.file
                            if not url:
                                raise Exception("图片 URL 为空")
                            caption = await self.get_image_caption(
                                url,
                                cfg["image_caption_provider_id"],
                                cfg["image_caption_prompt"],
                            )
                            parts.append(f" [Image: {caption}]")
                            txt_acc.append(f"[Image: {caption}]")
                        except Exception as e:
                            logger.error(f"获取图片描述失败: {e}")
                            parts.append(" [Image]")
                            txt_acc.append("[Image]")
                    else:
                        parts.append(" [Image]")
                        txt_acc.append("[Image]")
                else:
                    try:
                        from astrbot.api.message_components import At

                        if isinstance(comp, At):
                            if str(getattr(comp, "qq", "")) == str(event.get_self_id()):
                                at_self = True
                    except Exception:
                        pass

            final_message = "".join(parts)
            logger.debug(f"ltm | {event.unified_msg_origin} | {final_message}")
            self.session_chats[event.unified_msg_origin].append(final_message)
            if len(self.session_chats[event.unified_msg_origin]) > cfg["max_cnt"]:
                self.session_chats[event.unified_msg_origin].pop(0)

            try:
                role = (
                    "assistant"
                    if event.get_sender_id() == event.get_self_id()
                    else "user"
                )
                entry = {
                    "id": event.message_obj.message_id,
                    "sender_id": event.get_sender_id(),
                    "sender_name": event.get_sender_name(),
                    "content": " ".join([t for t in txt_acc if t]),
                    "timestamp": event.message_obj.timestamp,
                    "role": role,
                    "at_self": at_self,
                }
                self.session_struct_chats[event.unified_msg_origin].append(entry)
                if (
                    len(self.session_struct_chats[event.unified_msg_origin])
                    > cfg["max_cnt"]
                ):
                    self.session_struct_chats[event.unified_msg_origin].pop(0)
            except Exception:
                pass

    async def on_req_llm(self, event: AstrMessageEvent, req: ProviderRequest):
        """当触发 LLM 请求前，调用此方法修改 req"""
        if event.unified_msg_origin not in self.session_chats:
            return

        cfg = self.cfg(event)
        chats_str = "\n---\n".join(self.session_chats[event.unified_msg_origin])

        def _pick_target():
            arr = self.session_struct_chats.get(event.unified_msg_origin, [])
            recent = arr[-5:] if len(arr) > 5 else arr[:]
            candidates = [i for i in recent if i.get("role") == "user"]
            if not candidates:
                return None
            pref = [i for i in candidates if i.get("at_self")]
            if pref:
                return pref[-1]
            pref = [i for i in candidates if "?" in (i.get("content") or "")]
            if pref:
                return pref[-1]
            return candidates[-1]

        if cfg["enable_active_reply"]:
            target = _pick_target()
            base_inst = (
                "You are a normal group member with memory."
                " Read the recent group chat and choose ONE message to reply."
                " DO NOT assume the user is attacking you; be neutral or witty."
                " Keep the reply concise and natural."
            )
            if target:
                req.prompt = (
                    f"{base_inst}\n\nRecent chats:\n{chats_str}\n\n"
                    f"Focus message(from {target.get('sender_name', 'user')}): \n{target.get('content', '')}\n"
                    f"Write a direct reply to the focus message only."
                )
                req.contexts = []
                try:
                    logger.info(
                        f"ltm | {event.unified_msg_origin} | active_reply_prompt: {req.prompt}"
                    )
                except Exception:
                    pass
                try:
                    event.set_extra(
                        "quote_target",
                        {
                            "id": target.get("id"),
                            "sender_id": target.get("sender_id"),
                            "sender_name": target.get("sender_name"),
                            "message_str": target.get("content"),
                        },
                    )
                except Exception:
                    pass
            else:
                prompt = req.prompt
                req.prompt = f"{base_inst}\n\nRecent chats:\n{chats_str}\n\nNew message: `{prompt}`\nRespond naturally."
                req.contexts = []
                try:
                    logger.info(
                        f"ltm | {event.unified_msg_origin} | active_reply_prompt: {req.prompt}"
                    )
                except Exception:
                    pass
        else:
            req.system_prompt += (
                "You are now in a chatroom. The chat history is as follows: \n"
            )
            req.system_prompt += chats_str

    async def after_req_llm(self, event: AstrMessageEvent):
        if event.unified_msg_origin not in self.session_chats:
            return

        if event.get_result() and event.get_result().is_llm_result():
            final_message = f"[You/{datetime.datetime.now().strftime('%H:%M:%S')}]: {event.get_result().get_plain_text()}"
            logger.debug(f"ltm | {event.unified_msg_origin} | {final_message}")
            self.session_chats[event.unified_msg_origin].append(final_message)
            cfg = self.cfg(event)
            if len(self.session_chats[event.unified_msg_origin]) > cfg["max_cnt"]:
                self.session_chats[event.unified_msg_origin].pop(0)
