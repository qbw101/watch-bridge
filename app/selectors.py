DOUYIN_CHAT_URL = "https://www.douyin.com/chat"

# Ordered alternatives keep page-specific changes isolated from the workflow.
LOGIN_MARKERS = (
    'text=私信',
    'input[placeholder*="搜索"]',
    '[role="textbox"][placeholder*="搜索"]',
)
LOGIN_REQUIRED_MARKERS = (
    'text=扫码登录',
    'text=验证码登录',
    'text=登录后',
)
RISK_MARKERS = (
    'text=安全验证',
    'text=完成验证',
    'text=验证身份',
)
SEARCH_INPUTS = (
    'input[placeholder*="搜索"]',
    'input[placeholder="搜索"]',  # 精确匹配备用 selector，兼容慢渲染时属性值变化
    '[role="textbox"][placeholder*="搜索"]',
    'input[aria-label*="搜索"]',
    '[role="textbox"][aria-label*="搜索"]',
)
CHAT_PANEL_MARKERS = (
    '[class*="RightPanelHeader"]',
    '[class*="chatHeader"]',
    '[class*="ChatHeader"]',
    '[class*="messageContent"]',
    '[class*="chatContent"]',
    '[class*="MessagePanel"]',
)
# 输入框。**顺序是有代价的**：`first_visible` 命中之前，每个排在前面的失效
# selector 都要白等一轮（见 app/douyin.py::first_visible）。实测当前抖音版本上
# `[data-placeholder*="发送消息"]` 是那个真正命中的写法，原先排在它前面的三条
# （DraftEditor 系列）在页面上都不存在 —— 于是每次「发完消息把输入框找回来」
# 都要白等 3 × 1.43 秒，占掉整条发送链路六成时间。所以把确认可用的那条提到
# 最前面，其余保留作兜底（抖音改版后 placeholder 文案可能变）。
MESSAGE_INPUTS = (
    '[contenteditable="true"][data-placeholder*="发送消息"]',
    '[contenteditable="true"][data-placeholder]',
    '[data-contents="true"]',
    '.DraftEditor-editor [contenteditable="true"]',
    '.DraftEditor-root [contenteditable="true"]',
    '[contenteditable="true"][aria-label*="消息"]',
    '[contenteditable="true"]',
    'textarea[placeholder*="消息"]',
)
IMAGE_INPUTS = ('input[type="file"][accept*="image"]', 'input[type="file"]')
STICKER_BUTTONS = (
    'svg.messageMsgInputiconAction',
    'button[aria-label*="表情"]',
    '[role="button"][aria-label*="表情"]',
    '[title*="表情"]',
)
STICKER_PANELS = (
    '.componentsemojiemojiPanel',
    '[class*="emojiPanel"]',
    '[role="dialog"]',
    '[class*="sticker"]',
)
# 表情面板底部的分类栏。这些 tab 是**纯图标**，没有文字、没有 aria-label，
# 所以只能按序号点（不能用 get_by_text）。序号在扫描时记进表情库的 tab_index。
STICKER_TABS = '.emojiEmojisModalTabsubTab'
# 分类内容区。切栏时里面整片换掉。
STICKER_TAB_CONTENT = '.componentsemojitabPanel'
# 内容区里的表情项。**扫描脚本枚举用的选择器必须与这里一致**，
# 否则「扫描记下的序号」和「发送时点的第几个」会对不上。
# 注意类选择器只匹配完整的 class token，所以不会误命中 ...emojiItemDesc。
STICKER_TAB_ITEMS = '.componentsemojitabPanel .emojiEmojiItememojiItem'


def sticker_resource_key(src: str) -> str:
    """从表情图地址里取「资源名」—— 不随面板顺序漂移的那部分。

    抖音的表情图地址形如
        https://p3-im-emoticon-sign.byteimg.com/ies.fe.effect/5d830e…b304~tplv-xx.png?lk3s=…
    path 才是存储侧的标识：问号后面是带签名的临时参数（过期就变），`~` 后面是
    图片处理模板。所以资源名 = path 最后一段、截到 `~` 为止。

    为什么发送时要靠它定位（而不是靠「第几栏第几个」）：收藏一个新表情会把它
    插到面板最前面，后面所有项的序号整体顺移，缓存的序号就指向了邻居 ——
    于是「预览的是 A、发出去的是 B」。资源名认的是图本身，顺序怎么变都找得到。

    这份规则必须与 `scripts/scan_stickers.py::_resource_key` 完全一致（库里存的是
    那个值），所以两边共用本函数，scan 那边只是转发。
    """
    if not src:
        return ""
    path = src.split("?", 1)[0].split("#", 1)[0]
    last = path.rsplit("/", 1)[-1]
    return last.split("~", 1)[0].strip()
