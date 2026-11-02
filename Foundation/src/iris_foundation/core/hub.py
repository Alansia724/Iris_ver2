"""DataHub：进程内发布-订阅中枢，带每主题历史环形缓冲（**零 Qt**，§18）。

- publish(topic, payload)：同步投递给订阅者（GUI 线程内调用）
- subscribe / unsubscribe：主题订阅
- history(topic, n)：取历史数据（Monitor 打开时可直接回放）
- configure_history(topic, maxlen)：为主题启用历史缓冲

线程纪律：无锁，publish/subscribe 必须在同一线程（或经信号队列投递回
GUI 线程）；采集 worker 经 Qt 信号把帧带回 GUI 线程后再 publish。

派发隔离：订阅者回调抛异常不得击穿调用方——逐个隔离并落盘，其余
订阅者照常收到数据。（本模块原为 QObject；撤 Qt 后隔离由自己负责，不再
依赖 PyQt6 槽异常的 qFatalAbort 行为。）
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Callable

_log = logging.getLogger("labcontrol.hub")


class DataHub:
    """进程内发布-订阅中枢，带每主题历史环形缓冲。"""

    def __init__(self):
        self._subs: dict[str, list[Callable]] = {}
        self._history: dict[str, deque] = {}

    def configure_history(self, topic: str, maxlen: int) -> None:
        self._history[topic] = deque(maxlen=maxlen)

    def publish(self, topic: str, payload) -> None:
        if topic in self._history:
            self._history[topic].append(payload)
        for cb in tuple(self._subs.get(topic, ())):
            try:
                cb(payload)
            except Exception:
                _log.exception("hub subscriber failed on %s; "
                               "isolated (other subscribers unaffected)", topic)

    def subscribe(self, topic: str, cb: Callable) -> None:
        self._subs.setdefault(topic, [])
        if cb not in self._subs[topic]:
            self._subs[topic].append(cb)

    def unsubscribe(self, topic: str, cb: Callable) -> None:
        lst = self._subs.get(topic)
        if lst and cb in lst:
            lst.remove(cb)

    def history(self, topic: str, n: int | None = None) -> list:
        d = self._history.get(topic)
        if not d:
            return []
        items = list(d)
        return items if n is None else items[-n:]

    def subscriber_count(self, topic: str) -> int:
        return len(self._subs.get(topic, []))
