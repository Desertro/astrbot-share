import asyncio
import json
import os
import sys
import time
from urllib.parse import urlparse
from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
try:
    from .src.media.manager import MediaManager
    from .src.service.llm_image_service import LLMImageService
except ImportError:
    _base_dir = os.path.dirname(__file__)
    if _base_dir and _base_dir not in sys.path:
        sys.path.insert(0, _base_dir)
    from src.media.manager import MediaManager
    from src.service.llm_image_service import LLMImageService

@register('vision_media', 'author', '视觉媒体服务：抽图、压缩与识图描述', '0.1.0', '')
class VisionMedia(Star):

    def __init__(self, context: Context, config: dict | None=None):
        super().__init__(context)
        self.config = config or {}
        self._maybe_migrate_from_mem_chat_config()
        self._shoubanhua_yezi_api_key = ''
        self._mem_chat_llm_api_key = ''
        vision_llm = self.config.get('vision_llm', {})
        if isinstance(vision_llm, dict):
            api_key_source = str(vision_llm.get('api_key_source') or 'local').strip().lower()
            if api_key_source == 'shoubanhua':
                self._shoubanhua_yezi_api_key = self._load_shoubanhua_yezi_api_key()
            elif api_key_source == 'mem_chat':
                self._mem_chat_llm_api_key = self._load_mem_chat_llm_api_key()
        self._db_path = os.path.join(os.path.dirname(__file__), 'vision_media.sqlite3')
        self._media_mgr = MediaManager(self.config, db_path=self._db_path, get_conf=self.config.get, resolve_vision_llm=self._resolve_vision_llm_config)
        self._llm_image_service = LLMImageService(self.config.get)
        self._essence_cleanup_task: asyncio.Task | None = None
        self._last_essence_cleanup_ts = 0.0

    def _maybe_migrate_from_mem_chat_config(self):
        try:
            data_path = get_astrbot_data_path()
            mem_chat_cfg_path = os.path.join(data_path, 'config', 'astrbot_plugin_mem_chat_config.json')
            if not os.path.exists(mem_chat_cfg_path):
                return
            with open(mem_chat_cfg_path, encoding='utf-8-sig') as f:
                mem_chat_cfg = json.loads(f.read() or '{}')
            if not isinstance(mem_chat_cfg, dict):
                return
            vision_llm = self.config.get('vision_llm', {}) if isinstance(self.config, dict) else {}
            vision_llm_empty = True
            if isinstance(vision_llm, dict):
                vision_llm_empty = not any([bool(vision_llm.get('enable')), bool(str(vision_llm.get('base_url') or '').strip()), bool(str(vision_llm.get('api_key') or '').strip()), bool(str(vision_llm.get('model') or '').strip()), bool(str(vision_llm.get('prompt') or '').strip())])
            defaults = {'chat_model_supports_vision': False, 'vision_image_cache_enable': True, 'vision_image_cache_ttl_days': 30, 'vision_image_cache_max_records': 20000, 'mem_chat_llm_image_max_kb': 2048, 'mem_chat_llm_image_max_dim': 4096, 'mem_chat_llm_image_quality_init': 90, 'mem_chat_llm_image_quality_min': 65, 'mem_chat_llm_gif_max_frames': 6, 'mem_chat_llm_gif_total_max_kb': 3072}
            changed = False
            if vision_llm_empty and isinstance(mem_chat_cfg.get('vision_llm'), dict):
                self.config['vision_llm'] = mem_chat_cfg.get('vision_llm', {})
                changed = True
            for k, default_v in defaults.items():
                if k in mem_chat_cfg:
                    cur_v = self.config.get(k, default_v) if isinstance(self.config, dict) else default_v
                    if cur_v == default_v:
                        try:
                            self.config[k] = mem_chat_cfg.get(k)
                            changed = True
                        except Exception:
                            pass
            if changed and hasattr(self.config, 'save_config'):
                try:
                    self.config.save_config()
                except Exception:
                    pass
        except Exception:
            return

    @staticmethod
    def _is_yezi_url(value: object) -> bool:
        try:
            parsed = urlparse(str(value or '').strip())
            return parsed.scheme == 'https' and parsed.hostname == 'api.yezi.help'
        except Exception:
            return False

    def _load_shoubanhua_yezi_api_key(self) -> str:
        """读取手办化插件的 Yezi key，但不把它写回 vision_media 配置文件。"""
        try:
            data_path = get_astrbot_data_path()
            cfg_path = os.path.join(data_path, 'config', 'astrbot_plugin_shoubanhua_config.json')
            if not os.path.exists(cfg_path):
                return ''
            with open(cfg_path, encoding='utf-8-sig') as f:
                cfg = json.loads(f.read() or '{}')
            if not isinstance(cfg, dict):
                return ''
            candidates = ((cfg.get('api_url'), cfg.get('api_keys')), (cfg.get('backup_api_url'), cfg.get('backup_api_keys')), (cfg.get('third_api_url'), cfg.get('third_api_keys')))
            for api_url, raw_keys in candidates:
                if not self._is_yezi_url(api_url):
                    continue
                if isinstance(raw_keys, str):
                    raw_keys = [raw_keys]
                if not isinstance(raw_keys, list):
                    continue
                for raw_key in raw_keys:
                    key = str(raw_key or '').strip()
                    if key:
                        logger.info('[vision_media] 已复用手办化插件的 Yezi API 凭据')
                        return key
        except Exception as e:
            logger.warning(f'[vision_media] 读取手办化插件 Yezi 凭据失败: {e}')
        return ''

    @staticmethod
    def _is_deepseek_url(value: object) -> bool:
        try:
            parsed = urlparse(str(value or '').strip())
            return parsed.scheme == 'https' and parsed.hostname == 'api.deepseek.com'
        except Exception:
            return False

    def _load_mem_chat_llm_api_key(self) -> str:
        """复用 mem_chat 的 DeepSeek key，但不把它复制到 vision_media 配置。"""
        try:
            data_path = get_astrbot_data_path()
            cfg_path = os.path.join(data_path, 'config', 'astrbot_plugin_mem_chat_config.json')
            if not os.path.exists(cfg_path):
                return ''
            with open(cfg_path, encoding='utf-8-sig') as f:
                cfg = json.loads(f.read() or '{}')
            if not isinstance(cfg, dict):
                return ''
            if not self._is_deepseek_url(cfg.get('llm_base_url')):
                logger.warning('[vision_media] mem_chat 的 llm_base_url 不是 DeepSeek 官方地址，已拒绝复用凭据')
                return ''
            key = str(cfg.get('llm_api_key') or '').strip()
            if key:
                logger.info('[vision_media] 已复用 mem_chat 的 DeepSeek API 凭据')
            return key
        except Exception as e:
            logger.warning(f'[vision_media] 读取 mem_chat DeepSeek 凭据失败: {e}')
            return ''

    def _resolve_vision_llm_config(self, raw_config: dict | None) -> dict:
        conf = dict(raw_config or {})
        source = str(conf.get('api_key_source') or 'local').strip().lower()
        if source == 'mem_chat':
            if not self._is_deepseek_url(conf.get('base_url')):
                conf['api_key'] = ''
                logger.warning(__import__('share_runtime').resolve('[vision_media] api_key_source=mem_chat 仅允许用于 {{AB_CMD_PROVIDER_SOURCES_2_API_BASE}}，已拒绝注入凭据'))
                return conf
            conf['api_key'] = self._mem_chat_llm_api_key
            if not conf['api_key']:
                logger.warning('[vision_media] 未在 mem_chat 配置中找到可用的 DeepSeek API 凭据')
            return conf
        if source != 'shoubanhua':
            return conf
        if not self._is_yezi_url(conf.get('base_url')):
            conf['api_key'] = ''
            logger.warning('[vision_media] api_key_source=shoubanhua 仅允许用于 https://api.yezi.help，已拒绝注入凭据')
            return conf
        conf['api_key'] = self._shoubanhua_yezi_api_key
        if not conf['api_key']:
            logger.warning('[vision_media] 未在手办化插件配置中找到可用的 Yezi API 凭据')
        return conf

    async def extract_images(self, event):
        return await self._media_mgr.extract_images_from_event(event)

    async def render_text_with_captions(self, event):
        return await self._media_mgr.get_text_with_images(event)

    async def prepare_llm_image_contents(self, image_urls: list[str]) -> list[dict]:
        return await self._llm_image_service.prepare_llm_image_contents(image_urls)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        await self._maybe_run_essence_cleanup(event)
        if not bool(self.config.get('auto_ingest_group_images', True)):
            return
        try:
            if not event.get_group_id():
                return
        except Exception:
            return
        try:
            imgs = await self._media_mgr.extract_images_from_event(event)
        except Exception:
            imgs = []
        if not imgs:
            return
        try:
            rendered = await self._media_mgr.get_text_with_images(event)
        except Exception:
            return
        if not bool(self.config.get('mirror_bot_image_msgs_to_mem_chat', True)):
            return
        try:
            self_id = str(event.get_self_id() or '').strip()
            sender_id = str(event.get_sender_id() or '').strip()
            if not self_id or not sender_id or self_id != sender_id:
                return
            gid = event.get_group_id() or event.get_sender_id()
            if not gid:
                return
            text = str(rendered or '').strip()
            if not text:
                return
            mem_chat = self.context.get_registered_star('mem_chat')
            db_mgr = getattr(mem_chat, 'db_mgr', None) if mem_chat else None
            if not db_mgr:
                return
            sender_name = ''
            try:
                sender_name = str(event.get_sender_name() or '').strip()
            except Exception:
                sender_name = ''
            sender_info = sender_name or 'Assistant'
            if sender_id:
                sender_info = f'{sender_info}/{sender_id}'
            await db_mgr.ensure_message_embedded(text, str(gid), sender_info)
        except Exception:
            return

    async def _maybe_run_essence_cleanup(self, event: AstrMessageEvent):
        try:
            if not self._media_mgr._vision_essence_cleanup_auto_enabled():
                return
            group_ids = sorted(self._media_mgr._vision_essence_cleanup_whitelist())
            if not group_ids:
                return
            bot = getattr(event, 'bot', None)
            if not bot:
                return
            self_id = int(str(event.get_self_id() or '').strip())
            if not self_id:
                return
            now = time.time()
            if self._essence_cleanup_task and (not self._essence_cleanup_task.done()):
                return
            interval = self._media_mgr._vision_essence_cleanup_interval_seconds()
            if now - self._last_essence_cleanup_ts < interval:
                return
            self._last_essence_cleanup_ts = now
            self._essence_cleanup_task = asyncio.create_task(self._run_essence_cleanup(bot=bot, self_id=self_id, group_ids=group_ids))
        except Exception:
            return

    async def _run_essence_cleanup(self, *, bot, self_id: int, group_ids: list[str]):
        try:
            cleaned = await self._media_mgr.cleanup_expired_essence_msgs_for_groups(bot=bot, self_id=self_id, group_ids=group_ids)
            for group_id, count in cleaned.items():
                logger.info('[vision_media] auto essence cleanup removed %s expired bot-owned essence msgs for group_id=%s', count, group_id)
        except Exception as e:
            logger.warning(f'[vision_media] auto essence cleanup failed: {e}')

    def close(self):
        try:
            if self._essence_cleanup_task and (not self._essence_cleanup_task.done()):
                self._essence_cleanup_task.cancel()
        except Exception:
            pass
        try:
            self._media_mgr.close()
        except Exception:
            pass
