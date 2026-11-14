import asyncio
import json
import os
import random
import re
import shutil
from datetime import datetime, timedelta

import astrbot.core.message.components as Comp
from astrbot.api.all import *
from astrbot.api.star import StarTools
from astrbot.core.message.components import Image, Node, Nodes, Plain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path, get_astrbot_path
from .alias_map import WIFE_ALIAS_LIST, normalize_alias_input

PLUGIN_DIR = StarTools.get_data_dir("astrbot_plugin_animewifex")
CONFIG_DIR = os.path.join(PLUGIN_DIR, "config")
IMG_DIR = os.path.join(PLUGIN_DIR, "img", "wife")
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(IMG_DIR, exist_ok=True)
PLUGIN_INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_IMG_DIR = os.path.join(get_astrbot_data_path(), "plugin_data", "wife")
SOURCE_IMG_DIR = os.path.join(
    get_astrbot_path(),
    "data",
    "plugin_data",
    "astrbot_plugin_animewifex",
    "img",
    "wife",
)
PLUGIN_BUNDLED_IMG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "plugin_data",
    "astrbot_plugin_animewifex",
    "img",
    "wife",
)
WISH_FILE = os.path.join(CONFIG_DIR, "wishes.json")

NTR_STATUS_FILE = os.path.join(CONFIG_DIR, "ntr_status.json")
NTR_RECORDS_FILE = os.path.join(CONFIG_DIR, "ntr_records.json")
CHANGE_RECORDS_FILE = os.path.join(CONFIG_DIR, "change_records.json")
RESET_RECORDS_FILE = os.path.join(CONFIG_DIR, "reset_ntr_records.json")
SWAP_REQUESTS_FILE = os.path.join(CONFIG_DIR, "swap_requests.json")
SWAP_LIMIT_FILE = os.path.join(CONFIG_DIR, "swap_limit_records.json")
# 定义存储计数的 JSON 文件路径
COUNT_FILE = os.path.join(CONFIG_DIR, "chara_stats.json")

# 惩罚与保护期相关文件与内存结构
FREEZE_RECORDS_FILE = os.path.join(CONFIG_DIR, "freeze_records.json")
NTR_TWO_DAY_FILE     = os.path.join(CONFIG_DIR, "ntr_two_day_records.json")
FORCED_IMG_REL       = os.path.join("img1", "游戏王#原始生命态尼比鲁.png")

freeze_records = {}
ntr_two_day_records = {}

def save_freeze_records():
    save_json(FREEZE_RECORDS_FILE, freeze_records)


def save_ntr_two_day_records():
    save_json(NTR_TWO_DAY_FILE, ntr_two_day_records)


def load_freeze_records():
    globals().update(freeze_records=load_json(FREEZE_RECORDS_FILE))


def load_ntr_two_day_records():
    globals().update(ntr_two_day_records=load_json(NTR_TWO_DAY_FILE))

# 特殊字符映射表：将Windows不允许的字符映射为特殊标记
CHAR_MAPPING = {
    "\\": "_backslash_",
    "/": "_slash_",
    ":": "_colon_",
    "*": "_star_",
    "?": "_question_",
    '"': "_quote_",
    "<": "_lt_",
    ">": "_gt_",
    "|": "_pipe_"
}

# 还原文件名中的特殊字符
# 注意需要从长到短排序，避免部分替换导致其他替换失效
def restore_special_chars(filename):
    # 创建反转的映射表
    reverse_mapping = {v: k for k, v in CHAR_MAPPING.items()}
    # 按替换字符串长度从长到短排序，确保较长的替换字符串先被处理
    sorted_replacements = sorted(reverse_mapping.items(), key=lambda x: len(x[0]), reverse=True)
    # 还原特殊字符
    restored_name = filename
    for replacement, original_char in sorted_replacements:
        restored_name = restored_name.replace(replacement, original_char)
    return restored_name

# 从角色名中提取纯角色名（去除最后一个下划线后的数字部分）
def get_pure_chara_name(chara):
    # 使用正则表达式匹配最后一个下划线后的数字部分
    # 例如："UMP-45" -> "UMP-45", "AL_1S_2" -> "AL_1S", "丛雨_2" -> "丛雨"
    match = re.match(r"(.+?)(?:_\d+)?$", chara)
    if match:
        return match.group(1)
    return chara


def _normalize_chara_key(chara: str) -> str:
    s = str(chara or "")
    s = re.sub(r"\.(jpg|jpeg|png|webp|gif)$", "", s, flags=re.IGNORECASE)
    return get_pure_chara_name(s)


def get_today():
    """获取当前上海时区日期字符串"""
    utc_now = datetime.utcnow()
    return (utc_now + timedelta(hours=8)).date().isoformat()

def _parse_date(date_str: str):
    try:
        return datetime.fromisoformat(date_str).date()
    except Exception:
        utc_now = datetime.utcnow()
        return (utc_now + timedelta(hours=8)).date()

def add_days(date_str: str, days: int) -> str:
    base = _parse_date(date_str)
    return (base + timedelta(days=days)).isoformat()

def within_two_day_window(start_date: str, today: str) -> bool:
    # 允许 start_date 当天或次日（差值<=1天）都在窗口内
    return abs((_parse_date(today) - _parse_date(start_date)).days) <= 1

def is_freeze_active_for(gid: str, uid: str, today: str):
    rec = freeze_records.get(gid, {}).get(uid)
    if not rec:
        return False, None
    until = rec.get("until", today)
    return _parse_date(until) >= _parse_date(today), rec

def load_json(path):
    """安全加载 JSON 文件"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_json(path, data):
    """保存数据到 JSON 文件"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def _load_json_flexible(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None


def _merge_dict_keep_dst(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
            continue
        if isinstance(dst.get(k), dict) and isinstance(v, dict):
            dst[k] = _merge_dict_keep_dst(dst[k], v)
    return dst


def _merge_chara_stats(dst: dict, src: dict) -> dict:
    for chara, rec in src.items():
        if chara not in dst:
            dst[chara] = rec
            continue
        if not isinstance(dst.get(chara), dict) or not isinstance(rec, dict):
            continue
        for key in ["pick_count", "ntr_count", "divorce_count"]:
            try:
                dst_val = int(dst[chara].get(key, 0) or 0)
                src_val = int(rec.get(key, 0) or 0)
                dst[chara][key] = max(dst_val, src_val)
            except Exception:
                continue
    return dst


def _migrate_legacy_config_dir(src_dir: str) -> bool:
    try:
        if not os.path.isdir(src_dir):
            return False
        migrated = False
        for name in os.listdir(src_dir):
            if not name.endswith(".json"):
                continue
            src_path = os.path.join(src_dir, name)
            dst_path = os.path.join(CONFIG_DIR, name)
            if not os.path.isfile(src_path):
                continue
            if not os.path.exists(dst_path):
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                shutil.copy2(src_path, dst_path)
                migrated = True
                continue
            src_data = _load_json_flexible(src_path)
            dst_data = _load_json_flexible(dst_path)
            if not isinstance(src_data, dict) or not isinstance(dst_data, dict):
                continue
            if name == os.path.basename(COUNT_FILE):
                merged = _merge_chara_stats(dst_data, src_data)
            else:
                merged = _merge_dict_keep_dst(dst_data, src_data)
            save_json(dst_path, merged)
            migrated = True
        return migrated
    except Exception:
        return False


def _migrate_legacy_data():
    migrated = False
    legacy_dirs = [
        os.path.join(PLUGIN_INSTALL_DIR, "config"),
        os.path.join(get_astrbot_data_path(), "plugins", "astrbot_plugin_animewifex", "config"),
    ]
    if os.name != "nt" and (os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP")):
        legacy_dirs.append(
            os.path.join(
                "/mnt",
                "c",
                "mycode",
                "qqbot",
                "AstrBot",
                "data",
                "plugin_data",
                "astrbot_plugin_animewifex",
                "config",
            ),
        )
    for d in legacy_dirs:
        migrated = _migrate_legacy_config_dir(d) or migrated
    legacy_files = [
        os.path.join(PLUGIN_INSTALL_DIR, "wishes.json"),
        os.path.join(PLUGIN_INSTALL_DIR, "chara_stats.json"),
    ]
    for src_path in legacy_files:
        try:
            if not os.path.isfile(src_path):
                continue
            name = os.path.basename(src_path)
            dst_path = os.path.join(CONFIG_DIR, name)
            if os.path.exists(dst_path):
                continue
            shutil.copy2(src_path, dst_path)
            migrated = True
        except Exception:
            continue
    if migrated:
        try:
            logger.info(f"astrbot_plugin_animewifex 已从旧目录迁移数据到 {CONFIG_DIR}")
        except Exception:
            pass


_migrate_legacy_data()


def _list_image_files(dir_path: str) -> list[str]:
    try:
        files: list[str] = []
        with os.scandir(dir_path) as it:
            for entry in it:
                try:
                    if entry.is_file():
                        files.append(entry.name)
                except Exception:
                    continue
        return files
    except Exception:
        return []


def _ensure_img_dir():
    global IMG_DIR
    for candidate in [IMG_DIR, SOURCE_IMG_DIR, PLUGIN_BUNDLED_IMG_DIR, LEGACY_IMG_DIR]:
        if _list_image_files(candidate):
            IMG_DIR = candidate
            return


_ensure_img_dir()

_IMG_FILES_CACHE = {"dir": None, "mtime_ns": None, "files": [], "files_lower": []}


def _get_img_files_cached() -> tuple[list[str], list[str]]:
    _ensure_img_dir()
    try:
        mtime_ns = os.stat(IMG_DIR).st_mtime_ns
    except Exception:
        mtime_ns = None
    if _IMG_FILES_CACHE["dir"] != IMG_DIR or _IMG_FILES_CACHE["mtime_ns"] != mtime_ns:
        files = _list_image_files(IMG_DIR)
        _IMG_FILES_CACHE["dir"] = IMG_DIR
        _IMG_FILES_CACHE["mtime_ns"] = mtime_ns
        _IMG_FILES_CACHE["files"] = files
        _IMG_FILES_CACHE["files_lower"] = [f.lower() for f in files]
    return _IMG_FILES_CACHE["files"], _IMG_FILES_CACHE["files_lower"]

ntr_statuses = {}
ntr_records  = {}
change_records = {}
swap_requests = {}
swap_limit_records = {}


def load_ntr_statuses():
    globals().update(ntr_statuses=load_json(NTR_STATUS_FILE))


def load_ntr_records():
    globals().update(ntr_records=load_json(NTR_RECORDS_FILE))


def load_change_records():
    raw = load_json(CHANGE_RECORDS_FILE)
    change_records.clear()
    for gid, users in raw.items():
        change_records[gid] = {}
        for uid, rec in users.items():
            if isinstance(rec, str):
                change_records[gid][uid] = {"date": rec, "count": 1}
            else:
                change_records[gid][uid] = rec

def save_ntr_statuses():
    save_json(NTR_STATUS_FILE, ntr_statuses)


def save_ntr_records():
    save_json(NTR_RECORDS_FILE, ntr_records)


def save_change_records():
    save_json(CHANGE_RECORDS_FILE, change_records)


def load_group_config(group_id: str) -> dict:
    path = os.path.join(CONFIG_DIR, f"{group_id}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_group_config(group_id: str, user_id: str, wife_name: str, date: str, nickname: str, config: dict):
    config[user_id] = [wife_name, date, nickname]
    path = os.path.join(CONFIG_DIR, f"{group_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)

def load_swap_requests():
    raw = load_json(SWAP_REQUESTS_FILE)
    today = get_today()
    cleaned = {}
    for gid, reqs in raw.items():
        valid = {}
        for uid, rec in reqs.items():
            if rec.get("date") == today:
                valid[uid] = rec
        if valid:
            cleaned[gid] = valid
    globals()["swap_requests"] = cleaned
    if raw != cleaned:
        save_json(SWAP_REQUESTS_FILE, cleaned)

def save_swap_requests():
    save_json(SWAP_REQUESTS_FILE, swap_requests)

def load_swap_limit_records():
    globals()["swap_limit_records"] = load_json(SWAP_LIMIT_FILE)

# 初始化计数的函数
def init_counts():
    if os.path.exists(COUNT_FILE):
        with open(COUNT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

# 初始化统计信息的函数
def init_stats():
    if os.path.exists(COUNT_FILE):
        with open(COUNT_FILE, encoding="utf-8") as f:
            stats = json.load(f)
        if isinstance(stats, dict):
            normalized = {}
            changed = False
            for k, v in stats.items():
                pure = _normalize_chara_key(k)
                if pure != k:
                    changed = True
                if pure not in normalized:
                    normalized[pure] = v if isinstance(v, dict) else {}
                    continue
                if not isinstance(normalized[pure], dict) or not isinstance(v, dict):
                    continue
                for key in ["pick_count", "ntr_count", "divorce_count"]:
                    try:
                        normalized[pure][key] = int(normalized[pure].get(key, 0) or 0) + int(v.get(key, 0) or 0)
                    except Exception:
                        continue
            if changed:
                try:
                    with open(COUNT_FILE, "w", encoding="utf-8") as f:
                        json.dump(normalized, f, ensure_ascii=False, indent=4)
                except Exception:
                    pass
            return normalized
        return {}
    return {}

# 更新抽中次数的函数
def update_pick_count(chara):
    pure_chara = _normalize_chara_key(chara)
    stats = init_stats()
    if pure_chara not in stats:
        stats[pure_chara] = {"pick_count": 0, "ntr_count": 0, "divorce_count": 0}
    stats[pure_chara]["pick_count"] += 1
    with open(COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=4)
    return stats[pure_chara]["pick_count"]

# 更新被牛次数的函数
def update_ntr_count(chara):
    pure_chara = _normalize_chara_key(chara)
    stats = init_stats()
    if pure_chara not in stats:
        stats[pure_chara] = {"pick_count": 0, "ntr_count": 0, "divorce_count": 0}
    stats[pure_chara]["ntr_count"] += 1
    with open(COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=4)
    return stats[pure_chara]["ntr_count"]

# 更新离婚次数的函数
def update_divorce_count(chara):
    pure_chara = _normalize_chara_key(chara)
    stats = init_stats()
    if pure_chara not in stats:
        stats[pure_chara] = {"pick_count": 0, "ntr_count": 0, "divorce_count": 0}
    stats[pure_chara]["divorce_count"] += 1
    with open(COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=4)
    return stats[pure_chara]["divorce_count"]

# 获取某个 chara 的所有统计信息
def get_chara_stats(chara):
    pure_chara = _normalize_chara_key(chara)
    stats = init_stats()
    # 获取角色统计信息，若角色不存在则使用默认值
    chara_stats = stats.get(pure_chara, {"pick_count": 0, "ntr_count": 0, "divorce_count": 0})
    # 确保 ntr_count 和 divorce_count 存在，不存在则赋值为 0
    if not chara_stats.get("ntr_count"):
        chara_stats["ntr_count"] = 0
    if not chara_stats.get("divorce_count"):
        chara_stats["divorce_count"] = 0
    # 更新统计信息
    stats[pure_chara] = chara_stats
    # 将更新后的统计信息写回 JSON 文件
    with open(COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=4)
    return chara_stats

def load_wishes():
    try:
        with open(WISH_FILE, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def save_wish(gid, uid, wish_chara):
    wishes = load_wishes()
    if gid not in wishes:
        wishes[gid] = {}
    wishes[gid][uid] = wish_chara
    with open(WISH_FILE, "w", encoding="utf-8-sig") as f:
        json.dump(wishes, f, ensure_ascii=False, indent=2)
def get_user_wish(gid, uid):
    wishes = load_wishes()
    return wishes.get(gid, {}).get(uid, None)

def save_swap_limit_records():
    save_json(SWAP_LIMIT_FILE, swap_limit_records)

load_ntr_statuses()
load_ntr_records()
load_change_records()
load_swap_requests()
load_swap_limit_records()

@register("astrbot_plugin_animewifex", "monbed", "群二次元老婆插件修改版", "1.5.7", "https://github.com/monbed/astrbot_plugin_animewifex")
class WifePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 配置参数初始化
        self.ntr_max = config.get("ntr_max")
        self.ntr_possibility = config.get("ntr_possibility")
        self.change_max_per_day = config.get("change_max_per_day")
        self.reset_max_uses_per_day = config.get("reset_max_uses_per_day")
        self.reset_success_rate = config.get("reset_success_rate")
        self.reset_mute_duration = config.get("reset_mute_duration")
        self.image_base_url = config.get("image_base_url")
        self.swap_max_per_day = config.get("swap_max_per_day")

        # 新增：保存用户搜索结果的字典，格式为 {用户ID: [匹配的文件列表]}
        self.search_results = {}
        # 新增：保存用户许愿选择状态的字典
        self.wish_selection_states = {}

        # 命令与处理函数映射
        self.commands = {
            "抽老婆": self.animewife,
            "牛老婆": self.ntr_wife,
            "查老婆": self.search_wife,
            "切换ntr开关状态": self.switch_ntr,
            "换老婆": self.change_wife,
            "重置牛": self.reset_ntr,
            "交换老婆": self.swap_wife,
            "同意交换": self.agree_swap_wife,
            "拒绝交换": self.reject_swap_wife,
            "查看交换请求": self.view_swap_requests,
            "许愿老婆": self.wish_wife,
            "许愿": self.confirm_wish,
            "重置许愿": self.reset_wish_wife,
            "添加老婆": self.add_wife,
            "重置抽老婆": self.reset_pick_wife,
            "管理添加老婆权限": self.manage_wife_adders,
            "抽老婆帮助": self.show_wife_help,
            "搜索老婆库": self.search_wife_lib,
            "全部展示": self.show_all_search_results
        }
        self.admins = self.load_admins()
        self.wife_adders = self.load_wife_adders()

    def load_wife_adders(self):
        # 加载有添加老婆权限的用户列表
        path = os.path.join("data", "plugins", "astrbot_plugin_animewifex", "wife_adders.json")
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8-sig") as f:
                    return json.load(f)
            else:
                # 如果文件不存在，创建一个空列表
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8-sig") as f:
                    json.dump([], f, ensure_ascii=False)
                return []
        except Exception:
            return []

    def save_wife_adders(self):
        # 保存有添加老婆权限的用户列表
        path = os.path.join("data", "plugins", "astrbot_plugin_animewifex", "wife_adders.json")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8-sig") as f:
                json.dump(self.wife_adders, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def load_admins(self):
        # 加载管理员列表
        path = os.path.join("data", "cmd_config.json")
        try:
            with open(path, encoding="utf-8-sig") as f:
                cfg = json.load(f)
                return cfg.get("admins_id", [])
        except Exception:
            return []

    def parse_at_target(self, event):
        # 解析@目标用户
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                return str(comp.qq)
        return None

    def parse_target(self, event):
        # 解析命令目标用户
        target = self.parse_at_target(event)
        if target:
            return target
        msg = event.message_str.strip()
        if msg.startswith("牛老婆") or msg.startswith("查老婆"):
            name = msg.split(maxsplit=1)[-1]
            if name:
                group_id = str(event.message_obj.group_id)
                cfg = load_group_config(group_id)
                for uid, data in cfg.items():
                    nick = event.get_sender_name()
                    if nick and re.search(re.escape(name), nick, re.IGNORECASE):
                        return uid
        return None

    @event_message_type(EventMessageType.ALL)
    async def on_all_messages(self, event: AstrMessageEvent):
        # 消息分发，根据命令调用对应方法
        if not hasattr(event.message_obj, "group_id"):
            return
        text = event.message_str.strip()
        # 按命令长度降序排序，优先匹配更长的命令
        sorted_commands = sorted(self.commands.items(), key=lambda x: len(x[0]), reverse=True)
        for cmd, func in sorted_commands:
            # 只有精确匹配或命令后跟空格时才触发
            if text == cmd or text.startswith(f"{cmd} "):
                async for res in func(event):
                    yield res
                break

    async def animewife(self, event: AstrMessageEvent):
        # 抽老婆主逻辑
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        today = get_today()
        cfg = load_group_config(gid)
        _ensure_img_dir()

        user_data = cfg.get(uid, [None, ""])
        last_date = user_data[1] if len(user_data) > 1 else ""
        stored_img = user_data[0]

        # 检查存储的图片是否无效（例如是目录）
        is_invalid = False
        if stored_img and os.path.isdir(os.path.join(IMG_DIR, stored_img)):
            is_invalid = True

        if last_date != today or is_invalid:
            # 如果今天还没抽，或者数据异常（是目录），重新抽取
            if uid in cfg:
                del cfg[uid]

            # 获取并过滤图片列表，排除目录
            local_imgs = _list_image_files(IMG_DIR)
            # 冻结保护期：如果用户处于冻结保护期，则强制使用冻结图片
            load_freeze_records()
            is_frozen, fr = is_freeze_active_for(gid, uid, today)
            if is_frozen:
                img = fr.get("img", FORCED_IMG_REL)
            else:
                if not local_imgs:
                    yield event.plain_result(f"{nick}，老婆图库为空，请把图片放到：{IMG_DIR}")
                    return
                # 清理过期的冻结记录
                fr_gid = freeze_records.get(gid, {})
                fr_tmp = fr_gid.get(uid)
                if fr_tmp:
                    until = fr_tmp.get("until", today)
                    if _parse_date(until) < _parse_date(today):
                        try:
                            del fr_gid[uid]
                        except Exception:
                            pass
                        save_freeze_records()
            # if local_imgs:
                # img = random.choice(local_imgs)
            # else:
                # try:
                    # async with aiohttp.ClientSession() as session:
                        # async with session.get(self.image_base_url) as resp:
                            # text = await resp.text()
                            # img = random.choice(text.splitlines())
                # except:
                    # yield event.plain_result('获取图片失败')
                    # return

            # 检查用户是否有许愿
            if not is_frozen:
                wish_chara = get_user_wish(gid, uid)
                if wish_chara:
                    # 筛选出包含许愿角色的图片
                    wish_imgs = []
                    other_imgs = []
                    for img in local_imgs:
                        name = os.path.splitext(img)[0]
                        # 还原文件名中的特殊字符
                        name = restore_special_chars(name)
                        if "#" in name:
                            source, chara = name.split("#", 1)
                            pure_chara = get_pure_chara_name(chara)

                            # 检查是否精确匹配来源#角色
                            if "#" in wish_chara:
                                if wish_chara == f"{source}#{pure_chara}":
                                    wish_imgs.append(img)
                                elif wish_chara in pure_chara:
                                    # 如果许愿带来源但没匹配上，不进行宽泛匹配，直接跳过
                                    # 或者可以考虑保留宽泛匹配作为备选？
                                    # 这里为了区分，如果用户指定了来源，就严格匹配来源
                                    pass
                            else:
                                # 旧逻辑：宽泛匹配
                                if wish_chara in pure_chara:
                                    wish_imgs.append(img)
                                else:
                                    other_imgs.append(img)
                        else:
                            other_imgs.append(img)

                    # 如果有匹配的图片，增加概率
                    if wish_imgs:
                        # 50%概率从许愿图片中选，50%从其他图片中选
                        # 优先判定 other_imgs 是否存在，避免 IndexError
                        if other_imgs and random.random() >= 0.5:
                            img = random.choice(other_imgs)
                        else:
                            img = random.choice(wish_imgs)
                    else:
                        # 过滤掉目录
                        valid_imgs = local_imgs
                        if not valid_imgs:
                            yield event.plain_result(f"{nick}，老婆图库为空，请把图片放到：{IMG_DIR}")
                            return
                        img = random.choice(valid_imgs)
                else:
                    # 过滤掉目录
                    valid_imgs = local_imgs
                    if not valid_imgs:
                        yield event.plain_result(f"{nick}，老婆图库为空，请把图片放到：{IMG_DIR}")
                        return
                    img = random.choice(valid_imgs)
            cfg[uid] = [img, today, nick]
            # 给霜晨月伊雷娜或者立华奏
            if uid == "1457542224" and not is_frozen:
                img = random.choice(["魔女之旅#伊蕾娜.jpg","魔女之旅#伊蕾娜.jpeg","Angel Beats#立华奏.jpeg","Angel Beats#立华奏.jpg","我的青春恋爱物语果然有问题#雪之下雪乃.png","某科学的超电磁炮#食蜂操祈.jpeg","某科学的超电磁炮#食蜂操祈.jpg"])
            write_group_config(gid, uid, img, today, nick, cfg)
        else:
            img = cfg[uid][0]

            # 新增：即使是重复抽取，也进行名称解析，以显示更详细的信息
            name = os.path.splitext(os.path.basename(img))[0]
            name = restore_special_chars(name)
            if "#" in name:
                source, chara = name.split("#", 1)
                source = restore_special_chars(source)
                chara = restore_special_chars(chara)
                stats = get_chara_stats(chara)
                pure_chara = get_pure_chara_name(chara)
                text = f'{nick}，你今天已经抽过老婆了\n你的老婆是来自《{source}》的{pure_chara}\n被抽中的次数: {stats["pick_count"]}\n被牛的次数: {stats["ntr_count"]}'
            else:
                text = f"{nick}，你已经抽过老婆了，是{name}"

            path = os.path.join(IMG_DIR, img)
            if os.path.exists(path):
                # 检查路径是否是目录，如果是目录，则说明图片路径配置错误或图片丢失
                if os.path.isdir(path):
                     # 尝试在目录中找一张图片？或者报错？
                     # 正常逻辑下 cfg[uid][0] 应该是文件名。
                     # 如果 cfg[uid][0] 是 "img1"，而 "img1" 是目录名，那说明之前存错了。
                     # 修复逻辑：如果 img 是目录名，这不应该发生，除非 IMG_DIR 下有子目录且被 listdir 扫到了。
                     # listdir 会列出子目录。
                     # 我们应该在 listdir 时过滤掉目录。
                     pass
                chain = [Plain(text), Image.fromFileSystem(path)]
            else:
                chain = [Plain(text), Image.fromURL(self.image_base_url + img)]
            try:
                yield event.chain_result(chain)
            except Exception:
                yield event.plain_result(text)
            return


        # 新增：解析出处和角色名，分隔符为#
        # 只取文件名部分作为标题，避免包含子目录（如img1）
        name = os.path.splitext(os.path.basename(img))[0]
        # 统一还原特殊字符，和“换老婆”展示保持一致
        name = restore_special_chars(name)
        if "#" in name:
            source, chara = name.split("#", 1)
            # 进一步还原分段后的特殊字符（冗余但更安全）
            source = restore_special_chars(source)
            chara = restore_special_chars(chara)
            # 更新抽中次数与统计
            update_pick_count(chara)
            stats = get_chara_stats(chara)
            # 显示纯角色名（去除数字后缀）
            pure_chara = get_pure_chara_name(chara)
            text = f'{nick}，你今天的老婆是来自《{source}》的{pure_chara}\n被抽中的次数: {stats["pick_count"]}\n被牛的次数: {stats["ntr_count"]}'
        else:
            # 未包含来源分隔符时，直接使用还原后的文件名基名
            text = f"{nick}，你今天的老婆是{name}"
        path = os.path.join(IMG_DIR, img)
        if os.path.exists(path):
            chain = [Plain(text), Image.fromFileSystem(path)]
        else:
            chain = [Plain(text), Image.fromURL(self.image_base_url + img)]
        try:
            yield event.chain_result(chain)
        except Exception:
            yield event.plain_result(text)



    async def ntr_wife(self, event: AstrMessageEvent):
        # 牛老婆主逻辑
        gid = str(event.message_obj.group_id)
        if not ntr_statuses.get(gid, True):
            yield event.plain_result("牛老婆功能未开启！")
            return
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        today = get_today()

        grp = ntr_records.setdefault(gid, {})
        rec = grp.get(uid, {"date": today, "count": 0})
        if rec["date"] != today:
            rec = {"date": today, "count": 0}
        if rec["count"] >= self.ntr_max:
            yield event.plain_result(f"{nick}，每日最多{self.ntr_max}次，明天再来~")
            return

        tid = self.parse_target(event)
        if not tid or tid == uid:
            msg = "请指定目标" if not tid else "不能牛自己"
            yield event.plain_result(f"{nick}，{msg}")
            return

        cfg = load_group_config(gid)
        if tid not in cfg or cfg[tid][1] != today:
            yield event.plain_result("对方没有可牛的老婆")
            return

        # 保护期检查：发起者或目标处于冻结期，均不可牛
        load_freeze_records()
        frozen_self, fr_self = is_freeze_active_for(gid, uid, today)
        if frozen_self:
            yield event.plain_result(f"{nick}，你当前老婆处于两天保护期，期间不可使用牛老婆或交换。")
            return
        frozen_target, fr_target = is_freeze_active_for(gid, tid, today)
        if frozen_target:
            yield event.plain_result("对方老婆处于两天保护期，暂时不能被牛或交换。")
            return

        # 两天窗口计数：第5次触发惩罚并冻结两天
        load_ntr_two_day_records()
        grp2 = ntr_two_day_records.setdefault(gid, {})
        tw = grp2.get(uid)
        if not tw or not within_two_day_window(tw.get("start", today), today):
            tw = {"start": today, "count": 0}
        tw["count"] += 1
        grp2[uid] = tw
        save_ntr_two_day_records()
        if tw["count"] == 5:
            # 强制替换为尼比鲁，并冻结两天
            img = FORCED_IMG_REL
            # 展示时只用文件名，避免包含子目录作为出处
            wife_name = os.path.splitext(os.path.basename(img))[0]
            wife_name = restore_special_chars(wife_name)
            src = wife_name.split("#", 1)[0] if "#" in wife_name else ""
            write_group_config(gid, uid, img, today, nick, cfg)
            frg = freeze_records.setdefault(gid, {})
            frg[uid] = {"until": add_days(today, 2), "img": img}
            save_freeze_records()
            # 取消交换请求
            cancel_msg = await self.cancel_swap_on_wife_change(gid, [uid])
            text = f"{nick}，两天内第5次使用牛老婆，已强制更换为来自《{src}》的原始生命态尼比鲁，并锁定两天。"
            path = os.path.join(IMG_DIR, img)
            if os.path.exists(path):
                chain = [Plain(text), Image.fromFileSystem(path)]
            else:
                chain = [Plain(text), Image.fromURL(self.image_base_url + img)]
            try:
                yield event.chain_result(chain)
            except Exception:
                yield event.plain_result(text)
            if cancel_msg:
                yield event.plain_result(cancel_msg)
            return

        rec["count"] += 1
        grp[uid] = rec
        save_ntr_records()
        if random.random() < self.ntr_possibility:
            wife = cfg[tid][0]
            del cfg[tid]
            cfg.pop(uid, None)
            write_group_config(gid, uid, wife, today, nick, cfg)
            # 检查并取消相关交换请求
            cancel_msg = await self.cancel_swap_on_wife_change(gid, [uid, tid])
            yield event.plain_result(f"{nick}，牛老婆成功！")
            # 更新被牛次数
            name = os.path.splitext(wife)[0]
            source, chara = name.split("#", 1)
            ntr_count = update_ntr_count(chara)
            print(f"{chara} 被牛的次数: {ntr_count}")
            if cancel_msg:
                yield event.plain_result(cancel_msg)
            # # 立即为新老婆抽取并展示
            # async for res in self.search_wife(event):
            #     yield res
        else:
            try:
                await event.bot.set_group_ban(group_id=int(gid), user_id=int(uid), duration=self.reset_mute_duration)
            except Exception:
                pass
            rem = self.ntr_max - rec["count"]
            yield event.plain_result(f"{nick}，失败！剩余次数{rem}，已被禁言{self.reset_mute_duration}秒。")

    async def search_wife(self, event: AstrMessageEvent):
        # 查老婆主逻辑
        gid = str(event.message_obj.group_id)
        tid = self.parse_target(event) or str(event.get_sender_id())
        today = get_today()
        cfg = load_group_config(gid)
        if tid not in cfg or cfg[tid][1] != today:
            yield event.plain_result("没有找到有效的老婆信息")
            return
        img = cfg[tid][0]
        # 只取文件名部分用于展示，避免包含子目录（如img1）
        name = os.path.splitext(os.path.basename(img))[0]
        owner = cfg[tid][2]
        # 新增：解析出处和角色名，分隔符为#
        # 统一还原特殊字符，展示与“换老婆”保持一致
        name = restore_special_chars(name)
        if "#" in name:
            source, chara = name.split("#", 1)
            source = restore_special_chars(source)
            chara = restore_special_chars(chara)
            stats = get_chara_stats(chara)
            pure_chara = get_pure_chara_name(chara)
            text = f'{owner}，你今天的老婆是来自《{source}》的{pure_chara}\n被抽中的次数: {stats["pick_count"]}\n被牛的次数: {stats["ntr_count"]}'
        else:
            text = f"{owner}的老婆是{name}"
        path = os.path.join(IMG_DIR, img)
        chain = [Plain(text), Image.fromFileSystem(path) if os.path.exists(path) else Image.fromURL(self.image_base_url + img)]
        try:
            yield event.chain_result(chain)
        except Exception:
            yield event.plain_result(text)

    async def switch_ntr(self, event: AstrMessageEvent):
        # 切换NTR开关，全局管理员、群管理员和群主可用
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()

        # 检查是否是全局管理员
        if uid in self.admins:
            # 全局管理员直接通过验证
            pass
        else:
            # 尝试获取群信息
            try:
                # 检查event是否有get_group方法
                if hasattr(event, "get_group"):
                    try:
                        group = await event.get_group()
                        if group:
                            # 检查是否是群主或群管理员
                            user_id = str(uid)

                            # 修复数据类型不匹配问题，将所有ID转换为字符串进行比较
                            group_owner_str = str(group.group_owner)
                            admins_str = [str(admin) for admin in group.group_admins] if group.group_admins else []

                            if user_id == group_owner_str or (admins_str and user_id in admins_str):
                                # 群主或群管理员通过验证
                                pass
                            else:
                                yield event.plain_result(f"{nick}，无权限，仅全局管理员、群管理员和群主可使用此功能")
                                return
                    except Exception:
                        # 尝试直接从event.message_obj获取角色信息
                        if hasattr(event.message_obj, "raw_message") and hasattr(event.message_obj.raw_message, "sender"):
                            sender_info = event.message_obj.raw_message.sender
                            if hasattr(sender_info, "role"):
                                if sender_info.role in ["owner", "admin"]:
                                    # 群主或群管理员通过验证
                                    pass
                                else:
                                    yield event.plain_result(f"{nick}，无权限，仅全局管理员、群管理员和群主可使用此功能")
                                    return
                        else:
                            yield event.plain_result(f"{nick}，无法获取用户角色信息，拒绝操作")
                            return
                else:
                    # 尝试直接从event.message_obj获取角色信息
                    if hasattr(event.message_obj, "raw_message") and hasattr(event.message_obj.raw_message, "sender"):
                        sender_info = event.message_obj.raw_message.sender
                        if hasattr(sender_info, "role"):
                            if sender_info.role in ["owner", "admin"]:
                                # 群主或群管理员通过验证
                                pass
                            else:
                                yield event.plain_result(f"{nick}，无权限，仅全局管理员、群管理员和群主可使用此功能")
                                return
                    else:
                        yield event.plain_result(f"{nick}，当前平台不支持群权限检查")
                        return
            except Exception as e:
                yield event.plain_result(f"{nick}，获取群信息失败: {str(e)}")
                return
        ntr_statuses[gid] = not ntr_statuses.get(gid, False)
        save_ntr_statuses()
        load_ntr_statuses()
        state = "开启" if ntr_statuses[gid] else "关闭"
        yield event.plain_result(f"{nick}，NTR已{state}")

    async def change_wife(self, event: AstrMessageEvent):
        # 换老婆主逻辑
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        today = get_today()
        cfg = load_group_config(gid)
        # 冻结保护期：禁止更换老婆
        load_freeze_records()
        is_frozen, fr_self = is_freeze_active_for(gid, uid, today)
        if is_frozen:
            yield event.plain_result(f"{nick}，你当前老婆处于两天保护期，暂时不能更换哦！")
            return
        recs = change_records.setdefault(gid, {})
        rec = recs.get(uid, {"date":"","count":0})
        if rec["date"] == today and rec["count"] >= self.change_max_per_day:
            yield event.plain_result(f"{nick}，今天已经换过{self.change_max_per_day}次老婆啦！")
            return
        if uid not in cfg or cfg[uid][1] != today:
            yield event.plain_result(f"{nick}，今天还没有老婆可以换哦！")
            return
        # 更新离婚次数
        tid = self.parse_target(event) or str(event.get_sender_id())
        img = cfg[tid][0]
        name = os.path.splitext(img)[0]
        source, chara = name.split("#", 1)
        divorce_count = update_divorce_count(chara)
        print(f"{chara} 离婚的次数: {divorce_count}")

        del cfg[uid]
        with open(os.path.join(CONFIG_DIR, f"{gid}.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
        if rec["date"] != today:
            rec = {"date": today, "count": 1}
        else:
            rec["count"] += 1
        recs[uid] = rec
        save_change_records()
        # 检查并取消相关交换请求
        cancel_msg = await self.cancel_swap_on_wife_change(gid, [uid])
        if cancel_msg:
            yield event.plain_result(cancel_msg)
        # 获取当前老婆的角色名，用于排除
        current_chara = chara

        # 定义一个临时的抽取方法，排除当前角色
        async def animewife_with_exclusion(event):
            gid = str(event.message_obj.group_id)
            uid = str(event.get_sender_id())
            nick = event.get_sender_name()
            today = get_today()
            cfg = load_group_config(gid)

            if uid not in cfg:
                _ensure_img_dir()
                local_imgs = _list_image_files(IMG_DIR)
                if not local_imgs:
                    yield event.plain_result(f"{nick}，老婆图库为空，请把图片放到：{IMG_DIR}")
                    return

                # 排除当前角色的图片
                filtered_imgs = []
                for img in local_imgs:
                    name = os.path.splitext(img)[0]
                    if "#" in name:
                        _, chara = name.split("#", 1)
                        pure_chara = get_pure_chara_name(chara)
                        # 如果不是当前角色，添加到过滤后的列表
                        if pure_chara != get_pure_chara_name(current_chara):
                            filtered_imgs.append(img)
                    else:
                        filtered_imgs.append(img)

                # 如果过滤后没有图片了，就使用所有图片
                if not filtered_imgs:
                    filtered_imgs = local_imgs

                # 检查用户是否有许愿
                wish_chara = get_user_wish(gid, uid)
                if wish_chara:
                    # 筛选出包含许愿角色的图片
                    wish_imgs = []
                    other_imgs = []
                    for img in filtered_imgs:
                        name = os.path.splitext(img)[0]
                        # 还原文件名中的特殊字符
                        name = restore_special_chars(name)
                        if "#" in name:
                            source, chara = name.split("#", 1)
                            pure_chara = get_pure_chara_name(chara)

                            # 检查是否精确匹配来源#角色
                            if "#" in wish_chara:
                                if wish_chara == f"{source}#{pure_chara}":
                                    wish_imgs.append(img)
                                elif wish_chara in pure_chara:
                                    pass
                            else:
                                if wish_chara in pure_chara:
                                    wish_imgs.append(img)
                                else:
                                    other_imgs.append(img)
                        else:
                            other_imgs.append(img)

                    # 如果有匹配的图片，增加概率
                    if wish_imgs:
                        # 50%概率从许愿图片中选，50%从其他图片中选
                        if not other_imgs or random.random() < 0.5:
                            img = random.choice(wish_imgs)
                        else:
                            img = random.choice(other_imgs)
                    else:
                        img = random.choice(filtered_imgs)
                else:
                    img = random.choice(filtered_imgs)

                cfg[uid] = [img, today, nick]
                # 给特定用户的特殊处理
                if uid == "1457542224":
                    # 从特殊列表中排除当前角色
                    special_imgs = ["魔女之旅#伊蕾娜.jpg","魔女之旅#伊蕾娜.jpeg","Angel Beats#立华奏.jpeg","Angel Beats#立华奏.jpg","我的青春恋爱物语果然有问题#雪之下雪乃.png","某科学的超电磁炮#食蜂操祈.jpeg","某科学的超电磁炮#食蜂操祈.jpg"]
                    filtered_special = []
                    for img in special_imgs:
                        name = os.path.splitext(img)[0]
                        if "#" in name:
                            _, chara = name.split("#", 1)
                            pure_chara = get_pure_chara_name(chara)
                            if pure_chara != get_pure_chara_name(current_chara):
                                filtered_special.append(img)
                    if filtered_special:
                        img = random.choice(filtered_special)
                        cfg[uid] = [img, today, nick]

                write_group_config(gid, uid, img, today, nick, cfg)

                # 解析出处和角色名
                # 只用文件名部分作为标题，避免包含子目录名
                name = os.path.splitext(os.path.basename(img))[0]
                # 还原文件名中的特殊字符
                name = restore_special_chars(name)
                if "#" in name:
                    source, chara = name.split("#", 1)
                    # 还原特殊字符
                    source = restore_special_chars(source)
                    chara = restore_special_chars(chara)
                    # 更新抽中次数
                    update_pick_count(chara)
                    stats = get_chara_stats(chara)
                    # 显示纯角色名
                    pure_chara = get_pure_chara_name(chara)
                    text = f'{nick}，你今天的新老婆是来自《{source}》的{pure_chara}\n被抽中的次数: {stats["pick_count"]}\n被牛的次数: {stats["ntr_count"]}'
                else:
                    # 还原特殊字符
                    name = restore_special_chars(name)
                    text = f"{nick}，你今天的新老婆是{name}"

                path = os.path.join(IMG_DIR, img)
                if os.path.exists(path):
                    chain = [Plain(text), Image.fromFileSystem(path)]
                else:
                    chain = [Plain(text), Image.fromURL(self.image_base_url + img)]
                try:
                    yield event.chain_result(chain)
                except Exception:
                    yield event.plain_result(text)
            else:
                # 如果用户已经有老婆（理论上不应该发生，因为已经删除了）
                img = cfg[uid][0]
                text = f"{nick}，更换老婆失败，请稍后再试"
                path = os.path.join(IMG_DIR, img)
                if os.path.exists(path):
                    chain = [Plain(text), Image.fromFileSystem(path)]
                else:
                    chain = [Plain(text), Image.fromURL(self.image_base_url + img)]
                try:
                    yield event.chain_result(chain)
                except Exception:
                    yield event.plain_result(text)

        # 使用带排除功能的抽取方法
        async for res in animewife_with_exclusion(event):
            yield res

    async def reset_ntr(self, event: AstrMessageEvent):
        # 重置牛老婆次数 - 管理员、群管理员和群主可使用，次数无限制
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()

        # 检查是否是全局管理员、群管理员或群主
        if uid in self.admins:
            # 全局管理员直接通过验证
            pass
        else:
            # 尝试获取群信息检查是否为群管理员或群主
            try:
                # 检查event是否有get_group方法
                if hasattr(event, "get_group"):
                    try:
                        group = await event.get_group()
                        if group:
                            # 检查是否是群主或群管理员
                            user_id = str(uid)

                            # 修复数据类型不匹配问题，将所有ID转换为字符串进行比较
                            group_owner_str = str(group.group_owner)
                            admins_str = [str(admin) for admin in group.group_admins] if group.group_admins else []

                            if user_id == group_owner_str or (admins_str and user_id in admins_str):
                                # 群主或群管理员通过验证
                                pass
                            else:
                                yield event.plain_result(f"{nick}，无权限，仅全局管理员、群管理员和群主可使用此功能")
                                return
                    except Exception:
                        # 尝试直接从event.message_obj获取角色信息
                        if hasattr(event.message_obj, "raw_message") and hasattr(event.message_obj.raw_message, "sender"):
                            sender_info = event.message_obj.raw_message.sender
                            if hasattr(sender_info, "role"):
                                if sender_info.role in ["owner", "admin"]:
                                    # 群主或群管理员通过验证
                                    pass
                                else:
                                    yield event.plain_result(f"{nick}，无权限，仅全局管理员、群管理员和群主可使用此功能")
                                    return
                        else:
                            yield event.plain_result(f"{nick}，当前平台不支持群权限检查")
                        return
            except Exception:
                yield event.plain_result(f"{nick}，权限检查过程中发生错误，拒绝操作")
                return

        # 解析要重置的目标用户
        tid = self.parse_at_target(event) or uid
        if gid in ntr_records and tid in ntr_records[gid]:
            del ntr_records[gid][tid]
            save_ntr_records()
        chain = [Plain("管理员操作：已重置"), At(qq=int(tid)), Plain("的牛老婆次数。")]
        yield event.chain_result(chain)

    async def show_wife_help(self, event: AstrMessageEvent):
        # 显示抽老婆帮助信息
        help_text = "💫 抽老婆插件使用帮助 💫\n\n"
        help_text += "📌 基础功能：\n"
        help_text += "抽老婆 - 抽取今日二次元老婆\n"
        help_text += "查老婆 - 查看自己或他人的老婆\n"
        help_text += "换老婆 - 更换今天的老婆（每日有次数限制）\n\n"
        help_text += "💕 互动功能：\n"
        help_text += "牛老婆 - 尝试抢走他人的老婆（失败会被禁言）\n"
        help_text += "交换老婆 - 向他人发起交换老婆请求\n"
        help_text += "同意交换 - 同意他人的交换请求\n"
        help_text += "拒绝交换 - 拒绝他人的交换请求\n"
        help_text += "查看交换请求 - 查看收到的交换请求\n\n"
        help_text += "⚙️ 群管理功能（群管理员/群主）：\n"
        help_text += "切换ntr开关状态 - 开启或关闭牛老婆功能\n"
        help_text += "重置抽老婆 - 重置指定用户的抽老婆次数\n"
        help_text += "重置牛 - 重置指定用户的牛老婆次数（无次数限制）\n\n"
        help_text += "👑 机器人管理功能（机器人管理员）：\n"
        help_text += "添加老婆 - 添加新的老婆角色\n"
        help_text += "管理添加老婆权限 - 管理谁可以添加新老婆\n\n"
        help_text += "🎯 高级功能：\n"
        help_text += "许愿老婆 - 设置想要抽中的角色\n"
        help_text += "重置许愿 - 清除已设置的角色许愿\n"
        help_text += "搜索老婆库 - 搜索老婆库中的角色\n"
        help_text += "全部展示 - 在搜索后发送全部匹配的图片\n\n"
        help_text += "🎮 快乐抽老婆，每天都有新体验！\n"
        help_text += "💡 提示：查老婆和牛老婆可@指定用户或输入用户名"
        yield event.plain_result(help_text)

    async def search_wife_lib(self, event: AstrMessageEvent):
        # 搜索老婆库功能
        text = event.message_str.strip()
        # 提取搜索关键词
        if text.startswith("搜索老婆库"):
            keyword = text[6:].strip()  # 6是"搜索老婆库"的长度
            if not keyword:
                yield event.plain_result("请输入搜索关键词，例如：搜索老婆库 丛雨")
                return

            try:
                # 获取老婆库中的所有图片文件
                all_files, all_files_lower = _get_img_files_cached()
                # 过滤出包含关键词的文件
                keyword_lower = keyword.lower()
                matched_files = [
                    f
                    for f, fl in zip(all_files, all_files_lower, strict=False)
                    if keyword_lower in fl
                ]

                if not matched_files:
                    yield event.plain_result(f"未找到包含 '{keyword}' 的老婆图片")
                else:
                    # 按文件名排序
                    matched_files.sort()
                    # 构建结果消息
                    restored_files = [restore_special_chars(file) for file in matched_files]
                    result_msg = f"找到 {len(matched_files)} 个包含 '{keyword}' 的老婆图片：\n" + "\n".join(restored_files)
                    # 保存搜索结果到用户ID对应的列表
                    user_id = str(event.get_sender_id())
                    self.search_results[user_id] = matched_files  # 保存原始文件名用于后续操作
                    # 提示用户可以使用全部展示命令
                    result_msg += "\n\n输入 '全部展示' 可以查看所有匹配的图片"
                    # 确保搜索结果在内存中保留足够长的时间
                    self.search_results[user_id] = matched_files
                    # 如果结果太长，进行分段发送
                    if len(result_msg) > 1000:
                        # 先发送总数
                        yield event.plain_result(f"找到 {len(matched_files)} 个包含 '{keyword}' 的老婆图片，将分批展示：")
                        # 分批发送文件名，每批最多50个
                        batch_size = 50
                        for i in range(0, len(matched_files), batch_size):
                            batch_files = matched_files[i:i+batch_size]
                            restored_batch = [restore_special_chars(file) for file in batch_files]
                            batch_msg = "\n".join(restored_batch)
                            yield event.plain_result(batch_msg)
                        # 提示用户可以使用全部展示命令
                        yield event.plain_result("输入 '全部展示' 可以查看所有匹配的图片")
                    else:
                        yield event.plain_result(result_msg)
            except Exception as e:
                yield event.plain_result(f"搜索失败：{str(e)}")
        else:
            yield event.plain_result("请使用格式：搜索老婆库 [关键词]")

    async def show_all_search_results(self, event: AstrMessageEvent):
        # 全部展示功能 - 先发送图片给自己，然后合并转发
        user_id = str(event.get_sender_id())
        bot_self_id = str(event.get_self_id())  # 获取机器人自己的ID

        # 检查用户是否有搜索结果
        if user_id not in self.search_results or not self.search_results[user_id]:
            yield event.plain_result("请先使用 '搜索老婆库 [关键词]' 进行搜索")
            return

        matched_files = self.search_results[user_id]
        total_count = len(matched_files)

        try:
            # 发送总数提示
            yield event.plain_result(f"正在准备 {total_count} 张图片，采用合并转发方式展示，请稍候...")

            # 创建转发消息节点列表
            nodes = []

            # 分批处理图片，每批最多5张
            batch_size = 5
            valid_image_count = 0  # 记录有效的图片数量

            for i in range(0, total_count, batch_size):
                batch_files = matched_files[i:i+batch_size]

                # 构建消息内容
                content = []
                batch_valid_count = 0  # 记录本批次有效的图片数量

                for file in batch_files:
                    file_path = os.path.join(IMG_DIR, file)
                    # 构造与“换老婆”一致的展示标题
                    base_name = os.path.splitext(file)[0]
                    base_name = restore_special_chars(base_name)
                    if "#" in base_name:
                        src, ch = base_name.split("#", 1)
                        src = restore_special_chars(src)
                        ch = restore_special_chars(ch)
                        display_title = f"来自《{src}》的{get_pure_chara_name(ch)}"
                    else:
                        display_title = base_name

                    if os.path.exists(file_path):
                        # 添加图片说明和图片
                        content.append(Plain(f"{display_title}\n"))
                        try:
                            image_component = Image.fromFileSystem(file_path)
                            content.append(image_component)
                            valid_image_count += 1
                            batch_valid_count += 1
                        except Exception as e:
                            content.append(Plain(f"{display_title}（图片加载失败：{str(e)}）\n"))
                    else:
                        content.append(Plain(f"{display_title}（文件不存在）\n"))

                # 只有当content不为空时才创建节点
                if content:
                    # 创建转发节点
                    node = Node(
                        name="机器人自己",  # 转发消息中显示的发送者昵称
                        uin=bot_self_id,    # 使用机器人自己的ID
                        content=content     # 消息内容（图片和说明）
                    )
                    nodes.append(node)

            # 检查nodes是否为空
            if not nodes:
                yield event.plain_result("没有找到有效的图片可以发送")
                return

            # 创建"发送完成"提示节点并添加到nodes列表
            completion_node = Node(
                name="机器人自己",
                uin=bot_self_id,
                content=[Plain(f"合并转发发送完成，共处理{total_count}张图片，成功加载{valid_image_count}张")]
            )
            nodes.append(completion_node)

            # 再次检查nodes列表内容，确保所有node都有有效的content
            validated_nodes = []
            for node in nodes:
                # 确保node的content不为空且包含至少一个有效组件
                if node.content and any(isinstance(comp, (Plain, Image)) for comp in node.content):
                    validated_nodes.append(node)

            if not validated_nodes:
                yield event.plain_result("验证后没有找到有效的消息节点，无法发送合并转发消息")
                return

            # 创建合并转发消息，使用经过验证的nodes列表
            forward_nodes = Nodes(nodes=validated_nodes)

            # 发送合并转发消息
            try:
                # 使用AstrMessageEvent类的chain_result方法来正确设置消息链
                # 这是发送合并转发消息的正确方式
                yield event.chain_result([forward_nodes])
            except Exception as e:
                # 如果合并转发失败，回退到原来的分批发送方式
                fallback_msg = f"合并转发失败，将使用普通方式发送：{str(e)}"
                yield event.plain_result(fallback_msg)

                # 分批发送图片，每批最多5张
                fallback_success_count = 0

                for i in range(0, total_count, batch_size):
                    batch_files = matched_files[i:i+batch_size]
                    chain = []

                    for file in batch_files:
                        file_path = os.path.join(IMG_DIR, file)
                        # 统一标题格式
                        # 只用文件名部分作为标题，避免包含子目录名
                        base_name = os.path.splitext(os.path.basename(file))[0]
                        base_name = restore_special_chars(base_name)
                        if "#" in base_name:
                            src, ch = base_name.split("#", 1)
                            src = restore_special_chars(src)
                            ch = restore_special_chars(ch)
                            display_title = f"来自《{src}》的{get_pure_chara_name(ch)}"
                        else:
                            display_title = base_name

                        if os.path.exists(file_path):
                            chain.append(Plain(f"{display_title}\n"))
                            try:
                                image_component = Image.fromFileSystem(file_path)
                                chain.append(image_component)
                                fallback_success_count += 1
                            except Exception as e:
                                chain.append(Plain(f"{display_title}（图片加载失败：{str(e)}）\n"))
                        else:
                            chain.append(Plain(f"{display_title}（文件不存在）\n"))

                    if chain:
                        try:
                            yield event.chain_result(chain)
                        except Exception as e:
                            error_msg = f"发送图片失败：{str(e)}"
                            yield event.plain_result(error_msg)

                yield event.plain_result(f"普通方式发送完成，共处理{total_count}张图片，成功发送{fallback_success_count}张")
        except Exception as e:
            error_msg = f"展示失败：{str(e)}"
            yield event.plain_result(error_msg)

    async def reset_pick_wife(self, event: AstrMessageEvent):
        # 重置抽老婆次数（管理员专用）
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()

        # 检查是否是全局管理员、群管理员或群主
        if uid in self.admins:
            # 全局管理员直接通过验证
            pass
        else:
            # 尝试获取群信息检查是否为群管理员或群主
            try:
                # 检查event是否有get_group方法
                if hasattr(event, "get_group"):
                    try:
                        group = await event.get_group()
                        if group:
                            # 检查是否是群主或群管理员
                            user_id = str(uid)

                            # 修复数据类型不匹配问题，将所有ID转换为字符串进行比较
                            group_owner_str = str(group.group_owner)
                            admins_str = [str(admin) for admin in group.group_admins] if group.group_admins else []

                            if user_id == group_owner_str or (admins_str and user_id in admins_str):
                                # 群主或群管理员通过验证
                                pass
                            else:
                                yield event.plain_result(f"{nick}，无权限，仅全局管理员、群管理员和群主可使用此功能")
                                return
                    except Exception:
                        # 尝试直接从event.message_obj获取角色信息
                        if hasattr(event.message_obj, "raw_message") and hasattr(event.message_obj.raw_message, "sender"):
                            sender_info = event.message_obj.raw_message.sender
                            if hasattr(sender_info, "role"):
                                if sender_info.role in ["owner", "admin"]:
                                    # 群主或群管理员通过验证
                                    pass
                                else:
                                    yield event.plain_result(f"{nick}，无权限，仅全局管理员、群管理员和群主可使用此功能")
                                    return
                        else:
                            yield event.plain_result(f"{nick}，当前平台不支持群权限检查")
                        return
            except Exception as e:
                yield event.plain_result(f"{nick}，获取群信息失败: {str(e)}")
                return

        # 解析要重置的目标用户
        tid = self.parse_at_target(event)
        if not tid:
            yield event.plain_result("请在命令后@要重置抽老婆次数的用户")
            return

        # 加载群组配置
        cfg = load_group_config(gid)

        # 检查目标用户是否有抽老婆记录
        if tid in cfg:
            # 删除用户的抽老婆记录
            del cfg[tid]
            # 保存更新后的配置
            path = os.path.join(CONFIG_DIR, f"{gid}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=4)

            # 重置换老婆次数
            if gid in swap_limit_records and tid in swap_limit_records[gid]:
                del swap_limit_records[gid][tid]
                save_swap_limit_records()

            # 重置换老婆次数
            if gid in change_records and tid in change_records[gid]:
                del change_records[gid][tid]
                save_change_records()
            # 检查并取消相关交换请求
            cancel_msg = await self.cancel_swap_on_wife_change(gid, [tid])
            # 发送成功消息
            chain = [Plain("管理员操作：已重置"), At(qq=int(tid)), Plain("的抽老婆次数，可以重新抽老婆了。")]
            yield event.chain_result(chain)
            # 如果有取消的交换请求，提示用户
            if cancel_msg:
                yield event.plain_result(cancel_msg)
        else:
            # 目标用户今天没有抽过老婆
            yield event.plain_result("该用户今天没有抽过老婆，无需重置")

    async def swap_wife(self, event: AstrMessageEvent):
        # 发起交换老婆请求
        gid  = str(event.message_obj.group_id)
        uid  = str(event.get_sender_id())
        tid  = self.parse_at_target(event)
        nick = event.get_sender_name()
        today = get_today()

        grp_limit = swap_limit_records.setdefault(gid, {})
        rec_lim  = grp_limit.get(uid, {"date":"","count":0})
        if rec_lim["date"] != today:
            rec_lim = {"date": today, "count": 0}
        if rec_lim["count"] >= self.swap_max_per_day:
            yield event.plain_result(f"{nick}，今天已发起{self.swap_max_per_day}次请求，明天再来~")
            return

        if not tid or tid == uid:
            yield event.plain_result(f"{nick}，请在命令后 @ 另一位用户。")
            return

        cfg = load_group_config(gid)
        for x in (uid, tid):
            if x not in cfg or cfg[x][1] != today:
                who = nick if x == uid else "对方"
                yield event.plain_result(f"{who}，今天还没有老婆，无法交换。")
                return
        # 冻结保护期：任一方在保护期内不可交换
        load_freeze_records()
        for x in (uid, tid):
            frozen, _ = is_freeze_active_for(gid, x, today)
            if frozen:
                who = "你" if x == uid else "对方"
                yield event.plain_result(f"{nick}，{who}的老婆处于两天保护期，无法交换。")
                return

        rec_lim["count"] += 1
        grp_limit[uid] = rec_lim
        save_swap_limit_records()

        grp = swap_requests.setdefault(gid, {})
        grp[uid] = {"target": tid, "date": today}
        save_swap_requests()

        yield event.chain_result([
            Plain(f"{nick} 请求与 "), At(qq=int(tid)),
            Plain(" 交换老婆，请对方用“同意交换 @发起者”或“拒绝交换 @发起者”。")
        ])

    async def agree_swap_wife(self, event: AstrMessageEvent):
        # 同意交换老婆
        gid  = str(event.message_obj.group_id)
        tid  = str(event.get_sender_id())
        uid  = self.parse_at_target(event)
        nick = event.get_sender_name()
        today = get_today()

        grp = swap_requests.get(gid, {})
        rec = grp.get(uid)
        if not rec or rec.get("target") != tid:
            yield event.plain_result(f'{nick}，请在命令后@发起者，或通过"查看交换请求"查看当前请求。')
            return

        # 冻结保护期检查
        load_freeze_records()
        for x in (uid, tid):
            frozen, _ = is_freeze_active_for(gid, x, today)
            if frozen:
                who = "发起者" if x == uid else "你"
                yield event.plain_result(f"{nick}，{who}的老婆处于两天保护期，暂时不能交换。")
                return

        cfg = load_group_config(gid)
        img_u, date_u, nick_u = cfg[uid]
        img_t, date_t, nick_t = cfg[tid]
        cfg[uid][0]  = img_t
        cfg[tid][0]  = img_u
        save_json(os.path.join(CONFIG_DIR, f"{gid}.json"), cfg)

        del grp[uid]
        save_swap_requests()

        # 检查并取消相关交换请求
        cancel_msg = await self.cancel_swap_on_wife_change(gid, [uid, tid])
        yield event.plain_result("交换成功！")
        if cancel_msg:
            yield event.plain_result(cancel_msg)

    async def reject_swap_wife(self, event: AstrMessageEvent):
        # 拒绝交换老婆
        gid  = str(event.message_obj.group_id)
        tid  = str(event.get_sender_id())
        uid  = self.parse_at_target(event)
        nick = event.get_sender_name()

        grp = swap_requests.get(gid, {})
        rec = grp.get(uid)
        if not rec or rec.get("target") != tid:
            yield event.plain_result(f'{nick}，请在命令后@发起者，或通过"查看交换请求"查看当前请求。')
            return

        del grp[uid]
        save_swap_requests()
        yield event.chain_result([At(qq=int(uid)), Plain("，对方拒绝了交换请求。")])

    async def view_swap_requests(self, event: AstrMessageEvent):
        """
        查看当前用户发起或@到自己的交换请求：
          - 如果既没发起也没人向自己发起，提示未找到请求；
          - 否则列出所有相关请求，并提示使用“同意交换 @发起者”或“拒绝交换 @发起者”。
          发起时展示目标用户昵称，收到时展示发起者昵称。
        """
        gid = str(event.message_obj.group_id)
        me = str(event.get_sender_id())

        grp = swap_requests.get(gid, {})
        cfg = load_group_config(gid)

        sent_targets = [rec["target"] for uid, rec in grp.items() if uid == me]
        received_from = [uid for uid, rec in grp.items() if rec.get("target") == me]

        if not sent_targets and not received_from:
            yield event.plain_result("未找到交换请求")
            return

        parts = []
        for tid in sent_targets:
            name = cfg.get(tid, [None, None, "未知用户"])[2]
            parts.append(f"→ 我发起给 {name} 的请求")
        for uid in received_from:
            name = cfg.get(uid, [None, None, "未知用户"])[2]
            parts.append(f"→ {name} 发起给我的请求")

        text = (
            "当前交换请求：\n"
            + "\n".join(parts)
            + "\n请在“同意交换”或“拒绝交换”命令后 @ 发起者"
        )
        yield event.plain_result(text)

    async def cancel_swap_on_wife_change(self, gid, user_ids):
        """
        检查并取消与user_ids相关的交换请求，返还交换次数，并返回提示文本（如有）。
        """
        changed = False
        today = get_today()
        grp = swap_requests.get(gid, {})
        grp_limit = swap_limit_records.setdefault(gid, {})
        to_cancel = []
        for req_uid, req in grp.items():
            if req_uid in user_ids or req.get("target") in user_ids:
                to_cancel.append(req_uid)
        for req_uid in to_cancel:
            # 返还次数
            rec_lim = grp_limit.get(req_uid, {"date": "", "count": 0})
            if rec_lim.get("date") == today and rec_lim.get("count", 0) > 0:
                rec_lim["count"] = max(0, rec_lim["count"] - 1)
                grp_limit[req_uid] = rec_lim
                changed = True
            del grp[req_uid]
        if to_cancel:
            save_swap_requests()
        if changed:
            save_swap_limit_records()
        # 返回提示文本，由调用方 yield
        if to_cancel:
            return "交换对象老婆已变更，取消交换请求并返还次数。"
        return None

    async def wish_wife(self, event: AstrMessageEvent):
        # 许愿老婆主逻辑
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()
        text = event.message_str.strip()

        # 解析许愿内容
        if text.startswith("许愿老婆") and len(text) > 4:
            wish_chara = text[4:].strip()
            alias_key = normalize_alias_input(wish_chara)
            alias_target = None
            for chara, aliases in WIFE_ALIAS_LIST:
                if alias_key == normalize_alias_input(chara):
                    alias_target = chara
                    break
                for alias in aliases:
                    if alias_key == normalize_alias_input(alias):
                        alias_target = chara
                        break
                if alias_target:
                    break
            if alias_target:
                wish_chara = alias_target

            # 确保许愿内容不为空
            if not wish_chara:
                yield event.plain_result("请使用格式：许愿老婆XXX")
                return

            # 检查角色是否在图片库中
            _ensure_img_dir()
            local_imgs = _list_image_files(IMG_DIR)
            if not local_imgs:
                yield event.plain_result(f"{nick}，老婆图库为空，请把图片放到：{IMG_DIR}")
                return

            candidates = set()

            for img in local_imgs:
                name = os.path.splitext(img)[0]
                # 还原文件名中的特殊字符
                name = restore_special_chars(name)
                if "#" in name:
                    source, chara = name.split("#", 1)
                    pure_chara = get_pure_chara_name(chara)

                    # 构造完整标识
                    full_id = f"{source}#{pure_chara}"

                    # 匹配逻辑
                    if wish_chara.lower() in pure_chara.lower():
                        candidates.add(full_id)
                    elif wish_chara.lower() in full_id.lower():
                        candidates.add(full_id)

            sorted_candidates = sorted(candidates, key=lambda x: len(x))

            if not sorted_candidates:
                yield event.plain_result(f"{nick}，{wish_chara}不在图片库中")
                return

            # 过滤出精确匹配纯名的（如果存在）
            exact_matches = [c for c in sorted_candidates if c.split("#")[1] == wish_chara]

            final_candidates = []
            if exact_matches:
                final_candidates = exact_matches
            else:
                final_candidates = sorted_candidates

            if len(final_candidates) == 1:
                target = final_candidates[0]
                src, pc = target.split("#", 1)
                save_wish(gid, uid, target)
                yield event.plain_result(f"{nick}，许愿成功！抽取{pc}（来自《{src}》）的概率提升至50%")
            else:
                # 多个候选，进入选择模式
                msg = f"{nick}，库里有以下角色符合条件，请回复“许愿+序号”进行选择\n例如：许愿 1\n"

                self.wish_selection_states[uid] = {
                    "candidates": final_candidates,
                    "time": datetime.now().timestamp(),
                }

                for i, cand in enumerate(final_candidates):
                    # cand is source#pure_chara
                    msg += f"{i+1}. {cand}\n"

                yield event.plain_result(msg)
        else:
            yield event.plain_result("请使用格式：许愿老婆XXX")

    async def confirm_wish(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id())
        gid = str(event.message_obj.group_id)
        nick = event.get_sender_name()
        text = event.message_str.strip()

        if not text.startswith("许愿"):
            return

        try:
            idx_str = text[2:].strip()
            if not idx_str:
                return
            idx = int(idx_str)
        except ValueError:
            return

        state = self.wish_selection_states.get(uid)
        if not state:
            yield event.plain_result(f"{nick}，你没有正在进行的许愿选择，请先使用“许愿老婆”指令。")
            return

        # 5分钟超时
        if datetime.now().timestamp() - state["time"] > 300:
            del self.wish_selection_states[uid]
            yield event.plain_result(f"{nick}，选择已超时，请重新许愿。")
            return

        candidates = state["candidates"]
        if 1 <= idx <= len(candidates):
            target = candidates[idx-1]
            src, pc = target.split("#", 1)
            save_wish(gid, uid, target)
            del self.wish_selection_states[uid]
            yield event.plain_result(f"{nick}，许愿成功！抽取{pc}（来自《{src}》）的概率提升至50%")
        else:
            yield event.plain_result(f"{nick}，序号无效，请输入 1-{len(candidates)} 之间的数字。")

    async def reset_wish_wife(self, event: AstrMessageEvent):
        # 重置许愿主逻辑
        gid = str(event.message_obj.group_id)
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()

        # 加载许愿信息
        wishes = load_wishes()

        # 检查用户是否有许愿
        if gid in wishes and uid in wishes[gid]:
            # 删除用户的许愿信息
            del wishes[gid][uid]

            # 如果该群组没有其他许愿信息，删除群组条目
            if not wishes[gid]:
                del wishes[gid]

            # 保存更新后的许愿信息
            with open(WISH_FILE, "w", encoding="utf-8-sig") as f:
                json.dump(wishes, f, ensure_ascii=False, indent=2)

            yield event.plain_result(f"{nick}，你的许愿已重置！")
        else:
            yield event.plain_result(f"{nick}，你当前没有许愿记录")

    async def get_first_image(self, event: AstrMessageEvent):
        # 获取消息中的第一张图片
        # 先检查回复中的图片（参考figurine_workshop的实现方式）
        for s in event.message_obj.message:
            if isinstance(s, Comp.Reply) and hasattr(s, "chain") and s.chain:
                for seg in s.chain:
                    if isinstance(seg, Image):
                        return seg

        # 尝试其他引用消息属性名，增强兼容性
        reply_msg = None
        if hasattr(event.message_obj, "reply") and event.message_obj.reply:
            reply_msg = event.message_obj.reply
        elif hasattr(event.message_obj, "quoted_message") and event.message_obj.quoted_message:
            reply_msg = event.message_obj.quoted_message

        if reply_msg:
            # 检查不同结构的消息组件
            if hasattr(reply_msg, "message"):
                for comp in reply_msg.message:
                    if isinstance(comp, Image):
                        return comp
            elif hasattr(reply_msg, "elements"):
                # 检查是否有elements属性
                for comp in reply_msg.elements:
                    if isinstance(comp, Image):
                        return comp
            elif isinstance(reply_msg, list):
                # 检查是否是直接的组件列表
                for comp in reply_msg:
                    if isinstance(comp, Image):
                        return comp

        # 从当前消息中获取图片
        if hasattr(event.message_obj, "message"):
            for comp in event.message_obj.message:
                # 避免抓取@对象的头像
                if isinstance(comp, Comp.At):
                    continue
                if isinstance(comp, Image):
                    return comp
        elif hasattr(event.message_obj, "elements"):
            for comp in event.message_obj.elements:
                # 避免抓取@对象的头像
                if isinstance(comp, Comp.At):
                    continue
                if isinstance(comp, Image):
                    return comp

        return None

    async def add_wife(self, event: AstrMessageEvent):
        # 添加老婆主逻辑，管理员和有添加老婆权限的用户可用
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()

        # 检查是否为管理员或有添加老婆权限的用户
        if uid not in self.admins and uid not in self.wife_adders:
            yield event.plain_result(f"{nick}，无权限添加老婆")
            return

        # 解析命令内容
        text = event.message_str.strip()
        if not text.startswith("添加老婆"):
            yield event.plain_result("请使用格式：添加老婆 XXX#XXX")
            return

        # 解析名称部分
        name_part = text[4:].strip()
        if not name_part or "#" not in name_part:
            yield event.plain_result("请使用格式：添加老婆 XXX#XXX，其中XXX#XXX为作品名#角色名")
            return

        # 获取图片
        image = await self.get_first_image(event)
        if not image:
            yield event.plain_result("未找到图片，请在消息中包含图片或引用包含图片的消息")
            return

        try:
            # 获取图片内容
            if hasattr(image, "path") and image.path:
                with open(image.path, "rb") as f:
                    img_data = f.read()
            else:
                # 如果没有本地路径，尝试从URL获取
                try:
                    import aiohttp as _aiohttp
                except Exception:
                    _aiohttp = None
                if _aiohttp is not None:
                    async with _aiohttp.ClientSession(trust_env=True) as session:
                        async with session.get(image.url, timeout=20) as resp:
                            img_data = await resp.read()
                else:
                    import urllib.request

                    def _fetch() -> bytes:
                        req = urllib.request.Request(image.url, method="GET")
                        with urllib.request.urlopen(req, timeout=20) as resp:
                            return resp.read()

                    img_data = await asyncio.to_thread(_fetch)

            # 确定文件扩展名
            if hasattr(image, "path") and image.path:
                _, ext = os.path.splitext(image.path)
                if not ext:
                    ext = ".jpg"  # 默认扩展名
            else:
                # 从URL或content_type推断扩展名
                content_type = getattr(image, "content_type", "")
                if "jpeg" in content_type:
                    ext = ".jpg"
                elif "png" in content_type:
                    ext = ".png"
                elif "gif" in content_type:
                    ext = ".gif"
                else:
                    ext = ".jpg"  # 默认扩展名

            # 替换特殊字符
            safe_name = name_part
            for char, replacement in CHAR_MAPPING.items():
                safe_name = safe_name.replace(char, replacement)

            # 保存图片
            filename = f"{safe_name}{ext}"
            filepath = os.path.join(IMG_DIR, filename)

            # 确保文件名不重复
            counter = 1
            while os.path.exists(filepath):
                base_name = safe_name
                filepath = os.path.join(IMG_DIR, f"{base_name}_{counter}{ext}")
                counter += 1

            with open(filepath, "wb") as f:
                f.write(img_data)

            # 显示给用户的名称需要还原特殊字符
            display_name = restore_special_chars(os.path.basename(filepath))

            yield event.plain_result(f"{nick}，添加老婆成功！已保存为：{display_name}")
        except Exception as e:
            yield event.plain_result(f"添加老婆失败：{str(e)}")

    async def manage_wife_adders(self, event: AstrMessageEvent):
        # 管理添加老婆权限的用户列表，仅管理员可用
        uid = str(event.get_sender_id())
        nick = event.get_sender_name()

        # 检查是否为管理员
        if uid not in self.admins:
            yield event.plain_result(f"{nick}，无权限管理添加老婆权限")
            return

        # 解析命令内容
        text = event.message_str.strip()
        if not text.startswith("管理添加老婆权限"):
            yield event.plain_result("请使用格式：管理添加老婆权限 [查看/添加/删除] [QQ号]")
            return

        # 解析操作和QQ号
        parts = text[8:].strip().split()
        if not parts:
            yield event.plain_result("请使用格式：管理添加老婆权限 [查看/添加/删除] [QQ号]")
            return

        operation = parts[0]

        if operation == "查看":
            # 查看当前有添加老婆权限的用户列表
            if not self.wife_adders:
                yield event.plain_result("当前没有额外的添加老婆权限用户")
            else:
                users_text = "\n".join(self.wife_adders)
                yield event.plain_result(f"当前有添加老婆权限的用户：\n{users_text}")
        elif operation == "添加":
            # 添加用户到有添加老婆权限的列表
            if len(parts) < 2:
                yield event.plain_result("请使用格式：管理添加老婆权限 添加 QQ号")
                return

            qq_id = parts[1]
            if not qq_id.isdigit():
                yield event.plain_result("QQ号必须是数字")
                return

            if qq_id in self.wife_adders:
                yield event.plain_result(f"用户 {qq_id} 已经在添加老婆权限列表中")
                return

            self.wife_adders.append(qq_id)
            if self.save_wife_adders():
                yield event.plain_result(f"成功添加用户 {qq_id} 到添加老婆权限列表")
            else:
                yield event.plain_result("保存失败，请重试")
        elif operation == "删除":
            # 从有添加老婆权限的列表中删除用户
            if len(parts) < 2:
                yield event.plain_result("请使用格式：管理添加老婆权限 删除 QQ号")
                return

            qq_id = parts[1]
            if not qq_id.isdigit():
                yield event.plain_result("QQ号必须是数字")
                return

            if qq_id not in self.wife_adders:
                yield event.plain_result(f"用户 {qq_id} 不在添加老婆权限列表中")
                return

            self.wife_adders.remove(qq_id)
            if self.save_wife_adders():
                yield event.plain_result(f"成功从添加老婆权限列表中删除用户 {qq_id}")
            else:
                yield event.plain_result("保存失败，请重试")
