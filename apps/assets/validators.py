"""别名校验。

别名用于控制台的快速切换，因此规则必须比名称严格得多：

- **不能是纯数字**：数字串保留给 id，否则 ``sw 3`` 无法判断是实例 id 还是别名。
- **不能含特殊字符**：空格、点号、@、分号都被解析器用作分隔符
  （``集群.实例``、``集群@主机``），别名里出现这些字符会让解析产生歧义。
- **必须以字母开头**：保证任何别名都不会与 id 混淆。

允许的字符：字母、数字、下划线 ``_``、连字符 ``-``。
"""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError

# 以字母开头，后面允许字母/数字/下划线/连字符，总长 1-32
ALIAS_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")

ALIAS_MAX_LENGTH = 32

ALIAS_HELP_TEXT = (
    "可选。用于控制台快速切换，必须以字母开头，"
    "只能包含字母、数字、下划线和连字符，不能是纯数字"
)


def validate_alias(value: str | None) -> None:
    """校验别名格式。空值视为"未设置别名"，直接放行。"""
    if not value:
        return

    if value.isdigit():
        raise ValidationError(
            "别名不能是纯数字：数字串保留给实例/集群 id",
            code="alias_all_digits",
        )

    if not ALIAS_PATTERN.match(value):
        raise ValidationError(
            "别名必须以字母开头，且只能包含字母、数字、下划线、连字符（最长 32 位）",
            code="alias_invalid_format",
        )


def normalize_alias(value: str | None) -> str | None:
    """把空串归一化成 ``None``。

    唯一约束在 SQLite / PostgreSQL 下都不把多个 NULL 视为冲突，
    但空串会彼此冲突——所以"没有别名"必须一律存成 NULL。
    """
    if value is None:
        return None
    value = value.strip()
    return value or None
