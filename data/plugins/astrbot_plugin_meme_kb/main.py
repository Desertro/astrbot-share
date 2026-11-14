"""meme_kb: 让爱丽丝把聊天和你教给她的东西，攒成可检索的梗知识库。

写入是主人专属；读取（#梗 查）谁都能用。
向量用 OpenAI 兼容的 /embeddings（默认复用 mem_chat 的 bge-m3 配置）。
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
try:
    from . import kb
except ImportError:
    import kb
DATA_DIR = Path(__file__).resolve().parents[2]
MEM_CHAT_CONF = DATA_DIR / 'config' / 'astrbot_plugin_mem_chat_config.json'
DEFAULT_DB = DATA_DIR / 'plugin_data' / 'meme_kb' / 'memes.db'

@register('meme_kb', 'dsh', '梗知识库：把聊天和搜索到的东西整理成可检索的梗', '0.1.0')
class MemeKB(Star):

    def __init__(self, context: Context, config: dict | None=None):
        super().__init__(context, config)
        self.conf = config or {}

    def _c(self, key: str, default):
        value = self.conf.get(key, default)
        return default if value is None else value

    def _admin_uids(self) -> set[str]:
        raw = self._c('admin_uids', [])
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.split(',') if x.strip()]
        return {str(x).strip() for x in raw}

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return str(event.get_sender_id()) in self._admin_uids()

    def _db(self) -> Path:
        raw = str(self._c('db_path', '')).strip()
        return Path(raw) if raw else DEFAULT_DB

    def _creds(self) -> tuple[str, str, str, str, str]:
        """Return (embed_base, embed_key, embed_model, chat_base, chat_key)."""
        base = str(self._c('embed_base_url', '')).strip()
        key = str(self._c('embed_api_key', '')).strip()
        model = str(self._c('embed_model', '')).strip()
        chat_base = str(self._c('chat_base_url', '')).strip()
        chat_key = str(self._c('chat_api_key', '')).strip()
        if (not base or not key) and MEM_CHAT_CONF.exists():
            try:
                conf = json.loads(MEM_CHAT_CONF.read_text(encoding='utf-8-sig'))
                base = base or str(conf.get('embedding_openai_base_url', '')).strip()
                key = key or str(conf.get('embedding_openai_api_key', '')).strip()
                model = model or str(conf.get('embedding_openai_model', '')).strip()
                chat_base = chat_base or str(conf.get('llm_base_url', '')).strip()
                chat_key = chat_key or str(conf.get('llm_api_key', '')).strip()
            except Exception:
                pass
        return (base or __import__('share_runtime').resolve('{{AB_MEM_CHAT_EMBEDDING_OPENAI_BASE_URL}}'), key, model or 'BAAI/bge-m3', chat_base or __import__('share_runtime').resolve('{{AB_CMD_PROVIDER_SOURCES_2_API_BASE}}'), chat_key)

    @filter.command('梗')
    async def cmd_meme(self, event: AstrMessageEvent, args: GreedyStr):
        """#梗 记 <内容> / #梗 查 <词> / #梗 列表 / #梗 删 <id>"""
        raw = str(args).strip()
        if not raw:
            yield event.plain_result('用法：\n#梗 记 <一段聊天或解释>   ← 爱丽丝整理成一条梗知识存起来\n#梗 查 <词>               ← 按意思检索，不是关键词匹配\n#梗 列表 / #梗 删 <id>')
            return
        action, _, rest = raw.partition(' ')
        action = action.strip()
        rest = rest.strip()
        embed_base, embed_key, embed_model, chat_base, chat_key = self._creds()
        if action in ('记', '记下', 'add'):
            if not self._is_admin(event):
                yield event.plain_result('这个只有主人能加。')
                return
            if not rest:
                yield event.plain_result('要记什么？')
                return
            if not embed_key or not chat_key:
                yield event.plain_result('缺 embedding / chat 的 key，先看配置。')
                return
            try:
                item = await kb.extract(rest, base_url=chat_base, api_key=chat_key, model=str(self._c('chat_model', 'deepseek-flash')))
                row_id, created, score = await kb.upsert(self._db(), item, base_url=embed_base, api_key=embed_key, model=embed_model, group_id=event.get_group_id() or '', source=str(self._c('default_source', 'chat')), dedupe_score=float(self._c('dedupe_score', 0.92)))
            except Exception as exc:
                self.logger.error(f'[meme_kb] add failed: {exc}')
                yield event.plain_result(f'没存进去：{exc}')
                return
            verb = '记下了' if created else f'并到已有那条（相似度 {score:.2f}）'
            yield event.plain_result(f'{verb}：{item.get('term')}\n意思：{item.get('meaning') or '—'}\n（id={row_id}）')
            return
        if action in ('查', 'search', '找'):
            if not rest:
                yield event.plain_result('查什么？')
                return
            if not embed_key:
                yield event.plain_result('缺 embedding key。')
                return
            try:
                hits = await kb.search(self._db(), rest, base_url=embed_base, api_key=embed_key, model=embed_model, k=int(self._c('search_k', 3)), min_score=float(self._c('min_score', 0.45)))
            except Exception as exc:
                self.logger.error(f'[meme_kb] search failed: {exc}')
                yield event.plain_result(f'查不动：{exc}')
                return
            if not hits:
                yield event.plain_result('库里没有像的。')
                return
            lines = [f'{i + 1}. {h['term']}（{h['score']:.2f}）：{h['meaning']}' for i, h in enumerate(hits)]
            yield event.plain_result('查到这些：\n' + '\n'.join(lines))
            return
        if action in ('列表', 'ls', 'list'):
            con = kb.connect(self._db())
            try:
                rows = con.execute('select id, term, meaning, updated_at from meme order by updated_at desc limit 10').fetchall()
            finally:
                con.close()
            if not rows:
                yield event.plain_result('库还是空的。')
                return
            lines = []
            for rid, term, meaning, ts in rows:
                when = time.strftime('%m-%d %H:%M', time.localtime(ts))
                lines.append(f'#{rid} {term}（{when}）：{str(meaning)[:40]}')
            yield event.plain_result('最近记的：\n' + '\n'.join(lines))
            return
        if action in ('删', 'del', 'rm'):
            if not self._is_admin(event):
                yield event.plain_result('只有主人能删。')
                return
            target = rest.strip()
            if not target.isdigit():
                yield event.plain_result('要删哪条？给个 id（#梗 列表 里那个 #后面的数字）')
                return
            con = kb.connect(self._db())
            try:
                cur = con.execute('delete from meme where id = ?', (int(target),))
                con.commit()
            finally:
                con.close()
            yield event.plain_result('删掉了。' if cur.rowcount else '没这条。')
            return
        yield event.plain_result('看不懂，试试：#梗 记 / #梗 查 / #梗 列表 / #梗 删')

    @filter.command('梗库')
    async def cmd_stats(self, event: AstrMessageEvent):
        """#梗库 —— 看看攒了多少条"""
        con = kb.connect(self._db())
        try:
            total = con.execute('select count(*) from meme').fetchone()[0]
            today = con.execute('select count(*) from meme where created_at > ?', (time.time() - 86400,)).fetchone()[0]
        finally:
            con.close()
        yield event.plain_result(f'梗库共 {total} 条，最近 24 小时新增 {today} 条。')
