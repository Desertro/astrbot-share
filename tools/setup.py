#!/usr/bin/env python3
"""Generate a fresh installation from sanitized templates and local credentials."""

import argparse
import datetime
import getpass
import importlib.util
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from share_runtime import MARKER, read_env  # noqa: E402

DROP = object()


def render(obj, values, identities):
    """Render nested templates and remove unassigned identity-specific entries."""
    if isinstance(obj, dict):
        if set(obj) == {"__env_int__"}:
            value = values.get(obj["__env_int__"], "")
            return int(value) if value else DROP
        result = {}
        for key, value in obj.items():
            if any(
                name in identities and not values.get(name)
                for name in MARKER.findall(key)
            ):
                continue
            new_key = MARKER.sub(lambda match: values.get(match[1], ""), key)
            new_value = render(value, values, identities)
            if new_value is DROP:
                if key in {"target_umo", "umo", "group_id", "user_id"}:
                    return DROP
                continue
            result[new_key] = new_value
        return result
    if isinstance(obj, list):
        return [
            value
            for entry in obj
            if (value := render(entry, values, identities)) is not DROP
        ]
    if isinstance(obj, str):
        if any(
            name in identities and not values.get(name) for name in MARKER.findall(obj)
        ):
            return DROP
        return MARKER.sub(lambda match: values.get(match[1], ""), obj)
    return obj


def main():
    """Run the interactive wizard, validation or deterministic noninteractive export."""
    parser = argparse.ArgumentParser(
        description="AstrBot 配置向导；不会读取原作者的运行目录。"
    )
    parser.add_argument(
        "--all", action="store_true", help="同时填写所有可选服务、账号和密钥"
    )
    parser.add_argument(
        "--non-interactive", action="store_true", help="从 .env 和环境变量生成配置"
    )
    parser.add_argument(
        "--check", action="store_true", help="只检查填写情况和已生成配置"
    )
    parser.add_argument(
        "--list", action="store_true", help="列出变量用途，不显示填写的值"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="备份后重新生成已有配置；保留已有数据库",
    )
    parser.add_argument(
        "--docker", action="store_true", help="将默认安装路径生成为容器内的 /AstrBot"
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path, default=ROOT)
    args = parser.parse_args()
    specs = json.loads((ROOT / "config/variables.json").read_text(encoding="utf-8"))
    if args.list:
        for name, spec in specs.items():
            print(
                name,
                "必填" if spec["required"] else "可选",
                "; ".join(spec["locations"]),
            )
        return 0
    values = {name: spec["default"] for name, spec in specs.items()}
    saved = read_env(args.env_file)
    values.update(saved)
    values.update({name: os.environ[name] for name in specs if name in os.environ})
    if not args.non_interactive and not args.check:
        print(
            "填写一次，自动同步到各插件。回车保留已有值；可选项留空后按功能自行补齐。"
        )
        print("AB_QQ_1 是管理员 QQ；AB_QQ_2 是主群。其他群与私聊账号可在 .env 中追加。")
        for name, spec in specs.items():
            if not spec["required"] and not args.all:
                continue
            if name == "AB_ONEBOT_TOKEN" and not values[name]:
                values[name] = secrets.token_urlsafe(32)
                print("已生成 OneBot Token，保存在 .env 的 AB_ONEBOT_TOKEN。")
                continue
            print("\n" + name + "：" + "; ".join(spec["locations"][:2]))
            prompt = "已填写，回车保留：" if values[name] else "请输入（回车跳过）："
            new_value = getpass.getpass(prompt) if spec["secret"] else input(prompt)
            if new_value:
                values[name] = new_value
            elif name == "AB_DASHBOARD_PASSWORD" and not values[name]:
                alphabet = string.ascii_letters + string.digits
                values[name] = "Aa1" + "".join(
                    secrets.choice(alphabet) for _ in range(25)
                )
                print("已生成面板密码，保存在 .env 的 AB_DASHBOARD_PASSWORD。")
        args.env_file.parent.mkdir(parents=True, exist_ok=True)
        content = "# Private installation values. Never commit this file.\n"
        content += (
            "\n".join(
                f"{name}={json.dumps(values[name], ensure_ascii=False)}"
                for name in specs
            )
            + "\n"
        )
        args.env_file.write_text(content, encoding="utf-8")
        args.env_file.chmod(0o600)
    missing = [
        name
        for name, spec in specs.items()
        if spec["required"] and not values.get(name)
    ]
    if missing:
        print("缺少必填项：" + ", ".join(missing), file=sys.stderr)
        return 2
    for name in [key for key in specs if key.startswith("AB_QQ_")]:
        if values[name] and not re.fullmatch(r"[1-9]\d{4,12}", values[name]):
            print("QQ / 群号格式不正确：" + name, file=sys.stderr)
            return 2
    password = values["AB_DASHBOARD_PASSWORD"]
    if len(password) < 8 or not all(
        re.search(pattern, password) for pattern in [r"[A-Z]", r"[a-z]", r"\d"]
    ):
        print("面板密码至少 8 位，并包含大写、小写字母和数字。", file=sys.stderr)
        return 2
    output = args.output.resolve()
    values["ASTRBOT_ROOT"] = "/AstrBot" if args.docker else str(output)
    values["USER_HOME"] = "/root" if args.docker else str(Path.home())
    for _ in range(8):
        values = {
            name: MARKER.sub(lambda match: values.get(match[1], ""), value)
            for name, value in values.items()
        }
    identities = {
        name
        for name, spec in specs.items()
        if spec["category"] == "identity" or name.startswith("AB_QQ_")
    }
    prepared = {}
    for template in sorted((ROOT / "config/templates").rglob("*.json")):
        relative = template.relative_to(ROOT / "config/templates")
        obj = render(
            json.loads(template.read_text(encoding="utf-8")), values, identities
        )
        if obj is DROP:
            raise ValueError("Empty root template: " + str(relative))
        serialized = json.dumps(obj, ensure_ascii=False)
        if MARKER.search(serialized):
            raise ValueError("Unresolved marker: " + str(relative))
        prepared[relative] = obj
    config = prepared[Path("data/cmd_config.json")]
    if args.check:
        missing_files = [
            str(relative)
            for relative in prepared
            if relative != Path("database.json") and not (output / relative).exists()
        ]
        if missing_files:
            print("尚未生成配置：" + ", ".join(missing_files), file=sys.stderr)
            return 2
        for relative in prepared:
            if relative == Path("database.json"):
                continue
            data = json.loads((output / relative).read_text(encoding="utf-8-sig"))
            if MARKER.search(json.dumps(data)):
                print("配置中还有占位符：" + str(relative), file=sys.stderr)
                return 2
        if not (output / "data/data_v4.db").exists():
            print("缺少初始角色数据库。", file=sys.stderr)
            return 2
        print("配置检查通过。未输出密钥。")
        return 0
    existing = [
        output / relative
        for relative in prepared
        if relative != Path("database.json") and (output / relative).exists()
    ]
    if existing and not args.overwrite:
        print(
            "已有配置，未改动任何文件。修改后如需重建，请使用 --overwrite；已有数据库会保留。",
            file=sys.stderr,
        )
        return 2
    auth_spec = importlib.util.spec_from_file_location(
        "share_auth_password", ROOT / "astrbot/core/utils/auth_password.py"
    )
    auth = importlib.util.module_from_spec(auth_spec)
    auth_spec.loader.exec_module(auth)
    dashboard = config["dashboard"]
    dashboard["password"] = auth.hash_md5_dashboard_password(password)
    dashboard["pbkdf2_password"] = auth.hash_dashboard_password(password)
    dashboard["jwt_secret"] = secrets.token_urlsafe(48)
    dashboard["password_storage_upgraded"] = True
    dashboard["password_change_required"] = False
    if existing:
        backup = (
            output
            / "data/setup-backups"
            / datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )
        for path in existing:
            target = backup / path.relative_to(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    for relative, obj in prepared.items():
        if relative == Path("database.json"):
            continue
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".new")
        temporary.write_text(
            json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.chmod(0o600)
        temporary.replace(path)
    database = output / "data/data_v4.db"
    if not database.exists():
        with sqlite3.connect(database) as connection:
            for table, entry in prepared[Path("database.json")].items():
                connection.execute(entry["schema"])
                for row in entry["rows"]:
                    columns = list(row)
                    column_sql = ", ".join('"' + column + '"' for column in columns)
                    params = ", ".join("?" for _ in columns)
                    connection.execute(
                        f'INSERT INTO "{table}" ({column_sql}) VALUES ({params})',
                        [row[column] for column in columns],
                    )
        database.chmod(0o600)
    print("已生成 " + str(len(prepared) - 1) + " 份配置，并保留原有功能参数。")
    print("已初始化角色与指令配置；聊天记录、群成员资料、记忆数据库从空白开始。")
    print(
        "QQ 反向 WebSocket：ws://<AstrBot主机>:6199/ws；Token 对应 .env 的 AB_ONEBOT_TOKEN。"
    )
    print("管理面板：http://localhost:6185；账号和密码见本机 .env。")
    print("可选密钥或外部服务没有填写时，对应功能需要补齐后才能使用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
