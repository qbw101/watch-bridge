"""把从网页上抓来的「名字」收成一个名字 —— 只干这一件事。

为什么值得单开一个模块：同一个坑在四处各犯过一次（2026-09-25 才发现）。
`bridge/reader.py` 抓会话列表、`bridge/friend_scan.py` 汇总扫描结果、
`bridge/watch_friends.py` 读写 config.json、`app/douyin.py` 比对名字，
「清理」全都写成了 `.strip()` —— 而 strip 只去**首尾**空白，对字符串
**中间**的换行毫无感觉。

脏数据是这么来的：会话行里昵称和右侧的时间在同一个块级容器里，`innerText`
在块级子元素之间会插一个换行，整段拿来当名字就多出来一行 ——
`"某位好友\\n前天"`。`.trim()` 去不掉中间那个换行。

后果不是「显示难看」那么轻：这个名字**搜不到**。搜索框是单行输入框，填进去
换行会被浏览器转成空格，于是实际搜的是 `某位好友 前天`，而页面上根本没有
叫这个的会话；精确比对也过不去。手表端看到的就是「卡在搜索框里」。

所以规范化只留这一处实现，别的地方都调它 —— 别再写 `.strip()` 当清理。
"""

from __future__ import annotations

import re

# 连续的空白（空格、制表符、换行、全角空格）压成一个半角空格。
# 名字**内部**的空格是有意义的，不能整个删掉 —— 见下面 normalize_name 的说明。
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_name(value: object) -> str:
    """把抓来的文本收成一个名字；认不出来的一律给空字符串。

    - `"某位好友\\n前天"` -> `"某位好友"`：只留**第一行**。第二行起是会话
      时间或最后一条消息预览，那是页面布局的产物，不是名字的一部分。
    - `"  小明 7.25 "` -> `"小明 7.25"`：去首尾，中间的空格留着 —— 名字里
      带日期是真有人这么起名的（如「小明 7.25」），不能一并删掉。
    - `None` / 数字 / 别的类型 -> `""`，由调用方当作「没有名字」丢掉。
    """
    if not isinstance(value, str):
        return ""
    lines = value.splitlines()
    first = lines[0] if lines else ""
    return _WHITESPACE_RUN.sub(" ", first).strip()
