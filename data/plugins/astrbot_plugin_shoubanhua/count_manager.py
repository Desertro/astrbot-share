import asyncio
import datetime
import functools
import json
import random
from pathlib import Path
from astrbot.core.log import LogManager
logger = LogManager.GetLogger('astrbot_plugin_shoubanhua')

class CountManager:

    def __init__(self, plugin_data_dir: Path, config_getter, config_setter):
        plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.user_counts_file = plugin_data_dir / 'user_counts.json'
        self.user_counts: dict[str, int] = {}
        self.group_counts_file = plugin_data_dir / 'group_counts.json'
        self.group_counts: dict[str, int] = {}
        self.user_checkin_file = plugin_data_dir / 'user_checkin.json'
        self.user_checkin_data: dict[str, str] = {}
        self._get_conf = config_getter
        self._set_conf = config_setter

    async def initialize(self):
        await self._load_user_counts()
        await self._load_group_counts()
        await self._load_user_checkin_data()

    async def _load_user_counts(self):
        if not self.user_counts_file.exists():
            self.user_counts = {}
            return
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, self.user_counts_file.read_text, 'utf-8')
            data = await loop.run_in_executor(None, json.loads, content)
            if isinstance(data, dict):
                self.user_counts = {str(k): v for k, v in data.items()}
        except Exception as e:
            logger.error(f'加载用户次数文件时发生错误: {e}', exc_info=True)
            self.user_counts = {}

    async def _save_user_counts(self):
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(None, functools.partial(json.dumps, self.user_counts, ensure_ascii=False, indent=4))
            await loop.run_in_executor(None, self.user_counts_file.write_text, json_data, 'utf-8')
        except Exception as e:
            logger.error(f'保存用户次数文件时发生错误: {e}', exc_info=True)

    def _get_user_count(self, user_id: str) -> int:
        return self.user_counts.get(str(user_id), 0)

    async def _decrease_user_count(self, user_id: str):
        user_id_str = str(user_id)
        count = self._get_user_count(user_id_str)
        if count > 0:
            self.user_counts[user_id_str] = count - 1
            await self._save_user_counts()

    async def _increase_user_count(self, user_id: str, count: int) -> int:
        user_id_str = str(user_id)
        current_count = self._get_user_count(user_id_str)
        new_count = current_count + count
        self.user_counts[user_id_str] = new_count
        await self._save_user_counts()
        return new_count

    async def _load_group_counts(self):
        if not self.group_counts_file.exists():
            self.group_counts = {}
            return
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, self.group_counts_file.read_text, 'utf-8')
            data = await loop.run_in_executor(None, json.loads, content)
            if isinstance(data, dict):
                self.group_counts = {str(k): v for k, v in data.items()}
        except Exception as e:
            logger.error(f'加载群组次数文件时发生错误: {e}', exc_info=True)
            self.group_counts = {}

    async def _save_group_counts(self):
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(None, functools.partial(json.dumps, self.group_counts, ensure_ascii=False, indent=4))
            await loop.run_in_executor(None, self.group_counts_file.write_text, json_data, 'utf-8')
        except Exception as e:
            logger.error(f'保存群组次数文件时发生错误: {e}', exc_info=True)

    def _get_group_count(self, group_id: str) -> int:
        return self.group_counts.get(str(group_id), 0)

    async def _decrease_group_count(self, group_id: str):
        group_id_str = str(group_id)
        count = self._get_group_count(group_id_str)
        if count > 0:
            self.group_counts[group_id_str] = count - 1
            await self._save_group_counts()

    async def _increase_group_count(self, group_id: str, count: int) -> int:
        group_id_str = str(group_id)
        current_count = self._get_group_count(group_id_str)
        new_count = current_count + count
        self.group_counts[group_id_str] = new_count
        await self._save_group_counts()
        return new_count

    async def _load_user_checkin_data(self):
        if not self.user_checkin_file.exists():
            self.user_checkin_data = {}
            return
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, self.user_checkin_file.read_text, 'utf-8')
            data = await loop.run_in_executor(None, json.loads, content)
            if isinstance(data, dict):
                self.user_checkin_data = {str(k): v for k, v in data.items()}
        except Exception as e:
            logger.error(f'加载用户签到文件时发生错误: {e}', exc_info=True)
            self.user_checkin_data = {}

    async def _save_user_checkin_data(self):
        loop = asyncio.get_running_loop()
        try:
            json_data = await loop.run_in_executor(None, functools.partial(json.dumps, self.user_checkin_data, ensure_ascii=False, indent=4))
            await loop.run_in_executor(None, self.user_checkin_file.write_text, json_data, 'utf-8')
        except Exception as e:
            logger.error(f'保存用户签到文件时发生错误: {e}', exc_info=True)

    async def checkin(self, user_id: str) -> tuple[bool, str]:
        """处理用户签到，返回(是否成功, 消息内容)"""
        if not self._get_conf('enable_checkin', False):
            return (False, '📅 本机器人未开启签到功能。')
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        last_checkin_date = self.user_checkin_data.get(user_id)
        if last_checkin_date == today_str:
            current_count = self._get_user_count(user_id)
            return (False, f'您今天已经签到过了，明天再来吧！\n您当前剩余个人次数: {current_count}')
        reward = 0
        is_random_val = self._get_conf('enable_random_checkin', False)
        if str(is_random_val).lower() == 'true':
            max_reward = self._get_conf('checkin_random_reward_max', 5)
            max_reward = max(1, int(max_reward))
            reward = random.randint(1, max_reward)
        else:
            reward = self._get_conf('checkin_fixed_reward', 3)
            reward = int(reward)
        current_count = self._get_user_count(user_id)
        new_count = await self._increase_user_count(user_id, reward)
        self.user_checkin_data[user_id] = today_str
        await self._save_user_checkin_data()
        return (True, f'🎉 签到成功！\n您获得了 {reward} 次个人使用次数。\n当前剩余: {new_count} 次。')

    async def query_counts(self, user_id_to_query: str, group_id: str | None=None, is_admin: bool=False, event=None) -> tuple[bool, str]:
        """查询用户个人次数和群组次数，支持管理员查询他人次数"""
        if is_admin and event:
            at_users = []
            try:
                messages = event.get_messages()
                for msg in messages:
                    if hasattr(msg, 'type') and msg.type == 'At' and hasattr(msg, 'qq'):
                        qq = str(msg.qq)
                        if qq != 'all':
                            at_users.append(qq)
            except Exception as e:
                logger.warning(f'获取At组件时出错: {e}')
            if at_users:
                target_user_id = at_users[0]
                user_count = self._get_user_count(target_user_id)
                reply_msg = f'用户 {target_user_id} 个人剩余次数为: {user_count}'
                if group_id:
                    group_count = self._get_group_count(group_id)
                    reply_msg += f'\n本群共享剩余次数为: {group_count}'
                return (True, reply_msg)
        user_count = self._get_user_count(user_id_to_query)
        reply_msg = f'您的个人剩余次数为: {user_count}'
        if group_id:
            group_count = self._get_group_count(group_id)
            reply_msg += f'\n本群共享剩余次数为: {group_count}'
        return (True, reply_msg)

    async def add_user_counts(self, event) -> tuple[bool, str]:
        """为用户增加使用次数"""
        message_text = event.message_str
        params = message_text.split()
        if len(params) < 2:
            return (False, '❌ 格式错误，请使用：手办化增加用户次数 <QQ号> [次数]')
        target_qq = params[1]
        try:
            count = int(params[2]) if len(params) > 2 else 1
        except ValueError:
            return (False, '❌ 增加次数必须是有效的数字。')
        if count <= 0:
            return (False, f'❌ 增加次数必须大于0，当前值: {count}')
        new_count = await self._increase_user_count(target_qq, count)
        return (True, f'✅ 已为用户 {target_qq} 增加 {count} 次，TA当前剩余 {new_count} 次。')

    async def add_group_counts(self, event) -> tuple[bool, str]:
        """为群组增加使用次数"""
        message_text = str(event.message_str or '').strip()
        params_text = message_text
        command_name = '手办化增加群组次数'
        if params_text.startswith('#'):
            params_text = params_text[1:].lstrip()
        if params_text.startswith(command_name):
            params_text = params_text[len(command_name):].strip()
        params = params_text.split()
        if len(params) < 1:
            return (False, '❌ 格式错误，请使用：手办化增加群组次数 [群组ID] <次数>')
        if len(params) == 1:
            target_group = event.get_group_id()
            if not target_group:
                return (False, '❌ 请在群聊中使用此命令，或同时指定群组ID和次数。')
            try:
                count = int(params[0])
            except ValueError:
                return (False, '❌ 增加次数必须是有效的数字。')
        else:
            target_group = params[0]
            try:
                count = int(params[1])
            except ValueError:
                return (False, '❌ 增加次数必须是有效的数字。')
        if count <= 0:
            return (False, f'❌ 增加次数必须大于0，当前值: {count}')
        new_count = await self._increase_group_count(target_group, count)
        return (True, f'✅ 已为群组 {target_group} 增加 {count} 次，该群当前剩余 {new_count} 次。')

    async def check_and_decrease_quota(self, user_id: str, group_id: str | None=None) -> tuple[bool, str | None]:
        """检查并扣除使用次数，返回(是否可以使用, 错误消息)"""
        enable_user_limit = self._get_conf('enable_user_limit', False)
        enable_group_limit = self._get_conf('enable_group_limit', False)
        user_count = self._get_user_count(user_id)
        group_count = self._get_group_count(group_id) if group_id else 0
        logger.debug(f'用户 {user_id} 的个人次数: {user_count}')
        if group_id:
            logger.debug(f'群组 {group_id} 的次数: {group_count}')
        can_use = True
        error_msg = None
        use_group_quota = False
        use_user_quota = False
        if group_id:
            if enable_group_limit:
                if group_count > 0:
                    use_group_quota = True
                    logger.debug(f'将使用群组次数: 群ID={group_id}, 剩余次数={group_count}')
                elif enable_user_limit:
                    if user_count > 0:
                        use_user_quota = True
                    else:
                        can_use = False
                        error_msg = '❌ 本群和您的个人使用次数均已用尽，请联系管理员补充。'
                else:
                    can_use = False
                    error_msg = '❌ 本群使用次数已用尽，请联系管理员补充。'
            elif enable_user_limit:
                if user_count > 0:
                    use_user_quota = True
                else:
                    can_use = False
                    error_msg = '❌ 您的使用次数已用完，请联系管理员补充。'
            else:
                can_use = True
        elif enable_user_limit:
            if user_count > 0:
                use_user_quota = True
            else:
                can_use = False
                error_msg = '❌ 您的使用次数已用完，请联系管理员补充。'
        if can_use:
            if use_group_quota:
                await self._decrease_group_count(group_id)
            elif use_user_quota:
                await self._decrease_user_count(user_id)
            logger.debug(f'用户 {user_id} 使用了一次功能，剩余个人次数: {self._get_user_count(user_id)}')
            if group_id:
                logger.debug(f'剩余群组次数: {self._get_group_count(group_id)}')
        return (can_use, error_msg)
