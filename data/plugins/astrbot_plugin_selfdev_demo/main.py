from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

GREETING = "demo v1：你好，我是演示插件。"
VERSION_TAG = "v1"


@register("astrbot_plugin_selfdev_demo", "dsh", "self_dev 靶子插件", "0.1.0")
class DemoPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    @filter.command("demo")
    async def cmd_demo(self, event: AstrMessageEvent):
        yield event.plain_result(f"{GREETING}（{VERSION_TAG}）")
