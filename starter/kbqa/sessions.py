"""对话历史。

契约 §5：同一个 `session_id` 的多次请求算同一段对话，**不同 `session_id` 之间不能串线**。
所以历史必须按 `session_id` 分开存：只用一个全局列表的话，A 问“6 月营业额”，
B 紧接着问“那 7 月呢”，B 会接到 A 的上文，答出别人问过的数。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Optional

MAX_TURNS = 6
MAX_SESSIONS = 500


class SessionStore:
    """最近几轮对话，够解追问就行。按 session_id 隔离，超出容量按最久未用淘汰。"""

    def __init__(self, max_sessions: int = MAX_SESSIONS, max_turns: int = MAX_TURNS) -> None:
        self._sessions: "OrderedDict[str, list[dict]]" = OrderedDict()
        self._lock = threading.Lock()
        self.max_sessions = max_sessions
        self.max_turns = max_turns

    @staticmethod
    def _key(session_id: Optional[str]) -> Optional[str]:
        key = (session_id or "").strip()
        # 没给 session_id 就当成无状态：不读历史也不写历史。
        # 随便给个 key 会让两个互不相关的调用共用上文。
        return key or None

    def history(self, session_id: Optional[str]) -> list[dict]:
        key = self._key(session_id)
        if key is None:
            return []
        with self._lock:
            turns = self._sessions.get(key)
            if not turns:
                return []
            self._sessions.move_to_end(key)
            return list(turns)

    def append(self, session_id: Optional[str], turn: dict) -> None:
        key = self._key(session_id)
        if key is None:
            return
        with self._lock:
            turns = self._sessions.setdefault(key, [])
            turns.append(turn)
            del turns[: max(0, len(turns) - self.max_turns)]
            self._sessions.move_to_end(key)
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
