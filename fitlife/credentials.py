"""定向读取本项目的Windows通用凭据；不枚举其他凭据，不写秘密文件。"""

import argparse
import os
import sys
import warnings
from getpass import GetPassWarning, getpass
from pathlib import Path

COMMENT = "FitLife MySQL password (UTF-16LE)"


def target_name(environment, host="127.0.0.1", port=3306):
    if environment not in {"dev", "test"}:
        raise ValueError("INVALID_DATABASE_ENVIRONMENT")
    return f"FitLife/MySQL/{host}:{port}/fitlife_{environment}"


def _read(target, username):
    if os.name != "nt":
        return None
    import pywintypes
    import win32cred

    try:
        item = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)
    except pywintypes.error as exc:
        if exc.winerror == 1168:
            return None
        raise RuntimeError("CREDENTIAL_STORE_UNAVAILABLE") from None
    if item["UserName"] != username or item.get("Comment") != COMMENT:
        raise RuntimeError("CREDENTIAL_METADATA_MISMATCH")
    value = item["CredentialBlob"]
    return value.decode("utf-16-le") if isinstance(value, bytes) else value


def _write(target, username, password, *, session_only=False):
    import win32cred

    win32cred.CredWrite(
        {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": target,
            "UserName": username,
            "CredentialBlob": password,
            "Comment": COMMENT,
            "Persist": win32cred.CRED_PERSIST_SESSION
            if session_only
            else win32cred.CRED_PERSIST_LOCAL_MACHINE,
        }
    )


def database_password(environment, host="127.0.0.1", port=3306, *, interactive=True):
    target = target_name(environment, host, port)
    value = os.getenv("FITLIFE_DB_PASSWORD")
    if not value and os.getenv("FITLIFE_DB_PASSWORD_FILE"):
        value = Path(os.environ["FITLIFE_DB_PASSWORD_FILE"]).read_text(encoding="utf-8").strip()
    value = value or _read(target, f"fit_{environment}")
    if value:
        return value
    if not interactive or not sys.stdin.isatty():
        raise RuntimeError("DATABASE_PASSWORD_REQUIRED")
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        value = getpass(f"fit_{environment} 密码（隐藏输入，不保存）：")
    if not value:
        raise ValueError("EMPTY_PASSWORD")
    return value


def main():
    parser = argparse.ArgumentParser(
        description="将FitLife数据库密码保存到当前用户的Windows凭据管理器"
    )
    parser.add_argument("--environment", required=True, choices=["dev", "test"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument(
        "--delete", action="store_true", help="仅删除对应的本项目系统凭据，不改数据库"
    )
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("此入口仅用于Windows；其他环境通过进程环境变量提供密码。")
    import pywintypes
    import win32cred

    target = target_name(args.environment, args.host, args.port)
    if args.delete:
        try:
            win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC)
        except pywintypes.error as exc:
            if exc.winerror != 1168:
                raise SystemExit("系统凭据删除失败；未输出秘密内容。") from None
        print("已删除本项目对应的系统凭据；数据库未改变。")
        return
    if not sys.stdin.isatty():
        raise SystemExit("请在本机交互终端运行，密码不能从命令参数或管道输入。")
    # 先验证连接身份，再保存，避免把错误密码写入系统。
    from sqlalchemy import text

    from fitlife.database import DatabaseSettings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            password = getpass(f"fit_{args.environment} 密码（将保存在Windows凭据管理器）：")
        if not password:
            raise ValueError("EMPTY_PASSWORD")
        engine = DatabaseSettings(
            environment=args.environment, host=args.host, port=args.port, password=password
        ).engine()
        try:
            with engine.connect() as connection:
                account, database = connection.execute(
                    text("SELECT CURRENT_USER(),DATABASE()")
                ).one()
                if (
                    account.split("@", 1)[0] != f"fit_{args.environment}"
                    or database != f"fitlife_{args.environment}"
                ):
                    raise ValueError("DATABASE_IDENTITY_MISMATCH")
        finally:
            engine.dispose()
        _write(target, f"fit_{args.environment}", password)
        if _read(target, f"fit_{args.environment}") != password:
            raise RuntimeError("CREDENTIAL_ROUNDTRIP_FAILED")
    except Exception as exc:
        raise SystemExit(f"凭据配置未完成：{type(exc).__name__}；未输出密码或连接详情。") from None
    print("数据库身份已验证，密码已保存到当前用户的Windows凭据管理器；未写入项目文件。")


if __name__ == "__main__":
    main()
