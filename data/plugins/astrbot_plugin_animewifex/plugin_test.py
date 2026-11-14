import os
import sys

# 确保插件目录在Python路径中
plugin_dir = os.path.dirname(os.path.abspath(__file__))
if plugin_dir not in sys.path:
    sys.path.append(plugin_dir)

# 导入日志配置
import logging

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

print("\n=== 模拟AstrBot插件加载测试 ===")

# 模拟Context和AstrBotConfig类，因为我们没有实际的框架类
class MockContext:
    def __init__(self):
        self.name = "mock_context"

class MockAstrBotConfig:
    def __init__(self):
        # 设置一些模拟配置
        self.config_data = {
            "ntr_max": 3,
            "ntr_possibility": 50,
            "change_max_per_day": 5,
            "reset_max_uses_per_day": 2,
            "reset_success_rate": 50,
            "reset_mute_duration": 0,
            "image_base_url": "",
            "swap_max_per_day": 2
        }

    def get(self, key, default=None):
        return self.config_data.get(key, default)

try:
    # 导入WifePlugin类
    from main import WifePlugin
    print("✓ WifePlugin类导入成功")

    # 创建模拟对象
    mock_context = MockContext()
    mock_config = MockAstrBotConfig()

    # 初始化插件
    print("初始化WifePlugin类...")
    plugin = WifePlugin(mock_context, mock_config)
    print("✓ WifePlugin类初始化成功")

    # 检查插件基本功能
    print("检查插件基本功能...")

    # 检查commands字典
    if hasattr(plugin, "commands"):
        print(f"✓ commands字典包含 {len(plugin.commands)} 个命令")
        print("可用命令:", list(plugin.commands.keys()))
    else:
        print("✗ 未找到commands字典")

    # 检查基本方法是否存在
    basic_methods = ["animewife", "ntr_wife", "search_wife", "change_wife"]
    for method in basic_methods:
        if hasattr(plugin, method):
            print(f"✓ {method}方法存在")
        else:
            print(f"✗ {method}方法不存在")

except ImportError as e:
    print(f"✗ 导入WifePlugin失败: {e}")
except Exception as e:
    print(f"✗ 初始化WifePlugin过程中发生错误: {e}")
    import traceback
    traceback.print_exc()

print("\n=== 测试完成 ===")

# 清理临时文件
try:
    import os
    # 这里可以根据需要删除测试过程中创建的临时文件
    print("清理完成")
except Exception:
    pass
