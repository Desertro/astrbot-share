import asyncio
import hashlib
import io
import json
import math
import re
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from fractions import Fraction
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
import aiohttp
import cv2
import numpy as np
from PIL import Image as PILImage
from PIL import ImageOps
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
BSKY_API = 'https://public.api.bsky.app/xrpc/app.bsky.feed.getFeed'
WALLHAVEN_API = 'https://wallhaven.cc/api/v1/search'
PIXIV_RANKING_API = 'https://www.pixiv.net/ranking.php'
PIXIV_USER_API = 'https://www.pixiv.net/ajax/user/{user_id}'
PIXIV_SEARCH_API = 'https://www.pixiv.net/ajax/search/artworks/{tag}'
X_SYNDICATION_API = 'https://cdn.syndication.twimg.com/tweet-result'
X_FALLBACK_API = 'https://api.fxtwitter.com/status/{tweet_id}'
FXTWITTER_MEDIA_API = 'https://api.fxtwitter.com/2/profile/{handle}/media'
DEFAULT_BSKY_FEEDS = 'at://did:plc:tazrmeme4dzahimsykusrwrk/app.bsky.feed.generator/anime-en-hot'
DEFAULT_X_ACCOUNTS = 'anzai_teachco,imasogari8119,32asako,redolbus,MonsterOfSukeP,nodati53,mona_svpa,sugarwhite1046,Cafe_Raichu,nekoP_X_00,date_shinobu,kokuzou593,Hle_____,shohosandaisuki,MikaPikaZo,rurudo_,neco_person,shirabii,TorinoAqua,DSmile9'
DEFAULT_X_RSS_BASES = 'https://nitter.net,https://rss.xcancel.com'
PREFERRED_TOPICS: dict[str, tuple[float, tuple[str, ...]]] = {'蔚蓝档案': (3.0, ('ブルーアーカイブ', 'ブルアカ', 'blue archive', '蔚蓝档案', '碧蓝档案')), '碧蓝航线': (3.0, ('アズールレーン', 'アズレン', 'azur lane', '碧蓝航线', '碧藍航線')), 'BanG Dream': (3.0, ('ban g dream', 'bang dream', 'バンドリ', 'ガルパ', 'mygo', 'ave mujica', '少女乐队')), '东方 Project': (3.0, ('東方project', '東方 project', 'touhou', '东方project', '东方 project')), '影之诗': (3.0, ('shadowverse worlds beyond', 'shadowverse', 'シャドウバースworlds beyond', 'シャドウバースwb', 'シャドウバース', 'シャドバ', '影之诗', '影之詩')), '鸣潮': (2.0, ('鳴潮', '鸣潮', 'wuthering waves')), '原神': (2.0, ('原神', 'genshin')), '崩坏：星穹铁道': (2.0, ('崩壊スターレイル', '崩壊：スターレイル', '崩坏：星穹铁道', '崩坏星穹铁道')), '绝区零': (2.0, ('ゼンレスゾーンゼロ', '絶区零', '绝区零')), '明日方舟：终末地': (2.0, ('アークナイツエンドフィールド', 'エンドフィールド', '明日方舟：终末地', '终末地')), '明日方舟': (2.0, ('アークナイツ', '明日方舟', 'arknights'))}
PIXIV_TARGET_TAGS: tuple[tuple[str, str], ...] = (('ブルーアーカイブ', '蔚蓝档案'), ('アズールレーン', '碧蓝航线'), ('バンドリ', 'BanG Dream'), ('東方Project', '东方 Project'), ('シャドウバース', '影之诗'), ('鳴潮', '鸣潮'), ('原神', '原神'), ('崩壊スターレイル', '崩坏：星穹铁道'), ('ゼンレスゾーンゼロ', '绝区零'), ('アークナイツエンドフィールド', '明日方舟：终末地'), ('アークナイツ', '明日方舟'))
X_SYNDICATION_FEATURES = ';'.join(('tfw_timeline_list:', 'tfw_follower_count_sunset:true', 'tfw_tweet_edit_backend:on', 'tfw_refsrc_session:on', 'tfw_fosnr_soft_interventions_enabled:on', 'tfw_show_birdwatch_pivots_enabled:on', 'tfw_show_business_verified_badge:on', 'tfw_duplicate_scribes_to_settings:on', 'tfw_use_profile_image_shape_enabled:on', 'tfw_show_blue_verified_badge:on', 'tfw_legacy_timeline_sunset:true', 'tfw_show_gov_verified_badge:on', 'tfw_show_business_affiliate_badge:on', 'tfw_tweet_edit_frontend:on'))
BLOCKED_TERMS = {'adult', 'anus', 'bdsm', 'breast', 'breasts', 'buttocks', 'cameltoe', 'explicit', 'female genitalia', 'feet', 'genitals', 'gore', 'hentai', 'lingerie', 'naked', 'nipples', 'nsfl', 'nsfw', 'nude', 'nudity', 'panties', 'penis', 'porn', 'r18', 'r-18', 'sex', 'sexual', 'soles', 'swimsuit', 'toes', 'underwear', 'uncensored', 'vagina', 'r-18g', '18禁', '成人向け', 'エロ', 'おっぱい', '爆乳', 'ぱんつ', 'パンツ', '下着', 'バニー', 'ランジェリー', 'スク水', '競スク', '足裏', '足コキ', 'セックス', '触手', '裸体', '裸露', '乳首', '巨乳', '色情', '泳装', '水着'}
NON_ART_TERMS = {'air date', 'announcement', 'cast interview', 'episode preview', 'key visual', 'magazine', 'official visual', 'pages long', 'release date', 'releases on', 'staff interview', 'trailer', 'tv anime', 'will air', 'アニメ化', 'お知らせ', '発売', '予約', 'フィギュア', '商品', '漫画'}
BLOCKED_LABELS = {'!hide', 'graphic-media', 'nudity', 'porn', 'sexual'}
NUDENET_BLOCKED_CLASSES = {'ANUS_EXPOSED', 'BUTTOCKS_EXPOSED', 'FEMALE_BREAST_EXPOSED', 'FEMALE_GENITALIA_EXPOSED', 'MALE_GENITALIA_EXPOSED'}
POLISHED_ILLUSTRATION_CATEGORIES = {'polished_character_illustration', 'polished_scene_illustration'}
VISION_AUDIT_PROMPT = '你是严格的二次元成品插画编辑，只挑选值得作为壁纸或画师作品欣赏的高质量插画。\n只输出一行 JSON，不要 Markdown 或解释：\n{"category":"polished_character_illustration","quality_score":90,"anime_art":true,"safe":true,"finished":true,"text_heavy":false,"screenshot_or_promo":false,"comic_or_infographic":false,"ai_generated":false,"contains_male_character":false,"reason":"简短原因"}\n\ncategory 只能是以下之一：\n- polished_character_illustration：完成度高、以动漫角色为主体的成品插画\n- polished_scene_illustration：完成度高、有明确场景和构图的动漫成品插画\n- simple_or_low_detail_art：白底、低细节、简单平涂、随手涂鸦、草图、低完成度剧情小图\n- comic_or_infographic：漫画页、多格图、设定表、道具说明、图鉴、教程、信息图\n- screenshot_or_promo：动画/游戏截图、字幕图、海报、广告、宣传物料、商品图\n- meme_or_reaction：梗图、表情图、以笑点或大字文案为主的图片\n- non_anime_or_other：真人、风景、3D、摄影或其他不符合类型\n\nquality_score 使用严格的 0-100 标准：\n- 95-100：商业主视觉或顶级画师壁纸级，构图、光影、色彩、背景与细节近乎无可挑剔\n- 88-94：明显高于普通社交媒体配图的精美成品，有成熟构图、细腻光影和丰富细节\n- 80-87：完成度尚可，但属于简单平涂、普通立绘、背景较空或表现力一般的角色图\n- 60-79：虽是动漫绘画，但简单平涂、白底、背景空洞、细节少或像普通社交媒体小图\n- 0-59：截图、漫画、梗图、信息图、设定图、草稿或非插画\n\n硬性规则：\n1. 出现多个说明区块、道具配文字、对话框、字幕条、显眼大字时，必须分类为 comic_or_infographic、screenshot_or_promo 或 meme_or_reaction，text_heavy=true。\n2. 仅仅“画着动漫角色”不等于成品插画；简单白底角色互动图、低细节平涂图必须为 simple_or_low_detail_art，quality_score 不得超过 79。\n3. finished 只有在线稿、上色、构图和细节均达到可独立欣赏的成品水平时才为 true。\n4. 裸露、内衣泳装、性暗示、恋足/癖好、血腥猎奇均 safe=false。\n5. 明显 AI 生成图 ai_generated=true。宁可严格拒绝，不要为了凑数放宽。\n6. 单角色简单平涂、纯色/空白背景、缺少光影层次的作品即使线稿完整也不得超过 87；热门作品 IP 不能成为加分理由。\n7. 只要画面中出现任何可辨识的男性、男孩或男性拟人角色，无论是主体、配角、背景人物、倒影还是小窗插图，都必须 contains_male_character=true；男女同框也为 true。只有纯女性角色或完全无人形角色时才为 false，不能因为男性不是主体而忽略。\n8. quality_score 必须逐图独立判断，不要机械沿用 JSON 示例分数；符合 88-94 档定义的精美成品必须按实际完成度给分。\n'

@dataclass(slots=True)
class Candidate:
    item_key: str
    source: str
    image_url: str
    page_url: str
    created_at: datetime
    title: str
    author: str
    score: float
    stats: str
    related_authors: tuple[str, ...] = ()
    topic: str = ''
    topic_weight: float = 0.0

@dataclass(slots=True)
class XFeedItem:
    tweet_id: str
    author: str
    created_at: datetime
    title: str

@dataclass(slots=True)
class ImageFingerprint:
    sha256: str
    phash: str
    dhash: str
    crop_phashes: tuple[str, ...]

@register('astrbot_plugin_x_anime_push_test', 'Codex', '无账号二次元热门图片聚合推送测试', '0.8.3')
class AnimePushTestPlugin(Star):

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._push_task: asyncio.Task | None = None
        self._enabled = bool(self.config.get('enabled', False))
        self._target_umo = str(self.config.get('target_umo', __import__('share_runtime').resolve('default:GroupMessage:{{AB_QQ_8}}')))
        self._interval_seconds = max(60, int(self.config.get('interval_seconds', 180)))
        self._max_age_hours = max(1, int(self.config.get('max_age_hours', 48)))
        self._min_likes = max(0, int(self.config.get('min_likes', 2000)))
        self._min_width = max(320, int(self.config.get('min_width', 800)))
        self._min_height = max(320, int(self.config.get('min_height', 800)))
        self._max_download_mb = max(2, int(self.config.get('max_download_mb', 18)))
        self._duplicate_hash_distance = min(16, max(1, int(self.config.get('duplicate_hash_distance', 10))))
        self._require_nsfw_scan = bool(self.config.get('require_nsfw_scan', True))
        self._nsfw_threshold = min(0.95, max(0.05, float(self.config.get('nsfw_threshold', 0.22))))
        self._use_llm_vision_filter = bool(self.config.get('use_llm_vision_filter', True))
        configured_vision_providers = str(self.config.get('vision_provider_ids', 'deepseek_v4_flash,Google'))
        self._vision_provider_ids = [value.strip() for value in configured_vision_providers.split(',') if value.strip()]
        self._vision_min_quality_score = min(95, max(60, int(self.config.get('vision_min_quality_score', 88))))
        self._reject_male_characters = bool(self.config.get('reject_male_characters', True))
        self._vision_fallback_max_output_tokens = min(6000, max(800, int(self.config.get('vision_fallback_max_output_tokens', 3000))))
        self._max_candidates_per_cycle = max(1, int(self.config.get('max_candidates_per_cycle', 6)))
        self._candidate_refill_rounds = min(3, max(0, int(self.config.get('candidate_refill_rounds', 1))))
        self._send_start_notice = bool(self.config.get('send_start_notice', True))
        self._test_started_at = self._optional_config_datetime('test_started_at')
        self._test_ends_at = self._optional_config_datetime('test_ends_at')
        if self._test_started_at is not None and self._test_ends_at is not None and (self._test_ends_at <= self._test_started_at):
            logger.warning('[anime_public_push_test] ignoring invalid test window: end<=start')
            self._test_started_at = None
            self._test_ends_at = None
        configured_x_accounts = str(self.config.get('x_accounts', DEFAULT_X_ACCOUNTS))
        self._x_accounts = [value.strip().lstrip('@') for value in configured_x_accounts.split(',') if value.strip()]
        configured_x_rss_bases = str(self.config.get('x_rss_bases', DEFAULT_X_RSS_BASES))
        self._x_rss_bases = [value.strip().rstrip('/') for value in configured_x_rss_bases.split(',') if value.strip()]
        self._x_dynamic_artist_limit = max(10, int(self.config.get('x_dynamic_artist_limit', 60)))
        self._x_pixiv_ranking_pages = min(5, max(1, int(self.config.get('x_pixiv_ranking_pages', 3))))
        self._pixiv_target_users_per_tag = min(30, max(3, int(self.config.get('pixiv_target_users_per_tag', 12))))
        self._x_max_tweets_per_refresh = max(20, int(self.config.get('x_max_tweets_per_refresh', 120)))
        self._x_timeline_concurrency = min(24, max(2, int(self.config.get('x_timeline_concurrency', 10))))
        self._x_media_posts_per_author = min(50, max(5, int(self.config.get('x_media_posts_per_author', 20))))
        self._x_refresh_minutes = max(5, int(self.config.get('x_refresh_minutes', 15)))
        self._author_pool_enabled = bool(self.config.get('author_pool_enabled', True))
        self._author_pool_max_size = max(50, int(self.config.get('author_pool_max_size', 300)))
        self._author_pool_bootstrap_size = min(self._author_pool_max_size, max(20, int(self.config.get('author_pool_bootstrap_size', 80))))
        self._author_pool_refresh_limit = max(20, int(self.config.get('author_pool_refresh_limit', 80)))
        self._author_pool_new_per_refresh = max(1, int(self.config.get('author_pool_new_per_refresh', 12)))
        self._author_pool_exploration_ratio = min(0.5, max(0.05, float(self.config.get('author_pool_exploration_ratio', 0.25))))
        self._author_pause_after_rejects = max(2, int(self.config.get('author_pause_after_rejects', 3)))
        configured_feeds = str(self.config.get('bluesky_feed_uris', DEFAULT_BSKY_FEEDS))
        self._bluesky_feeds = [value.strip() for value in configured_feeds.split(',') if value.strip()]
        self._data_dir = Path(__import__('share_runtime').resolve('{{ASTRBOT_ROOT}}/data/plugin_data/astrbot_plugin_x_anime_push_test'))
        self._download_dir = self._data_dir / 'downloads'
        self._db_path = self._data_dir / 'push_state.sqlite3'
        self._detector: Any | None = None
        self._detector_failed = False
        self._dynamic_x_handles: list[str] = []
        self._dynamic_target_x_handles: dict[str, set[str]] = {}
        self._dynamic_x_handles_expires = datetime.min.replace(tzinfo=timezone.utc)
        self._x_candidate_cache: list[Candidate] = []
        self._x_candidate_cache_expires = datetime.min.replace(tzinfo=timezone.utc)
        self._x_candidate_cache_handles: set[str] = set()
        self._last_x_fetch_handles: set[str] = set()
        self._last_x_fetch_was_cache = False
        self._x_timeline_cooldown_until = datetime.min.replace(tzinfo=timezone.utc)

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        if not self._enabled or self._push_task is not None:
            return
        self._prepare_storage()
        self._push_task = asyncio.create_task(self._push_loop(), name='anime_public_push_test_loop')
        logger.info('[anime_public_push_test] started: target=%s interval=%ss age=%sh', self._target_umo, self._interval_seconds, self._max_age_hours)

    def _prepare_storage(self):
        self._download_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as db:
            db.execute("\n                CREATE TABLE IF NOT EXISTS processed_items (\n                    item_key TEXT PRIMARY KEY,\n                    source TEXT NOT NULL,\n                    status TEXT NOT NULL,\n                    reason TEXT NOT NULL DEFAULT '',\n                    processed_at TEXT NOT NULL\n                )\n                ")
            db.execute('\n                CREATE TABLE IF NOT EXISTS sent_images (\n                    sha256 TEXT PRIMARY KEY,\n                    phash TEXT NOT NULL,\n                    item_key TEXT NOT NULL,\n                    source TEXT NOT NULL,\n                    page_url TEXT NOT NULL,\n                    sent_at TEXT NOT NULL\n                )\n                ')
            db.execute('CREATE INDEX IF NOT EXISTS idx_sent_images_phash ON sent_images(phash)')
            sent_image_columns = {str(row[1]) for row in db.execute('PRAGMA table_info(sent_images)').fetchall()}
            if 'dhash' not in sent_image_columns:
                db.execute("ALTER TABLE sent_images ADD COLUMN dhash TEXT NOT NULL DEFAULT ''")
            if 'crop_phashes' not in sent_image_columns:
                db.execute("ALTER TABLE sent_images ADD COLUMN crop_phashes TEXT NOT NULL DEFAULT ''")
            db.execute("\n                CREATE TABLE IF NOT EXISTS x_author_pool (\n                    handle TEXT PRIMARY KEY COLLATE NOCASE,\n                    origin TEXT NOT NULL,\n                    discovered_by TEXT NOT NULL DEFAULT '',\n                    status TEXT NOT NULL DEFAULT 'probation',\n                    score REAL NOT NULL DEFAULT 30,\n                    recent_posts INTEGER NOT NULL DEFAULT 0,\n                    hot_posts INTEGER NOT NULL DEFAULT 0,\n                    vision_passes INTEGER NOT NULL DEFAULT 0,\n                    vision_rejects INTEGER NOT NULL DEFAULT 0,\n                    consecutive_rejects INTEGER NOT NULL DEFAULT 0,\n                    sent_posts INTEGER NOT NULL DEFAULT 0,\n                    last_discovered_at TEXT NOT NULL,\n                    last_checked_at TEXT,\n                    updated_at TEXT NOT NULL,\n                    created_at TEXT NOT NULL\n                )\n                ")
            db.execute('CREATE INDEX IF NOT EXISTS idx_x_author_pool_selection ON x_author_pool(status, score DESC, last_checked_at)')
            db.execute('\n                CREATE TABLE IF NOT EXISTS x_author_evidence (\n                    item_key TEXT NOT NULL,\n                    signal TEXT NOT NULL,\n                    handle TEXT NOT NULL,\n                    created_at TEXT NOT NULL,\n                    PRIMARY KEY (item_key, signal)\n                )\n                ')
            db.execute('\n                CREATE TABLE IF NOT EXISTS x_author_topics (\n                    handle TEXT NOT NULL COLLATE NOCASE,\n                    topic TEXT NOT NULL,\n                    weight REAL NOT NULL,\n                    last_seen_at TEXT NOT NULL,\n                    PRIMARY KEY (handle, topic)\n                )\n                ')
            db.execute('CREATE INDEX IF NOT EXISTS idx_x_author_topics_weight ON x_author_topics(weight DESC, handle)')
            db.execute('\n                CREATE TABLE IF NOT EXISTS test_runs (\n                    started_at TEXT PRIMARY KEY,\n                    ends_at TEXT NOT NULL,\n                    start_notice_at TEXT,\n                    completed_at TEXT,\n                    sent_count INTEGER NOT NULL DEFAULT 0,\n                    rejected_count INTEGER NOT NULL DEFAULT 0,\n                    duplicate_count INTEGER NOT NULL DEFAULT 0\n                )\n                ')
            if self._test_started_at is not None and self._test_ends_at is not None:
                db.execute('\n                    INSERT INTO test_runs (started_at, ends_at)\n                    VALUES (?, ?)\n                    ON CONFLICT(started_at) DO UPDATE SET\n                        ends_at = excluded.ends_at\n                    ', (self._test_started_at.isoformat(), self._test_ends_at.isoformat()))
            db.execute("DELETE FROM processed_items WHERE status = 'rejected' AND reason IN ('vision filter failed', 'vision provider unavailable')")
            now = datetime.now(timezone.utc).isoformat()
            for handle in self._x_accounts:
                normalized = self._normalize_x_handle(handle)
                if not normalized:
                    continue
                db.execute("\n                    INSERT INTO x_author_pool\n                        (handle, origin, status, score, last_discovered_at,\n                         updated_at, created_at)\n                    VALUES (?, 'seed', 'active', 60, ?, ?, ?)\n                    ON CONFLICT(handle) DO UPDATE SET\n                        origin = CASE\n                            WHEN x_author_pool.origin = 'seed' THEN 'seed'\n                            ELSE x_author_pool.origin\n                        END,\n                        last_discovered_at = excluded.last_discovered_at\n                    ", (normalized, now, now, now))
            strict_start_row = db.execute("\n                SELECT MIN(processed_at) FROM processed_items\n                WHERE status = 'rejected'\n                  AND reason LIKE 'vision:%quality=%'\n                ").fetchone()
            strict_start = str(strict_start_row[0] or '') if strict_start_row else ''
            if strict_start:
                verified_pages = db.execute("\n                    SELECT page_url FROM sent_images\n                    WHERE source = 'X' AND sent_at >= ?\n                    ", (strict_start,)).fetchall()
                for page_url, in verified_pages:
                    match = re.search('x\\.com/([A-Za-z0-9_]{1,15})/status/', page_url)
                    handle = self._normalize_x_handle(match.group(1) if match else '')
                    if not handle:
                        continue
                    db.execute("\n                        INSERT INTO x_author_pool\n                            (handle, origin, status, score, vision_passes,\n                             sent_posts, last_discovered_at, updated_at, created_at)\n                        VALUES (?, 'verified_history', 'active', 73, 1, 1, ?, ?, ?)\n                        ON CONFLICT(handle) DO UPDATE SET\n                            status = 'active',\n                            score = MAX(x_author_pool.score, 73),\n                            vision_passes = MAX(x_author_pool.vision_passes, 1),\n                            sent_posts = MAX(x_author_pool.sent_posts, 1),\n                            updated_at = excluded.updated_at\n                        ", (handle, now, now, now))
            db.commit()

    async def _push_loop(self):
        now = datetime.now(timezone.utc)
        if self._test_started_at is not None and now < self._test_started_at:
            await asyncio.sleep((self._test_started_at - now).total_seconds())
        if self._test_ends_at is not None and datetime.now(timezone.utc) >= self._test_ends_at:
            await self._finish_test_window()
            self._enabled = False
            return
        if self._send_start_notice and (not self._test_start_notice_sent()):
            window_line = ''
            duration_label = self._test_duration_label()
            if self._test_started_at is not None and self._test_ends_at is not None:
                local_tz = timezone(timedelta(hours=8))
                start_text = self._test_started_at.astimezone(local_tz).strftime('%m-%d %H:%M')
                end_text = self._test_ends_at.astimezone(local_tz).strftime('%m-%d %H:%M')
                window_line = f'测试时间：{start_text} 至 {end_text}（{duration_label}）\n'
            notice_title = f'[{duration_label}二次元热门图测试已启动]\n' if duration_label else '[二次元热门图测试已启动]\n'
            notice = MessageChain([Plain(notice_title + window_line + f'每 {self._interval_seconds} 秒检查一次；只推最近 {self._max_age_hours} 小时且达到 {self._min_likes}❤/收藏的图片。\n没有达标新图时，本轮静默跳过。')])
            await asyncio.sleep(3)
            for attempt in range(1, 6):
                try:
                    await self.context.send_message(self._target_umo, notice)
                    logger.info('[anime_public_push_test] start notice sent to %s on attempt=%s', self._target_umo, attempt)
                    self._mark_test_start_notice_sent()
                    break
                except Exception as exc:
                    if attempt >= 5:
                        logger.exception('[anime_public_push_test] start notice failed after retries')
                        break
                    logger.warning('[anime_public_push_test] start notice attempt=%s failed: %s; retrying', attempt, type(exc).__name__)
                    await asyncio.sleep(3)
        while self._enabled:
            if self._test_ends_at is not None and datetime.now(timezone.utc) >= self._test_ends_at:
                await self._finish_test_window()
                self._enabled = False
                break
            try:
                await self._push_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('[anime_public_push_test] push cycle failed')
            sleep_seconds = float(self._interval_seconds)
            if self._test_ends_at is not None:
                remaining = (self._test_ends_at - datetime.now(timezone.utc)).total_seconds()
                sleep_seconds = min(sleep_seconds, max(0.0, remaining))
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)

    def _test_start_notice_sent(self) -> bool:
        if self._test_started_at is None or not self._db_path.exists():
            return False
        with sqlite3.connect(self._db_path) as db:
            row = db.execute('SELECT start_notice_at FROM test_runs WHERE started_at = ?', (self._test_started_at.isoformat(),)).fetchone()
        return bool(row and row[0])

    def _mark_test_start_notice_sent(self) -> None:
        if self._test_started_at is None:
            return
        with sqlite3.connect(self._db_path) as db:
            db.execute('UPDATE test_runs SET start_notice_at = ? WHERE started_at = ?', (datetime.now(timezone.utc).isoformat(), self._test_started_at.isoformat()))
            db.commit()

    async def _finish_test_window(self) -> None:
        if self._test_started_at is None or self._test_ends_at is None:
            return
        start_iso = self._test_started_at.isoformat()
        end_iso = self._test_ends_at.isoformat()
        with sqlite3.connect(self._db_path) as db:
            row = db.execute('SELECT completed_at FROM test_runs WHERE started_at = ?', (start_iso,)).fetchone()
            if row and row[0]:
                return
            sent_count = int(db.execute('SELECT COUNT(*) FROM sent_images WHERE sent_at >= ? AND sent_at <= ?', (start_iso, end_iso)).fetchone()[0])
            status_counts = dict(db.execute('\n                    SELECT status, COUNT(*) FROM processed_items\n                    WHERE processed_at >= ? AND processed_at <= ?\n                    GROUP BY status\n                    ', (start_iso, end_iso)).fetchall())
        rejected_count = int(status_counts.get('rejected', 0))
        duplicate_count = int(status_counts.get('duplicate', 0))
        message = MessageChain([Plain(f'[{self._test_duration_label()}二次元热门图测试已结束]\n合格推送：{sent_count} 张\n质量/安全审核未通过：{rejected_count} 张\n重复图片拦截：{duplicate_count} 张\n测试推送现已自动停止。')])
        try:
            await self.context.send_message(self._target_umo, message)
        except Exception:
            logger.exception('[anime_public_push_test] finish notice failed')
        completed_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._db_path) as db:
            db.execute('\n                UPDATE test_runs\n                SET completed_at = ?, sent_count = ?, rejected_count = ?,\n                    duplicate_count = ?\n                WHERE started_at = ?\n                ', (completed_at, sent_count, rejected_count, duplicate_count, start_iso))
            db.commit()
        logger.info('[anime_public_push_test] test completed: sent=%s rejected=%s duplicate=%s', sent_count, rejected_count, duplicate_count)

    async def _push_once(self) -> bool:
        timeout = aiohttp.ClientTimeout(total=35, connect=12)
        headers = {'Accept': 'application/json,image/avif,image/webp,image/*,*/*;q=0.8', 'Accept-Language': 'en,ja', 'User-Agent': 'AstrBot-AnimePublicPushTest/0.3'}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers, trust_env=True) as session:
            used_x_handles: set[str] = set()
            attempted_keys: set[str] = set()
            total_candidates = 0
            total_unseen = 0
            examined = 0
            batches_checked = 0
            initial_cache_hit = False
            for batch_index in range(self._candidate_refill_rounds + 1):
                batches_checked += 1
                if batch_index == 0:
                    source_results = await asyncio.gather(self._fetch_x_public(session), self._fetch_bluesky(session), self._fetch_wallhaven(session), return_exceptions=True)
                    source_names = ('X', 'Bluesky', 'Wallhaven')
                    initial_cache_hit = self._last_x_fetch_was_cache
                else:
                    refill_exclusions = set() if initial_cache_hit else used_x_handles
                    source_results = await asyncio.gather(self._fetch_x_public(session, force_refresh=True, excluded_handles=refill_exclusions), return_exceptions=True)
                    source_names = ('X refill',)
                used_x_handles.update(self._last_x_fetch_handles)
                candidates: list[Candidate] = []
                for source, result in zip(source_names, source_results):
                    if isinstance(result, Exception):
                        logger.warning('[anime_public_push_test] %s fetch failed: %s', source, result)
                        continue
                    candidates.extend(result)
                candidates = self._rank_candidates(candidates)
                total_candidates += len(candidates)
                unseen_candidates = [candidate for candidate in candidates if candidate.item_key not in attempted_keys and (not self._is_processed(candidate.item_key))]
                total_unseen += len(unseen_candidates)
                for candidate in unseen_candidates:
                    if examined >= self._max_candidates_per_cycle:
                        break
                    attempted_keys.add(candidate.item_key)
                    examined += 1
                    try:
                        prepared = await self._download_and_prepare(session, candidate)
                    except Exception as exc:
                        logger.warning('[anime_public_push_test] image rejected %s: %s', candidate.item_key, exc)
                        self._mark_processed(candidate, 'rejected', str(exc)[:240])
                        continue
                    if prepared is None:
                        continue
                    image_path, fingerprint = prepared
                    try:
                        duplicate_reason = self._duplicate_image_reason(fingerprint)
                        if duplicate_reason:
                            logger.info('[anime_public_push_test] duplicate blocked %s: %s', candidate.item_key, duplicate_reason)
                            self._mark_processed(candidate, 'duplicate', duplicate_reason)
                            continue
                        safe, reason = await self._passes_nsfw_scan(image_path)
                        if not safe:
                            self._record_author_signal(candidate, 'unsafe', reason)
                            self._mark_processed(candidate, 'rejected', reason)
                            continue
                        safe, reason = await self._passes_llm_vision_filter(image_path)
                        if not safe:
                            if not reason.startswith('vision unavailable'):
                                self._record_author_signal(candidate, 'vision_reject', reason)
                                self._mark_processed(candidate, 'rejected', reason)
                            continue
                        self._record_author_signal(candidate, 'vision_pass', reason)
                        sent = await self._send_candidate_with_retry(candidate, image_path)
                        if not sent:
                            continue
                        self._mark_sent(candidate, fingerprint)
                        self._record_author_signal(candidate, 'sent')
                        logger.info('[anime_public_push_test] pushed %s from %s to %s after batches=%s examined=%s', candidate.item_key, candidate.source, self._target_umo, batches_checked, examined)
                        return True
                    finally:
                        image_path.unlink(missing_ok=True)
                if examined >= self._max_candidates_per_cycle:
                    break
                if batch_index < self._candidate_refill_rounds:
                    logger.info('[anime_public_push_test] no qualified image in batch %s; refilling from unchecked authors (used=%s examined=%s)', batches_checked, len(used_x_handles), examined)
            logger.info('[anime_public_push_test] cycle produced no send after refill: batches=%s candidates=%s unseen=%s examined=%s', batches_checked, total_candidates, total_unseen, examined)
            return False

    async def _send_candidate_with_retry(self, candidate: Candidate, image_path: Path) -> bool:
        for attempt in range(1, 4):
            try:
                await self.context.send_message(self._target_umo, self._build_message(candidate, image_path))
                return True
            except Exception as exc:
                if attempt >= 3:
                    logger.error('[anime_public_push_test] send failed after %s attempts for %s: %s', attempt, candidate.item_key, exc)
                    return False
                logger.warning('[anime_public_push_test] send attempt %s failed for %s; retrying: %s', attempt, candidate.item_key, exc)
                await asyncio.sleep(attempt * 2)
        return False

    @staticmethod
    def _normalize_x_handle(value: str) -> str:
        handle = str(value or '').strip().lstrip('@').lower()
        return handle if re.fullmatch('[a-z0-9_]{1,15}', handle) else ''

    @staticmethod
    def _detect_preferred_topic(text: str) -> tuple[str, float]:
        normalized = re.sub('[_#\\-:：!！]+', ' ', str(text or '').lower())
        matches: list[tuple[float, int, str]] = []
        for topic, (weight, keywords) in PREFERRED_TOPICS.items():
            for keyword in keywords:
                normalized_keyword = re.sub('[_#\\-:：!！]+', ' ', keyword.lower())
                if normalized_keyword in normalized:
                    matches.append((weight, len(normalized_keyword), topic))
                    break
        if not matches:
            return ('', 0.0)
        weight, _, topic = max(matches)
        return (topic, float(weight))

    def _record_author_topics(self, handle_topics: dict[str, set[str]]) -> None:
        if not handle_topics:
            return
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._db_path) as db:
            for raw_handle, topics in handle_topics.items():
                handle = self._normalize_x_handle(raw_handle)
                if not handle:
                    continue
                for topic in topics:
                    definition = PREFERRED_TOPICS.get(topic)
                    if not definition:
                        continue
                    db.execute('\n                        INSERT INTO x_author_topics\n                            (handle, topic, weight, last_seen_at)\n                        VALUES (?, ?, ?, ?)\n                        ON CONFLICT(handle, topic) DO UPDATE SET\n                            weight = MAX(x_author_topics.weight, excluded.weight),\n                            last_seen_at = excluded.last_seen_at\n                        ', (handle, topic, float(definition[0]), now))
            db.commit()

    def _author_topic_affinity(self, candidate: Candidate) -> tuple[str, float]:
        if candidate.topic:
            return (candidate.topic, candidate.topic_weight)
        if candidate.source != 'X' or not self._author_pool_enabled:
            return ('', 0.0)
        handle = self._normalize_x_handle(candidate.author)
        with sqlite3.connect(self._db_path) as db:
            row = db.execute('\n                SELECT topic, weight FROM x_author_topics\n                WHERE handle = ?\n                ORDER BY weight DESC, last_seen_at DESC\n                LIMIT 1\n                ', (handle,)).fetchone()
        return (str(row[0]), float(row[1])) if row else ('', 0.0)

    def _upsert_discovered_authors(self, handles: list[str] | tuple[str, ...], *, origin: str, discovered_by: str='', max_new: int | None=None) -> int:
        if not self._author_pool_enabled:
            return 0
        normalized = list(dict.fromkeys((handle for value in handles if (handle := self._normalize_x_handle(value)))))
        if not normalized:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        initial_score = {'seed': 60.0, 'pixiv_target': 50.0, 'pixiv': 40.0, 'related': 25.0}.get(origin, 30.0)
        added = 0
        with sqlite3.connect(self._db_path) as db:
            existing = {str(row[0]).lower() for row in db.execute(f'SELECT handle FROM x_author_pool WHERE handle IN ({','.join(('?' for _ in normalized))})', normalized).fetchall()}
            for handle in normalized:
                is_new = handle not in existing
                if is_new and max_new is not None and (added >= max_new):
                    continue
                status = 'active' if origin == 'seed' else 'probation'
                db.execute("\n                    INSERT INTO x_author_pool\n                        (handle, origin, discovered_by, status, score,\n                         last_discovered_at, updated_at, created_at)\n                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n                    ON CONFLICT(handle) DO UPDATE SET\n                        origin = CASE\n                            WHEN x_author_pool.origin = 'seed' THEN 'seed'\n                            ELSE x_author_pool.origin\n                        END,\n                        discovered_by = CASE\n                            WHEN x_author_pool.discovered_by = ''\n                            THEN excluded.discovered_by\n                            ELSE x_author_pool.discovered_by\n                        END,\n                        last_discovered_at = excluded.last_discovered_at\n                    ", (handle, origin, self._normalize_x_handle(discovered_by), status, initial_score, now, now, now))
                if is_new:
                    added += 1
                    existing.add(handle)
            db.commit()
        self._prune_author_pool()
        return added

    def _prune_author_pool(self) -> None:
        with sqlite3.connect(self._db_path) as db:
            row = db.execute('SELECT COUNT(*) FROM x_author_pool').fetchone()
            excess = max(0, int(row[0] if row else 0) - self._author_pool_max_size)
            if not excess:
                return
            removable = db.execute("\n                SELECT handle FROM x_author_pool\n                WHERE origin != 'seed' AND vision_passes = 0 AND sent_posts = 0\n                ORDER BY score ASC, updated_at ASC\n                LIMIT ?\n                ", (excess,)).fetchall()
            db.executemany('DELETE FROM x_author_pool WHERE handle = ?', removable)
            db.commit()

    def _select_author_handles(self, excluded_handles: set[str] | None=None) -> list[str]:
        excluded = {handle for value in excluded_handles or set() if (handle := self._normalize_x_handle(value))}
        if not self._author_pool_enabled:
            return [handle for handle in dict.fromkeys(self._x_accounts + self._dynamic_x_handles) if self._normalize_x_handle(handle) not in excluded]
        now = datetime.now(timezone.utc)
        resume_before = (now - timedelta(days=7)).isoformat()
        with sqlite3.connect(self._db_path) as db:
            db.execute("\n                UPDATE x_author_pool\n                SET status = 'probation', consecutive_rejects = 0, updated_at = ?\n                WHERE status = 'paused' AND updated_at < ?\n                ", (now.isoformat(), resume_before))
            rows = db.execute("\n                SELECT p.handle,\n                       p.score + COALESCE(MAX(t.weight), 0) * 8 AS selection_score,\n                       p.status, p.last_checked_at\n                FROM x_author_pool p\n                LEFT JOIN x_author_topics t ON t.handle = p.handle\n                WHERE p.status IN ('active', 'probation')\n                GROUP BY p.handle, p.score, p.status, p.last_checked_at,\n                         p.vision_passes, p.hot_posts\n                ORDER BY selection_score DESC,\n                         p.vision_passes DESC, p.hot_posts DESC\n                ").fetchall()
            db.commit()
        if excluded:
            rows = [row for row in rows if self._normalize_x_handle(str(row[0])) not in excluded]
        if not rows:
            return [] if excluded else list(self._x_accounts)
        limit = min(self._author_pool_refresh_limit, len(rows))
        explore_slots = min(limit, max(1, int(round(limit * self._author_pool_exploration_ratio))))
        exploit_slots = max(0, limit - explore_slots)
        selected = list(rows[:exploit_slots])
        selected_handles = {str(row[0]) for row in selected}
        exploration = sorted((row for row in rows if str(row[0]) not in selected_handles), key=lambda row: (row[3] is not None, str(row[3] or ''), -float(row[1] or 0)))[:explore_slots]
        selected.extend(exploration)
        return [str(row[0]) for row in selected]

    def _record_author_feed_check(self, handle: str, *, recent_posts: int, failed: bool=False) -> None:
        normalized = self._normalize_x_handle(handle)
        if not normalized or not self._author_pool_enabled:
            return
        now = datetime.now(timezone.utc).isoformat()
        bonus = 0.0 if failed else min(1.0, max(0, recent_posts) * 0.15)
        with sqlite3.connect(self._db_path) as db:
            db.execute('\n                UPDATE x_author_pool\n                SET recent_posts = recent_posts + ?,\n                    score = MIN(100, score + ?),\n                    last_checked_at = ?, updated_at = ?\n                WHERE handle = ?\n                ', (max(0, recent_posts), bonus, now, now, normalized))
            db.commit()

    def _record_author_signal(self, candidate: Candidate, signal: str, reason: str='') -> None:
        if candidate.source != 'X' or not self._author_pool_enabled:
            return
        handle = self._normalize_x_handle(candidate.author)
        if not handle:
            return
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._db_path) as db:
            inserted = db.execute('\n                INSERT OR IGNORE INTO x_author_evidence\n                    (item_key, signal, handle, created_at)\n                VALUES (?, ?, ?, ?)\n                ', (candidate.item_key, signal, handle, now)).rowcount
            if not inserted:
                return
            if signal == 'hot':
                db.execute('\n                    UPDATE x_author_pool\n                    SET hot_posts = hot_posts + 1,\n                        score = MIN(100, score + 2), updated_at = ?\n                    WHERE handle = ?\n                    ', (now, handle))
            elif signal == 'vision_pass':
                db.execute("\n                    UPDATE x_author_pool\n                    SET vision_passes = vision_passes + 1,\n                        consecutive_rejects = 0, status = 'active',\n                        score = MIN(100, score + 10), updated_at = ?\n                    WHERE handle = ?\n                    ", (now, handle))
            elif signal in {'vision_reject', 'unsafe'}:
                penalty = 8.0 if signal == 'unsafe' else 3.0
                row = db.execute('\n                    SELECT vision_passes, consecutive_rejects\n                    FROM x_author_pool WHERE handle = ?\n                    ', (handle,)).fetchone()
                passes = int(row[0] if row else 0)
                rejects = int(row[1] if row else 0) + 1
                pause_at = self._author_pause_after_rejects + (2 if passes else 0)
                status = 'paused' if rejects >= pause_at else None
                db.execute('\n                    UPDATE x_author_pool\n                    SET vision_rejects = vision_rejects + 1,\n                        consecutive_rejects = ?, score = MAX(0, score - ?),\n                        status = COALESCE(?, status), updated_at = ?\n                    WHERE handle = ?\n                    ', (rejects, penalty, status, now, handle))
            elif signal == 'sent':
                db.execute('\n                    UPDATE x_author_pool\n                    SET sent_posts = sent_posts + 1,\n                        score = MIN(100, score + 3), updated_at = ?\n                    WHERE handle = ?\n                    ', (now, handle))
            db.commit()
        if signal == 'vision_pass' and candidate.related_authors:
            self._upsert_discovered_authors(candidate.related_authors, origin='related', discovered_by=handle, max_new=3)

    def _author_pool_score(self, candidate: Candidate) -> float:
        if candidate.source != 'X' or not self._author_pool_enabled:
            return 0.0
        handle = self._normalize_x_handle(candidate.author)
        with sqlite3.connect(self._db_path) as db:
            row = db.execute('SELECT score FROM x_author_pool WHERE handle = ?', (handle,)).fetchone()
        return float(row[0] if row else 0.0)

    def _author_pool_summary(self) -> tuple[int, int, int, list[tuple]]:
        with sqlite3.connect(self._db_path) as db:
            counts = dict(db.execute('SELECT status, COUNT(*) FROM x_author_pool GROUP BY status').fetchall())
            top = db.execute('\n                SELECT handle, ROUND(score, 1), vision_passes, sent_posts\n                FROM x_author_pool\n                ORDER BY score DESC, vision_passes DESC, sent_posts DESC\n                LIMIT 8\n                ').fetchall()
        return (int(counts.get('active', 0)), int(counts.get('probation', 0)), int(counts.get('paused', 0)), top)

    def _author_pool_size(self) -> int:
        with sqlite3.connect(self._db_path) as db:
            row = db.execute('SELECT COUNT(*) FROM x_author_pool').fetchone()
        return int(row[0] if row else 0)

    def _author_pool_add_limit(self) -> int:
        bootstrap_remaining = max(0, self._author_pool_bootstrap_size - self._author_pool_size())
        return max(self._author_pool_new_per_refresh, bootstrap_remaining)

    def _pool_cached_discovered_authors(self) -> int:
        add_limit = self._author_pool_add_limit()
        target_handles = list(self._dynamic_target_x_handles)
        added_target = self._upsert_discovered_authors(target_handles, origin='pixiv_target', max_new=add_limit)
        remaining = max(0, add_limit - added_target)
        added_general = self._upsert_discovered_authors(self._dynamic_x_handles, origin='pixiv', max_new=remaining)
        self._record_author_topics(self._dynamic_target_x_handles)
        return added_target + added_general

    async def _fetch_x_public(self, session: aiohttp.ClientSession, *, force_refresh: bool=False, excluded_handles: set[str] | None=None) -> list[Candidate]:
        now = datetime.now(timezone.utc)
        if not force_refresh and (not excluded_handles) and self._x_candidate_cache and (now < self._x_candidate_cache_expires):
            self._last_x_fetch_was_cache = True
            self._last_x_fetch_handles = set(self._x_candidate_cache_handles)
            return [candidate for candidate in self._x_candidate_cache if self._is_fresh(candidate.created_at)]
        if now < self._x_timeline_cooldown_until:
            logger.info('[anime_public_push_test] X media timeline cooldown active until %s', self._x_timeline_cooldown_until.isoformat())
            return [candidate for candidate in self._x_candidate_cache if self._is_fresh(candidate.created_at)]
        self._last_x_fetch_was_cache = False
        handles = await self._discover_x_handles(session, excluded_handles=excluded_handles)
        self._last_x_fetch_handles = {handle for value in handles if (handle := self._normalize_x_handle(value))}
        if not handles:
            return []
        semaphore = asyncio.Semaphore(self._x_timeline_concurrency)

        async def fetch_timeline(handle: str):
            async with semaphore:
                return await self._fetch_x_media_timeline(session, handle)
        timeline_results = await asyncio.gather(*(fetch_timeline(handle) for handle in handles), return_exceptions=True)
        candidate_map: dict[str, Candidate] = {}
        failed_timelines = 0
        recent_media_posts = 0
        for handle, result in zip(handles, timeline_results):
            if isinstance(result, Exception):
                failed_timelines += 1
                self._record_author_feed_check(handle, recent_posts=0, failed=True)
                continue
            recent_posts, timeline_candidates = result
            recent_media_posts += recent_posts
            self._record_author_feed_check(handle, recent_posts=recent_posts, failed=False)
            for candidate in timeline_candidates:
                previous = candidate_map.get(candidate.item_key)
                if previous is None or candidate.score > previous.score:
                    candidate_map[candidate.item_key] = candidate
        candidates = sorted(candidate_map.values(), key=lambda candidate: (candidate.topic_weight, candidate.score), reverse=True)[:self._x_max_tweets_per_refresh]
        preferred_candidates = sum((1 for candidate in candidates if self._author_topic_affinity(candidate)[1] > 0))
        if force_refresh:
            merged = {candidate.item_key: candidate for candidate in self._x_candidate_cache + candidates}
            self._x_candidate_cache = list(merged.values())
            self._x_candidate_cache_handles.update(self._last_x_fetch_handles)
        else:
            self._x_candidate_cache = candidates
            self._x_candidate_cache_handles = set(self._last_x_fetch_handles)
        self._x_candidate_cache_expires = now + timedelta(minutes=self._x_refresh_minutes)
        failure_ratio = failed_timelines / max(1, len(handles))
        if len(handles) >= 10 and failure_ratio >= 0.75:
            self._x_timeline_cooldown_until = now + timedelta(minutes=10)
            logger.warning('[anime_public_push_test] FxTwitter timeline circuit opened: failures=%s/%s cooldown=10m', failed_timelines, len(handles))
        else:
            self._x_timeline_cooldown_until = datetime.min.replace(tzinfo=timezone.utc)
        logger.info('[anime_public_push_test] X media refresh: handles=%s timeline_failures=%s recent_media=%s likes>=%s=%s preferred=%s', len(handles), failed_timelines, recent_media_posts, self._min_likes, len(candidates), preferred_candidates)
        return candidates

    async def _fetch_x_media_timeline(self, session: aiohttp.ClientSession, handle: str) -> tuple[int, list[Candidate]]:
        async with session.get(FXTWITTER_MEDIA_API.format(handle=handle), params={'count': str(self._x_media_posts_per_author)}, headers={'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}) as response:
            if response.status == 204:
                return (0, [])
            response.raise_for_status()
            payload = await response.json(content_type=None)
        results = list(payload.get('results') or [])
        candidates: list[Candidate] = []
        recent_posts = 0
        for post in results:
            if not isinstance(post, dict) or post.get('type') != 'status':
                continue
            try:
                timestamp = float(post.get('created_timestamp') or 0)
                if timestamp > 0:
                    created_at = datetime.fromtimestamp(timestamp, timezone.utc)
                else:
                    created_at = parsedate_to_datetime(str(post.get('created_at') or '')).astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
            if not self._is_fresh(created_at):
                continue
            recent_posts += 1
            media = post.get('media') or {}
            photos = list(media.get('photos') or [])
            likes = int(post.get('likes') or 0)
            reposts = int(post.get('reposts') or 0)
            if post.get('possibly_sensitive') is True or not photos:
                continue
            if likes < self._min_likes:
                continue
            text = str(post.get('text') or '')
            alt = str(photos[0].get('altText') or '')
            if self._contains_blocked_term(f'{text} {alt}'):
                continue
            if self._contains_non_art_term(f'{text} {alt}'):
                continue
            image_url = str(photos[0].get('url') or '')
            if not image_url.startswith('https://pbs.twimg.com/media/'):
                continue
            author_data = post.get('author') or {}
            author = self._normalize_x_handle(str(author_data.get('screen_name') or handle))
            tweet_id = str(post.get('id') or '')
            if not author or not tweet_id.isdigit():
                continue
            age_hours = self._age_hours(created_at)
            score = math.log1p(likes + reposts * 2.0) / (age_hours + 2.0) ** 0.55
            topic, topic_weight = self._detect_preferred_topic(f'{text} {alt}')
            related_authors = tuple((related for value in re.findall('(?<![A-Za-z0-9_])@([A-Za-z0-9_]{1,15})', text) if (related := self._normalize_x_handle(value)) and related != author))[:4]
            candidate = Candidate(item_key=f'x:{tweet_id}', source='X', image_url=image_url, page_url=str(post.get('url') or f'https://x.com/{author}/status/{tweet_id}'), created_at=created_at, title=self._clean_title(text or '二次元插画'), author=f'@{author}', score=score, stats=f'❤ {likes}' + (f'  ↻ {reposts}' if reposts else ''), related_authors=tuple(dict.fromkeys(related_authors)), topic=topic, topic_weight=topic_weight)
            self._record_author_signal(candidate, 'hot')
            candidates.append(candidate)
        return (recent_posts, candidates)

    async def _discover_x_handles(self, session: aiohttp.ClientSession, *, excluded_handles: set[str] | None=None) -> list[str]:
        now = datetime.now(timezone.utc)
        if now < self._dynamic_x_handles_expires:
            added = self._pool_cached_discovered_authors()
            selected = self._select_author_handles(excluded_handles)
            if added:
                logger.info('[anime_public_push_test] cached Pixiv pool expansion: newly_pooled=%s selected_pool=%s', added, len(selected))
            return selected

        async def fetch_ranking(mode: str, page: int) -> list[dict[str, Any]]:
            params = {'mode': mode, 'format': 'json', 'p': str(page)}
            if mode == 'daily':
                params['content'] = 'illust'
            async with session.get(PIXIV_RANKING_API, params=params, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.pixiv.net/'}) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
            return list(payload.get('contents') or [])

        async def fetch_target_tag(tag: str) -> list[dict[str, Any]]:
            async with session.get(PIXIV_SEARCH_API.format(tag=tag), params={'word': tag, 'order': 'date_d', 'mode': 'all', 'p': '1', 's_mode': 's_tag_full', 'type': 'all', 'lang': 'ja'}, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.pixiv.net/'}) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
            body = payload.get('body') or {}
            return list((body.get('illustManga') or {}).get('data') or [])
        ranking_calls = [fetch_ranking(mode, page) for mode in ('daily', 'male', 'female') for page in range(1, self._x_pixiv_ranking_pages + 1)]
        target_calls = [fetch_target_tag(tag) for tag, _ in PIXIV_TARGET_TAGS]
        discovery_results = await asyncio.gather(*ranking_calls, *target_calls, return_exceptions=True)
        ranking_results = discovery_results[:len(ranking_calls)]
        target_results = discovery_results[len(ranking_calls):]
        ranked_users: dict[str, float] = {}
        for result in ranking_results:
            if isinstance(result, Exception):
                logger.warning('[anime_public_push_test] Pixiv ranking discovery failed: %s', result)
                continue
            for item in result:
                try:
                    created_at = datetime.fromtimestamp(int(item.get('illust_upload_timestamp') or 0), timezone.utc)
                except (TypeError, ValueError, OSError):
                    continue
                if not self._is_fresh(created_at):
                    continue
                user_id = str(item.get('user_id') or '')
                if not user_id:
                    continue
                ranked_users[user_id] = max(ranked_users.get(user_id, 0.0), float(item.get('rating_count') or 0))
        targeted_users: dict[str, set[str]] = {}
        targeted_scores: dict[str, float] = {}
        target_cutoff = now - timedelta(days=7)
        for (tag, topic), result in zip(PIXIV_TARGET_TAGS, target_results):
            if isinstance(result, Exception):
                logger.warning('[anime_public_push_test] Pixiv target discovery failed tag=%s: %s', tag, result)
                continue
            eligible: list[tuple[float, str]] = []
            for item in result:
                try:
                    created_at = self._parse_datetime(item.get('createDate'))
                except (TypeError, ValueError):
                    continue
                if created_at < target_cutoff:
                    continue
                user_id = str(item.get('userId') or '')
                if not user_id:
                    continue
                score = float(item.get('bookmarkCount') or 0)
                eligible.append((score, user_id))
            for score, user_id in sorted(eligible, reverse=True)[:self._pixiv_target_users_per_tag]:
                targeted_users.setdefault(user_id, set()).add(topic)
                topic_weight = PREFERRED_TOPICS.get(topic, (1.0, ()))[0]
                targeted_scores[user_id] = max(targeted_scores.get(user_id, 0.0), topic_weight * 100000.0 + score)
        targeted_ids = [user_id for user_id, _ in sorted(targeted_scores.items(), key=lambda pair: pair[1], reverse=True)]
        ranked_ids = [user_id for user_id, _ in sorted(ranked_users.items(), key=lambda pair: pair[1], reverse=True)]
        user_ids = list(dict.fromkeys(targeted_ids + ranked_ids))[:self._x_dynamic_artist_limit]
        semaphore = asyncio.Semaphore(8)

        async def fetch_profile(user_id: str) -> tuple[str, str] | None:
            async with semaphore:
                async with session.get(PIXIV_USER_API.format(user_id=user_id), params={'full': '1'}, headers={'User-Agent': 'Mozilla/5.0', 'Referer': f'https://www.pixiv.net/users/{user_id}'}) as response:
                    response.raise_for_status()
                    payload = await response.json(content_type=None)
            social = (payload.get('body') or {}).get('social') or {}
            twitter = social.get('twitter') or {}
            url = str(twitter.get('url') or '')
            match = re.search('(?:twitter\\.com|x\\.com)/([A-Za-z0-9_]{1,15})', url)
            return (user_id, match.group(1)) if match else None
        profile_results = await asyncio.gather(*(fetch_profile(user_id) for user_id in user_ids), return_exceptions=True)
        discovered_pairs = [value for value in profile_results if isinstance(value, tuple) and len(value) == 2]
        discovered = [handle for _, handle in discovered_pairs]
        target_handle_topics: dict[str, set[str]] = {}
        for user_id, handle in discovered_pairs:
            topics = targeted_users.get(user_id)
            if topics:
                target_handle_topics.setdefault(handle, set()).update(topics)
        if discovered:
            self._dynamic_x_handles = list(dict.fromkeys(discovered))
            self._dynamic_target_x_handles = target_handle_topics
            added = self._pool_cached_discovered_authors()
            self._dynamic_x_handles_expires = now + timedelta(hours=6)
        elif not self._dynamic_x_handles:
            added = 0
            self._dynamic_x_handles_expires = now + timedelta(minutes=15)
        else:
            added = 0
        selected = self._select_author_handles(excluded_handles)
        logger.info('[anime_public_push_test] Pixiv authors=%s newly_pooled=%s selected_pool=%s', len(self._dynamic_x_handles), added, len(selected))
        return selected

    async def _fetch_x_feed(self, session: aiohttp.ClientSession, handle: str) -> list[XFeedItem]:
        last_error: Exception | None = None
        for base_url in self._x_rss_bases:
            try:
                async with session.get(f'{base_url}/{handle}/rss', headers={'Accept': 'application/rss+xml,application/xml;q=0.9', 'User-Agent': 'FreshRSS/1.26.1'}) as response:
                    response.raise_for_status()
                    body = await response.text()
                if '<item>' not in body:
                    raise ValueError('empty X RSS feed')
                root = ElementTree.fromstring(body)
                results: list[XFeedItem] = []
                for node in root.findall('./channel/item'):
                    title = str(node.findtext('title') or '')
                    description = str(node.findtext('description') or '')
                    if title.startswith(('RT by @', 'R to @')):
                        continue
                    if '<img' not in description.lower():
                        continue
                    link = str(node.findtext('link') or '')
                    match = re.search('/([^/]+)/status/(\\d+)', link)
                    if not match:
                        continue
                    try:
                        created_at = parsedate_to_datetime(str(node.findtext('pubDate') or '')).astimezone(timezone.utc)
                    except (TypeError, ValueError):
                        continue
                    if not self._is_fresh(created_at):
                        continue
                    results.append(XFeedItem(tweet_id=match.group(2), author=match.group(1), created_at=created_at, title=self._clean_title(title.removeprefix('Pinned: '))))
                return results
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return []

    async def _candidate_from_x_tweet(self, session: aiohttp.ClientSession, item: XFeedItem) -> Candidate | None:
        data: dict[str, Any] = {}
        try:
            async with session.get(X_SYNDICATION_API, params={'id': item.tweet_id, 'lang': 'en', 'features': X_SYNDICATION_FEATURES, 'token': self._x_syndication_token(item.tweet_id)}, headers={'User-Agent': 'Mozilla/5.0'}) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
        except Exception as exc:
            logger.debug('[anime_public_push_test] X official detail failed %s: %s', item.tweet_id, exc)
        likes = int(data.get('favorite_count') or 0)
        photos = [media for media in data.get('mediaDetails') or [] if media.get('type') == 'photo']
        text = str(data.get('text') or item.title)
        user = data.get('user') or {}
        author = str(user.get('screen_name') or item.author)
        created_at = item.created_at
        if data.get('created_at'):
            created_at = self._parse_datetime(data['created_at'])
        reposts = int(data.get('retweet_count') or 0)
        sensitive = data.get('possibly_sensitive') is True
        if not data or not photos:
            try:
                async with session.get(X_FALLBACK_API.format(tweet_id=item.tweet_id), headers={'User-Agent': 'Mozilla/5.0'}) as response:
                    response.raise_for_status()
                    fallback = await response.json(content_type=None)
                tweet = fallback.get('tweet') or {}
                media = tweet.get('media') or {}
                photos = list(media.get('photos') or [])
                likes = int(tweet.get('likes') or 0)
                reposts = int(tweet.get('retweets') or 0)
                text = str(tweet.get('text') or item.title)
                author = str((tweet.get('author') or {}).get('screen_name') or item.author)
                sensitive = tweet.get('possibly_sensitive') is True
                created_value = tweet.get('created_at')
                if created_value:
                    created_at = parsedate_to_datetime(str(created_value)).astimezone(timezone.utc)
            except Exception:
                return None
        if sensitive or not photos or likes < self._min_likes:
            return None
        if not self._is_fresh(created_at):
            return None
        alt = str(photos[0].get('ext_alt_text') or '')
        if self._contains_blocked_term(f'{text} {alt}'):
            return None
        if self._contains_non_art_term(f'{text} {alt}'):
            return None
        image_url = str(photos[0].get('media_url_https') or photos[0].get('url') or '')
        if not image_url.startswith('https://'):
            return None
        if 'pbs.twimg.com/media/' in image_url and 'name=' not in image_url:
            image_url = f'{image_url}?name=orig'
        age_hours = self._age_hours(created_at)
        score = math.log1p(likes + reposts * 2.0) / (age_hours + 2.0) ** 0.55
        topic, topic_weight = self._detect_preferred_topic(f'{text} {alt}')
        related_handles = [self._normalize_x_handle(value) for value in re.findall('(?<![A-Za-z0-9_])@([A-Za-z0-9_]{1,15})', text)]
        quoted = data.get('quoted_tweet') or data.get('quoted_status') or {}
        if isinstance(quoted, dict):
            quoted_user = quoted.get('user') or {}
            related_handles.append(self._normalize_x_handle(str(quoted_user.get('screen_name') or '')))
        normalized_author = self._normalize_x_handle(author)
        related_authors = tuple((value for value in dict.fromkeys(related_handles) if value and value != normalized_author))[:4]
        candidate = Candidate(item_key=f'x:{item.tweet_id}', source='X', image_url=image_url, page_url=f'https://x.com/{author}/status/{item.tweet_id}', created_at=created_at, title=self._clean_title(text or item.title or '二次元插画'), author=f'@{author}', score=score, stats=f'❤ {likes}' + (f'  ↻ {reposts}' if reposts else ''), related_authors=related_authors, topic=topic, topic_weight=topic_weight)
        self._record_author_signal(candidate, 'hot')
        return candidate

    @staticmethod
    def _to_base36(value: int, width: int=0) -> str:
        alphabet = '0123456789abcdefghijklmnopqrstuvwxyz'
        if value == 0:
            encoded = '0'
        else:
            encoded = ''
            while value:
                value, remainder = divmod(value, 36)
                encoded = alphabet[remainder] + encoded
        return encoded.rjust(width, '0')

    @classmethod
    def _x_syndication_token(cls, tweet_id: str) -> str:
        value = float(tweet_id) / 1000000000000000.0 * math.pi
        whole = int(value)
        fraction = value - whole
        exact = Fraction(*value.as_integer_ratio())
        prefix = 0
        for digits in range(1, 25):
            fraction *= 36
            digit = int(fraction)
            fraction -= digit
            prefix = prefix * 36 + digit
            matches: list[tuple[Fraction, str]] = []
            denominator = 36 ** digits
            for candidate in (prefix, prefix + 1):
                rational = Fraction(whole * denominator + candidate, denominator)
                if float(rational) == value:
                    matches.append((abs(rational - exact), cls._to_base36(candidate, digits)))
            if matches:
                fractional = min(matches, key=lambda pair: pair[0])[1]
                return (cls._to_base36(whole) + fractional).replace('0', '')
        raise ValueError('cannot build X syndication token')

    async def _fetch_bluesky(self, session: aiohttp.ClientSession) -> list[Candidate]:
        results: dict[str, Candidate] = {}
        for feed_uri in self._bluesky_feeds:
            async with session.get(BSKY_API, params={'feed': feed_uri, 'limit': '100'}) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
            for entry in payload.get('feed', []):
                post = entry.get('post') or {}
                record = post.get('record') or {}
                embed = post.get('embed') or {}
                images = embed.get('images') or (embed.get('media') or {}).get('images')
                if not images:
                    continue
                created_at = self._parse_datetime(record.get('createdAt') or post.get('indexedAt'))
                if not self._is_fresh(created_at):
                    continue
                labels = {str(label.get('val', '')).lower() for label in (post.get('labels') or []) + ((post.get('author') or {}).get('labels') or [])}
                text = str(record.get('text') or '')
                alt = str(images[0].get('alt') or '')
                if labels & BLOCKED_LABELS or self._contains_blocked_term(f'{text} {alt}'):
                    continue
                if self._contains_non_art_term(f'{text} {alt}'):
                    continue
                uri = str(post.get('uri') or '')
                if not uri:
                    continue
                rkey = uri.rsplit('/', 1)[-1]
                author_data = post.get('author') or {}
                handle = str(author_data.get('handle') or 'unknown')
                image_url = str(images[0].get('fullsize') or '')
                if not image_url.startswith('https://'):
                    continue
                likes = int(post.get('likeCount') or 0)
                if likes < self._min_likes:
                    continue
                reposts = int(post.get('repostCount') or 0)
                replies = int(post.get('replyCount') or 0)
                age_hours = self._age_hours(created_at)
                engagement = likes + reposts * 2.0 + replies * 0.5
                score = math.log1p(engagement) / (age_hours + 2.0) ** 0.35
                topic, topic_weight = self._detect_preferred_topic(f'{text} {alt}')
                results[uri] = Candidate(item_key=f'bsky:{uri}', source='Bluesky', image_url=image_url, page_url=f'https://bsky.app/profile/{handle}/post/{rkey}', created_at=created_at, title=self._clean_title(alt or text or '二次元插画'), author=f'@{handle}', score=score, stats=f'❤ {likes}  ↻ {reposts}', topic=topic, topic_weight=topic_weight)
        return list(results.values())

    async def _fetch_wallhaven(self, session: aiohttp.ClientSession) -> list[Candidate]:
        searches = ({'categories': '010', 'purity': '100', 'sorting': 'toplist', 'topRange': '1d', 'order': 'desc', 'page': '1'}, {'categories': '010', 'purity': '100', 'sorting': 'date_added', 'order': 'desc', 'page': '1'})
        results: dict[str, Candidate] = {}
        for params in searches:
            async with session.get(WALLHAVEN_API, params=params) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
            for post in payload.get('data', []):
                if post.get('purity') != 'sfw' or post.get('category') != 'anime':
                    continue
                created_at = self._parse_datetime(post.get('created_at'))
                if not self._is_fresh(created_at):
                    continue
                post_id = str(post.get('id') or '')
                image_url = str(post.get('path') or '')
                page_url = str(post.get('url') or '')
                if not post_id or not image_url.startswith('https://'):
                    continue
                views = int(post.get('views') or 0)
                favorites = int(post.get('favorites') or 0)
                if favorites < self._min_likes:
                    continue
                age_hours = self._age_hours(created_at)
                engagement = views + favorites * 12.0
                score = math.log1p(engagement) / (age_hours + 2.0) ** 0.28
                candidate = Candidate(item_key=f'wallhaven:{post_id}', source='Wallhaven', image_url=image_url, page_url=page_url, created_at=created_at, title=f'Anime wallpaper · {post.get('resolution', '')}', author='Wallhaven 社区', score=score, stats=f'浏览 {views}  收藏 {favorites}')
                previous = results.get(post_id)
                if previous is None or candidate.score > previous.score:
                    results[post_id] = candidate
        return list(results.values())

    def _rank_candidates(self, candidates: list[Candidate]) -> list[Candidate]:
        fresh = [candidate for candidate in candidates if self._is_fresh(candidate.created_at)]
        grouped: dict[str, list[Candidate]] = {}
        for candidate in fresh:
            grouped.setdefault(candidate.source, []).append(candidate)

        def adjusted_score(item: Candidate) -> float:
            author_factor = 0.75 + min(100.0, self._author_pool_score(item)) / 200.0 if item.source == 'X' else 1.0
            _, topic_weight = self._author_topic_affinity(item)
            topic_factor = 1.0 + min(3.0, topic_weight) * 0.35
            return item.score * author_factor * topic_factor
        for values in grouped.values():
            values.sort(key=lambda item: (adjusted_score(item), item.created_at), reverse=True)
        sent_count = self._sent_count()
        source_pattern = ('X', 'X', 'X', 'Bluesky', 'X', 'X', 'Wallhaven', 'X', 'X', 'Bluesky')
        preferred = source_pattern[sent_count % len(source_pattern)]
        ordered = list(grouped.get(preferred, []))
        for source, values in grouped.items():
            if source != preferred:
                ordered.extend(values)
        return ordered

    async def _download_and_prepare(self, session: aiohttp.ClientSession, candidate: Candidate) -> tuple[Path, ImageFingerprint] | None:
        max_bytes = self._max_download_mb * 1024 * 1024
        async with session.get(candidate.image_url) as response:
            response.raise_for_status()
            content_length = int(response.headers.get('Content-Length') or 0)
            if content_length > max_bytes:
                raise ValueError('image too large')
            chunks = bytearray()
            async for chunk in response.content.iter_chunked(256 * 1024):
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    raise ValueError('image too large')
            raw = bytes(chunks)
        if len(raw) > max_bytes:
            raise ValueError('image too large')
        try:
            with PILImage.open(io.BytesIO(raw)) as opened:
                opened.seek(0)
                image = ImageOps.exif_transpose(opened).convert('RGB')
        except Exception as exc:
            raise ValueError('invalid image') from exc
        width, height = image.size
        if width < self._min_width or height < self._min_height:
            raise ValueError(f'resolution too small: {width}x{height}')
        ratio = max(width / height, height / width)
        if ratio > 2.8:
            raise ValueError(f'extreme aspect ratio: {width}x{height}')
        phash = self._perceptual_hash(image)
        dhash = self._difference_hash(image)
        crop_phashes = self._center_crop_phashes(image)
        image.thumbnail((2048, 2048), PILImage.Resampling.LANCZOS)
        normalized = io.BytesIO()
        image.save(normalized, format='JPEG', quality=90, optimize=True)
        output = normalized.getvalue()
        sha256 = hashlib.sha256(output).hexdigest()
        path = self._download_dir / f'{sha256[:20]}.jpg'
        path.write_bytes(output)
        return (path, ImageFingerprint(sha256, phash, dhash, crop_phashes))

    async def _passes_nsfw_scan(self, image_path: Path) -> tuple[bool, str]:
        if not self._require_nsfw_scan:
            return (True, 'scan disabled')
        detector = await self._get_nsfw_detector()
        if detector is None:
            return (False, 'NSFW detector unavailable')
        detections = await asyncio.to_thread(detector.detect, str(image_path))
        blocked = [item for item in detections if str(item.get('class', '')) in NUDENET_BLOCKED_CLASSES and float(item.get('score') or 0.0) >= self._nsfw_threshold]
        if not blocked:
            return (True, 'safe')
        worst = max(blocked, key=lambda item: float(item.get('score') or 0.0))
        return (False, f'NSFW {worst.get('class')}={float(worst.get('score') or 0.0):.3f}')

    async def _get_nsfw_detector(self):
        if self._detector is not None:
            return self._detector
        if self._detector_failed:
            return None
        try:
            from nudenet import NudeDetector
            self._detector = await asyncio.to_thread(NudeDetector)
        except Exception:
            self._detector_failed = True
            logger.exception('[anime_public_push_test] NudeNet init failed')
            return None
        return self._detector

    async def _passes_llm_vision_filter(self, image_path: Path) -> tuple[bool, str]:
        if not self._use_llm_vision_filter:
            return (True, 'vision scan disabled')
        audit_path = image_path.with_name(f'{image_path.stem}.audit.jpg')
        try:
            await asyncio.to_thread(self._create_audit_thumbnail, image_path, audit_path)
            using = self.context.get_using_provider(self._target_umo)
            provider_ids = list(self._vision_provider_ids)
            if using is not None and provider_ids:
                using_id = str(using.meta().id)
                if using_id not in provider_ids:
                    provider_ids.append(using_id)
            prompt = VISION_AUDIT_PROMPT
            errors: list[str] = []
            for provider_id in provider_ids:
                try:
                    response = await self.context.llm_generate(chat_provider_id=provider_id, image_urls=[str(audit_path)], prompt=prompt, system_prompt='你是严格保守的图片内容审核器，只输出 JSON。')
                    accepted, reason = self._parse_vision_audit(str(getattr(response, 'completion_text', '') or ''))
                    logger.info('[anime_public_push_test] vision audit provider=%s accepted=%s reason=%s', provider_id, accepted, reason)
                    return (accepted, 'vision safe' if accepted else f'vision: {reason}')
                except Exception as exc:
                    errors.append(f'{provider_id}: {type(exc).__name__}')
                    logger.warning('[anime_public_push_test] vision provider %s failed: %s', provider_id, exc)
            try:
                accepted, reason = await self._audit_with_vision_media(audit_path, prompt)
                logger.info('[anime_public_push_test] vision audit provider=vision_media accepted=%s reason=%s', accepted, reason)
                return (accepted, 'vision safe' if accepted else f'vision: {reason}')
            except Exception as exc:
                errors.append(f'vision_media: {type(exc).__name__}')
                logger.warning('[anime_public_push_test] vision_media fallback failed: %s', exc)
            return (False, 'vision unavailable: ' + ', '.join(errors))
        except Exception as exc:
            logger.warning('[anime_public_push_test] LLM vision setup failed: %s', exc)
            return (False, 'vision unavailable: setup failed')
        finally:
            audit_path.unlink(missing_ok=True)

    def _parse_vision_audit(self, raw: str) -> tuple[bool, str]:
        match = re.search('\\{.*\\}', raw, flags=re.DOTALL)
        if not match:
            raise ValueError('response is not JSON')
        result = json.loads(match.group(0))
        category = str(result.get('category') or '').strip()
        try:
            quality_score = int(result.get('quality_score'))
        except (TypeError, ValueError) as exc:
            raise ValueError('quality_score is missing') from exc
        accepted = category in POLISHED_ILLUSTRATION_CATEGORIES and quality_score >= self._vision_min_quality_score and (result.get('anime_art') is True) and (result.get('safe') is True) and (result.get('finished') is True) and (result.get('text_heavy') is False) and (result.get('screenshot_or_promo') is False) and (result.get('comic_or_infographic') is False) and (result.get('ai_generated') is False) and (not self._reject_male_characters or result.get('contains_male_character') is False)
        detail = str(result.get('reason') or 'vision rejected')[:100]
        male_flag = result.get('contains_male_character')
        reason = f'{category or 'unknown'} quality={quality_score} male={male_flag}: {detail}'
        return (accepted, reason)

    async def _audit_with_vision_media(self, audit_path: Path, prompt: str) -> tuple[bool, str]:
        metadata = self.context.get_registered_star('vision_media')
        vision_star = getattr(metadata, 'star_cls', None)
        manager = getattr(vision_star, '_media_mgr', None)
        config = getattr(vision_star, 'config', None)
        if manager is None or not isinstance(config, dict):
            raise RuntimeError('vision_media is not active')
        base_config = config.get('vision_llm', {})
        vision_config = manager._vision_fast_llm_config(base_config)
        if not vision_config:
            raise RuntimeError('vision_media route is not configured')
        vision_config = dict(vision_config)
        vision_config['enable_web_search'] = False
        vision_config['max_output_tokens'] = max(int(vision_config.get('max_output_tokens') or 0), self._vision_fallback_max_output_tokens)
        images = await manager.image_to_base64_frames(str(audit_path))
        if not images:
            raise RuntimeError('vision_media image conversion failed')
        url, headers, payload = manager._build_vision_llm_request(images, vision_config, prompt_override=prompt, want_json=True)
        timeout = 60 if vision_config.get('format') == 'volces_responses' else 30
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(url, headers=headers, json=payload, timeout=timeout) as response:
                if response.status != 200:
                    raise RuntimeError(f'vision_media HTTP {response.status}')
                data = await response.json()
        raw = str(manager._extract_vision_text(data, vision_config) or '').strip()
        return self._parse_vision_audit(raw)

    @staticmethod
    def _create_audit_thumbnail(source: Path, target: Path) -> None:
        with PILImage.open(source) as opened:
            image = opened.convert('RGB')
            image.thumbnail((1300, 1300), PILImage.Resampling.LANCZOS)
            image.save(target, format='JPEG', quality=88, optimize=True)

    def _build_message(self, candidate: Candidate, image_path: Path) -> MessageChain:
        local_time = candidate.created_at.astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
        topic_line = f'题材：{candidate.topic}\n' if candidate.topic else ''
        text = f'[二次元新图测试 · {candidate.source}]\n发布：{local_time}（最近 {self._max_age_hours} 小时）\n作者：{candidate.author}\n{topic_line}热度：{candidate.stats}\n来源：{candidate.page_url}'
        return MessageChain([Plain(text), Image.fromFileSystem(str(image_path))])

    def _is_processed(self, item_key: str) -> bool:
        with sqlite3.connect(self._db_path) as db:
            row = db.execute('SELECT 1 FROM processed_items WHERE item_key = ?', (item_key,)).fetchone()
        return row is not None

    def _mark_processed(self, candidate: Candidate, status: str, reason: str='') -> None:
        with sqlite3.connect(self._db_path) as db:
            db.execute('\n                INSERT OR REPLACE INTO processed_items\n                    (item_key, source, status, reason, processed_at)\n                VALUES (?, ?, ?, ?, ?)\n                ', (candidate.item_key, candidate.source, status, reason, datetime.now(timezone.utc).isoformat()))
            db.commit()

    def _mark_sent(self, candidate: Candidate, fingerprint: ImageFingerprint) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._db_path) as db:
            db.execute("\n                INSERT OR REPLACE INTO processed_items\n                    (item_key, source, status, reason, processed_at)\n                VALUES (?, ?, 'sent', '', ?)\n                ", (candidate.item_key, candidate.source, now))
            db.execute('\n                INSERT OR IGNORE INTO sent_images\n                    (sha256, phash, dhash, crop_phashes, item_key, source,\n                     page_url, sent_at)\n                VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n                ', (fingerprint.sha256, fingerprint.phash, fingerprint.dhash, ','.join(fingerprint.crop_phashes), candidate.item_key, candidate.source, candidate.page_url, now))
            db.commit()

    def _duplicate_image_reason(self, fingerprint: ImageFingerprint) -> str | None:
        with sqlite3.connect(self._db_path) as db:
            exact = db.execute('SELECT item_key FROM sent_images WHERE sha256 = ?', (fingerprint.sha256,)).fetchone()
            if exact:
                return f'exact sha256 matches {exact[0]}'
            hashes = db.execute('SELECT item_key, phash, dhash, crop_phashes FROM sent_images').fetchall()
        current_phash = int(fingerprint.phash, 16)
        current_dhash = int(fingerprint.dhash, 16)
        current_crops = tuple((int(value, 16) for value in fingerprint.crop_phashes))
        crop_distance_limit = max(4, self._duplicate_hash_distance - 3)
        for item_key, phash, dhash, crop_phashes in hashes:
            if phash:
                distance = (current_phash ^ int(phash, 16)).bit_count()
                if distance <= self._duplicate_hash_distance:
                    return f'phash distance={distance} matches {item_key}'
            if dhash:
                distance = (current_dhash ^ int(dhash, 16)).bit_count()
                if distance <= self._duplicate_hash_distance:
                    return f'dhash distance={distance} matches {item_key}'
            stored_crops = tuple((int(value, 16) for value in str(crop_phashes or '').split(',') if value))
            if stored_crops and current_crops and phash:
                current_variants = (current_phash, *current_crops)
                stored_variants = (int(phash, 16), *stored_crops)
                distance = min(((left ^ right).bit_count() for left in current_variants for right in stored_variants))
                if distance <= crop_distance_limit:
                    return f'crop phash distance={distance} matches {item_key}'
        return None

    def _sent_count(self) -> int:
        with sqlite3.connect(self._db_path) as db:
            row = db.execute('SELECT COUNT(*) FROM sent_images').fetchone()
        return int(row[0] if row else 0)

    @staticmethod
    def _perceptual_hash(image: PILImage.Image) -> str:
        gray = np.asarray(image.convert('L').resize((32, 32)), dtype=np.float32)
        dct = cv2.dct(gray)
        low = dct[:8, :8]
        median = float(np.median(low.flatten()[1:]))
        bits = (low > median).flatten()
        value = 0
        for bit in bits:
            value = value << 1 | int(bit)
        return f'{value:016x}'

    @staticmethod
    def _difference_hash(image: PILImage.Image) -> str:
        gray = np.asarray(image.convert('L').resize((9, 8), PILImage.Resampling.LANCZOS), dtype=np.int16)
        bits = (gray[:, 1:] > gray[:, :-1]).flatten()
        value = 0
        for bit in bits:
            value = value << 1 | int(bit)
        return f'{value:016x}'

    @classmethod
    def _center_crop_phashes(cls, image: PILImage.Image) -> tuple[str, ...]:
        width, height = image.size
        hashes: list[str] = []
        for fraction in (0.9, 0.8):
            crop_width = max(1, int(width * fraction))
            crop_height = max(1, int(height * fraction))
            left = (width - crop_width) // 2
            top = (height - crop_height) // 2
            cropped = image.crop((left, top, left + crop_width, top + crop_height))
            hashes.append(cls._perceptual_hash(cropped))
        return tuple(hashes)

    def _is_fresh(self, created_at: datetime) -> bool:
        now = datetime.now(timezone.utc)
        return now - timedelta(hours=self._max_age_hours) <= created_at <= now + timedelta(minutes=10)

    @staticmethod
    def _age_hours(created_at: datetime) -> float:
        delta = datetime.now(timezone.utc) - created_at
        return max(0.0, delta.total_seconds() / 3600.0)

    @staticmethod
    def _parse_datetime(value: Any) -> datetime:
        if not value:
            raise ValueError('missing created time')
        normalized = str(value).replace('Z', '+00:00')
        normalized = re.sub('(\\.\\d{6})\\d+(?=[+-]\\d{2}:\\d{2}$)', '\\1', normalized)
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _optional_config_datetime(self, key: str) -> datetime | None:
        value = str(self.config.get(key, '') or '').strip()
        if not value:
            return None
        try:
            return self._parse_datetime(value)
        except (TypeError, ValueError):
            logger.warning('[anime_public_push_test] ignoring invalid %s=%r', key, value)
            return None

    def _test_duration_label(self) -> str:
        if self._test_started_at is None or self._test_ends_at is None:
            return ''
        total_minutes = max(1, int(round((self._test_ends_at - self._test_started_at).total_seconds() / 60.0)))
        hours, minutes = divmod(total_minutes, 60)
        if hours and (not minutes):
            return f'{hours}小时'
        if hours:
            return f'{hours}小时{minutes}分钟'
        return f'{minutes}分钟'

    @staticmethod
    def _contains_blocked_term(text: str) -> bool:
        normalized = re.sub('[_#\\-]+', ' ', text.lower())
        return any((term in normalized for term in BLOCKED_TERMS))

    @staticmethod
    def _contains_non_art_term(text: str) -> bool:
        normalized = re.sub('[_#\\-]+', ' ', text.lower())
        return any((term in normalized for term in NON_ART_TERMS))

    @staticmethod
    def _clean_title(text: str) -> str:
        return ' '.join(text.replace('\n', ' ').split())[:160]

    @filter.command('二次元推送状态')
    async def push_status(self, event: AstrMessageEvent):
        status = '运行中' if self._enabled and self._push_task else '已停止'
        active, probation, paused, _ = self._author_pool_summary()
        window_line = ''
        if self._test_started_at is not None and self._test_ends_at is not None:
            local_tz = timezone(timedelta(hours=8))
            end_text = self._test_ends_at.astimezone(local_tz).strftime('%m-%d %H:%M:%S')
            remaining_hours = max(0.0, (self._test_ends_at - datetime.now(timezone.utc)).total_seconds() / 3600.0)
            window_line = f'测试结束：{end_text}（剩余 {remaining_hours:.1f} 小时）\n'
        yield event.plain_result(f'无账号二次元推送测试：{status}\n' + window_line + f'数据源：X 匿名时间线 + Bluesky + Wallhaven\n目标：{self._target_umo}\n检查间隔：{self._interval_seconds} 秒\n新鲜度：{self._max_age_hours} 小时\n最低热度：X/Bluesky ❤、Wallhaven 收藏 {self._min_likes}\n插画质量门槛：{self._vision_min_quality_score} 分\n男性角色：{('拦截' if self._reject_male_characters else '允许')}\n重复图哈希阈值：{self._duplicate_hash_distance}\nX 作者池：活跃 {active} / 观察 {probation} / 暂停 {paused}\n已发送：{self._sent_count()} 张')

    @filter.command('二次元作者池')
    async def author_pool_status(self, event: AstrMessageEvent):
        active, probation, paused, top = self._author_pool_summary()
        lines = ['二次元插画作者池', f'活跃 {active} / 观察 {probation} / 暂停 {paused}', f'每轮最多检查 {self._author_pool_refresh_limit} 位；探索比例 {self._author_pool_exploration_ratio:.0%}', '高权重作者：']
        lines.extend((f'@{handle}  分数 {score}  通过 {passed}  已发 {sent}' for handle, score, passed, sent in top))
        yield event.plain_result('\n'.join(lines))

    @filter.command('二次元推送一次')
    async def push_once_command(self, event: AstrMessageEvent):
        pushed = await self._push_once()
        yield event.plain_result('已推送一张新图' if pushed else '当前没有新的合格图片')

    async def terminate(self):
        self._enabled = False
        if self._push_task is not None:
            self._push_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._push_task
            self._push_task = None
