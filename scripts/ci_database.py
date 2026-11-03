"""仅供全新、一次性CI MySQL服务，绝不用于本地已有数据库。"""

import os
import secrets

import pymysql

if os.getenv("GITHUB_ACTIONS") != "true" or not os.getenv("GITHUB_ENV"):
    raise SystemExit("EPHEMERAL_GITHUB_CI_ONLY")

password = secrets.token_urlsafe(32)
print("::add-mask::" + password)
with pymysql.connect(host="127.0.0.1", user="root", port=3306) as connection:
    with connection.cursor() as cursor:
        cursor.execute("CREATE DATABASE fitlife_test CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
        cursor.execute("CREATE USER 'fit_test'@'%%' IDENTIFIED BY %s", (password,))
        cursor.execute("GRANT ALL ON fitlife_test.* TO 'fit_test'@'%'")
    connection.commit()
with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as target:
    target.write("FITLIFE_DB_PASSWORD=" + password + "\n")
print("已创建一次性CI测试账号；密码已掩码，仅传入本次作业环境。")
