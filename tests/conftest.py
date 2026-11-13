"""pytest 公共夹具。"""

from __future__ import annotations

import pytest


@pytest.fixture
def api_client():
    """Django 测试客户端（走完整中间件链路）。"""
    from django.test import Client

    return Client()
