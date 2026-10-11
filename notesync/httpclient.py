# -*- coding: utf-8 -*-
"""统一的出网入口 —— 去掉 urllib 硬塞的 `Connection: close`。

为什么需要这个模块
==================
CPython 的 `urllib.request.AbstractHTTPHandler.do_open` **硬写**了：

    headers["Connection"] = "close"

（源码里紧跟着一段注释："这个 handler 不处理持久连接，所以确保连接用完就关"）。
也就是说 —— **urllib 发出的每一个请求都带 `Connection: close`**，
调用方无论怎么在 `Request` 里指定 `Connection` 都覆盖不掉（`do_open` 之后又写一遍）。

为什么这会出问题
----------------
带 `Connection: close` 时，服务端把响应体发完就**立刻关闭 TCP 连接**。
正常网络上没问题；但在 **中间有透明代理 / 弱网** 的链路上，
代理可能在尾部数据还没送完时就把连接拆掉，客户端于是拿到
`IncompleteRead`（或干脆读超时），而且**大响应更容易中招**。

2026-10-11 在同一台服务器上做的对照实验（同一 URL、同一 Token、同一时间窗，
每项各 4 次，轮转执行以避免"链路分窗口抖动"造成的偏置）：

    | 请求方式                        | 结果   |
    |---------------------------------|--------|
    | 裸 socket，不发 Connection       | 4/4 ✅ |
    | + `Accept-Encoding: identity`   | 4/4 ✅ |
    | **+ `Connection: close`**       | **0/4 ❌** |
    | urllib 默认（会补 close）         | 0/4 ❌ |
    | urllib 手动指定 `keep-alive`      | 0/4 ❌（覆盖不掉） |

→ **唯一触发点就是 `Connection: close`。**
（同一实验里 `$top=100` 的单页响应是 381KB，网络本身完全能承载。）

做法：**保留 urllib**（重定向、代理、错误处理、Cookie 全都还在），
只换掉"用哪个连接类"—— 用一个子类在 `request()` 里把 `Connection` 头摘掉。
HTTP/1.1 本来就是持久连接语义，我们读完响应就 `close()`，行为上是等价的。

对别人的部署有影响吗
--------------------
没有。这是更标准的 HTTP/1.1 行为（`close` 才是特例），
在任何网络上都不会变差；而且**重定向与代理支持都保留了**。
"""

from __future__ import annotations

import http.client
import urllib.request

__all__ = ["OPENER", "DIRECT", "urlopen", "build"]


class _NoCloseMixin:
    """把调用方传进来的 `Connection` 头摘掉。

    `urllib` 在 `do_open` 里写的是 `headers["Connection"] = "close"`，
    这里在真正发送前过滤掉 —— 于是请求不带 Connection，
    服务端按 HTTP/1.1 默认的持久连接处理，不会发完就拆 TCP。
    """

    def request(self, method, url, body=None, headers=None, *,
                encode_chunked=False):
        if headers:
            headers = {k: v for k, v in headers.items()
                       if k.lower() != "connection"}
        return super().request(method, url, body, headers,
                               encode_chunked=encode_chunked)


class _NoCloseHTTPConnection(_NoCloseMixin, http.client.HTTPConnection):
    pass


class _NoCloseHTTPSConnection(_NoCloseMixin, http.client.HTTPSConnection):
    pass


class _NoCloseHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_NoCloseHTTPConnection, req)


class _NoCloseHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_NoCloseHTTPSConnection, req,
                            context=self._context,
                            check_hostname=self._check_hostname)


def build(use_proxy: bool = True) -> urllib.request.OpenerDirector:
    """造一个 opener。

    `use_proxy=False` 时不读环境里的 http_proxy/https_proxy
    （与小米侧原来的 `ProxyHandler({})` 语义一致）。
    `build_opener` 默认会挂一个 `ProxyHandler()`，所以必须显式覆盖。
    """
    handlers: list = []
    if not use_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    handlers += [_NoCloseHTTPHandler(), _NoCloseHTTPSHandler()]
    return urllib.request.build_opener(*handlers)


#: 默认 opener —— 尊重 http_proxy / https_proxy 环境变量（与原来行为一致）
OPENER = build(use_proxy=True)

#: 强制直连的 opener —— 不读任何代理环境变量
DIRECT = build(use_proxy=False)


def urlopen(req, timeout=None, *, opener=None):
    """`urllib.request.urlopen` 的替代品，只少了那个 `Connection: close`。"""
    return (opener or OPENER).open(req, timeout=timeout)
