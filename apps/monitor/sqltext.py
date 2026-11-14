"""SQL 文本归一化：把具体语句折叠成可聚合的"指纹"。

为什么必须归一化
----------------
``SELECT * FROM orders WHERE id = 1`` 与 ``... WHERE id = 2`` 是同一条 SQL
的两个实例。不做归一化，SQL 洞察表里的每一行都会是"只执行过一次"的噪声，
真正的慢 SQL 反而被淹没。

归一化做三件事
--------------
1. **抹掉字面量**：数字、字符串、日期 → ``?``。既让同构语句合并，
   也顺带避免把业务数据（手机号、身份证号）落进洞察表。
2. **折叠 IN 列表**：``IN (?,?,?)`` 与 ``IN (?,?)`` 应视为同一条语句，
   否则参数个数一变指纹就变。
3. **统一空白与大小写**：多行缩进、大小写混写不影响指纹。

``digest`` 取归一化文本的 sha1。为什么用 sha1 而不是 Python 的 ``hash()``：
后者每次进程启动都不同（字符串哈希有随机盐），落到库里跨进程就对不上了。

归一化必须**幂等**：``normalize(normalize(x)) == normalize(x)``。
否则同一个模板再次入库时会算出第二个指纹，聚合失效。
"""

from __future__ import annotations

import hashlib
import re

__all__ = ["normalize_sql", "digest_of", "MAX_TEMPLATE_LENGTH"]

# 模板入库长度上限：够看清语句结构，又不至于让一条巨型 SQL 撑爆行宽
MAX_TEMPLATE_LENGTH = 2000

# 字符串字面量：'' 与 '' 内的转义（MySQL 用 \' ，PG 用 ''）
_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.|'')*'", re.DOTALL)
# 双引号标识符在 PG 里是标识符、在 MySQL 里可能是字符串。
# 保守起见按"引号内容"整体替换成 ?——宁可少聚合一些，也不泄露内容。
_DOUBLE_QUOTED = re.compile(r'"(?:[^"\\]|\\.|"")*"', re.DOTALL)
# 数字字面量：前后不能紧邻标识符字符，否则会把 col1 咬掉
_NUMBER_LITERAL = re.compile(r"(?<![\w$.])\d+(?:\.\d+)?(?![\w$.])")
# IN 列表折叠：IN (?, ?, ?) -> IN (?)
_IN_LIST = re.compile(r"\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)", re.IGNORECASE)
# 连续空白
_WHITESPACE = re.compile(r"\s+")
# 十六进制字面量（MySQL 0x1F / X'1F'）
_HEX_LITERAL = re.compile(r"\b0x[0-9a-fA-F]+\b")


def normalize_sql(sql: str) -> str:
    """把一条 SQL 折叠成模板文本。

    处理顺序很重要：先抹字符串（字符串里可能含数字和括号），
    再抹数字，最后折叠 IN 列表——题序反了会把 ``'abc123'`` 里的数字
    先替换掉，模板里出现 ``'abc?'`` 这种既不是字面量也不是占位的怪东西。
    """
    if not sql:
        return ""

    text = sql.strip()

    # 1. 字符串/引号内容 → ?（保留引号本身，便于看出"这里原本有个字符串"）
    text = _STRING_LITERAL.sub("?", text)
    text = _DOUBLE_QUOTED.sub("?", text)

    # 2. 十六进制与数字 → ?
    text = _HEX_LITERAL.sub("?", text)
    text = _NUMBER_LITERAL.sub("?", text)

    # 3. IN 列表折叠
    text = _IN_LIST.sub("IN (?)", text)

    # 4. 空白统一。不能直接 collapse 掉换行——SQL 里的 -- 行注释靠换行终止，
    #    但注释在这个阶段已经不影响语义（服务端执行前会剥离），
    #    且模板入库后是给人看的，单行更易读。
    text = _WHITESPACE.sub(" ", text).strip()

    return text[:MAX_TEMPLATE_LENGTH]


def digest_of(sql: str) -> str:
    """归一化后取 sha1，作为跨采样的聚合键。

    用 sha1 而非 ``hash()``：后者带进程级随机盐，重启后同一语句算出
    不同指纹，洞察表里的历史数据就永远聚合不到一起。
    """
    template = normalize_sql(sql)
    return hashlib.sha1(template.encode("utf-8")).hexdigest()
