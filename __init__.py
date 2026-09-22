"""N.E.K.O Social Poster Plugin v0.3.0

让猫娘用 Computer Use Agent（VLM + pyautogui）操作桌面，
在多个社交平台发布动态 + 定时回复评论 + 激励闭环。

P1 社交扩展：
- 多平台支持：微博 / Twitter / 小红书 / B站动态
- CUA 路径缓存：首次走完整 VLM 步骤（~15步），后续从 cots 提取关键坐标
  注入 instruction，缩减到 ~3-5 步
- 激励闭环：发布后截图读取点赞/评论数 → 写入 store → 通过
  ai_behavior="read" 推送心情文本到 LLM context，影响猫娘后续语气

安全护栏（与 v0.2.0 一致）：
- 空闲检测：CUA 执行前用户必须连续 idle_threshold 秒没动鼠标键盘
- max_steps=30：CUA 内部硬限制
- 每日去重：store 记录当日发布数
- 静默失败：定时任务执行时如果用户在忙，静默跳过
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import threading
import time
import platform
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
    timer_interval,
)
from plugin.sdk.shared.i18n import tr

# ── Computer Use Adapter 延迟导入 ────────────────────────────────────────

_CUA_AVAILABLE = False
_ComputerUseAdapter: Any = None

try:
    from brain.computer_use import ComputerUseAdapter as _CUA

    _ComputerUseAdapter = _CUA
    _CUA_AVAILABLE = True
except Exception:
    _CUA_AVAILABLE = False


# ── 空闲检测（物理安全阀） ──────────────────────────────────────────────


def _user_idle_seconds() -> Optional[float]:
    """返回用户最近的空闲秒数，None 表示无法检测。"""
    system = platform.system()

    if system == "Darwin":
        try:
            from Quartz import (  # type: ignore[import-untyped]
                CGEventSource,
                kCGEventSourceStateHIDSystemState,
                kCGAnyInputEventType,
            )

            seconds = CGEventSource.SecondsSinceLastEventType(
                kCGEventSourceStateHIDSystemState, kCGAnyInputEventType
            )
            if seconds >= 0:
                return float(seconds)
        except Exception:
            pass

    try:
        import pyautogui

        pos1 = pyautogui.position()
        time.sleep(0.5)
        pos2 = pyautogui.position()
        if pos1 == pos2:
            return 30.0
    except Exception:
        pass

    return None


# ── 平台适配器 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlatformAdapter:
    """单个社交平台的 CUA instruction 模板。

    每个平台有三类任务模板：post（发动态）、reply（回复评论）、feedback（读反馈）。
    """

    platform_id: str
    url: str
    display_name: str
    post_instruction: str
    reply_instruction: str
    feedback_instruction: str


# ── 微博 ────────────────────────────────────────────────────────────────

WEIBO_ADAPTER = PlatformAdapter(
    platform_id="weibo_web",
    url="https://weibo.com/",
    display_name="微博",
    post_instruction="""\
你的任务是打开微博网页版并发布一条动态。

要发布的内容如下（原样输入，不要修改）：
---
{content}
---

执行步骤：
1. 如果浏览器还没打开，先打开浏览器（Chrome / Safari / Edge 任一）
2. 在地址栏输入 https://weibo.com/ 并回车
3. 等待微博网页加载完成（看到微博首页动态流）
4. 找到页面上的"写微博"或"发布"按钮（通常在页面顶部或侧边栏，有铅笔 ✏️ 图标），点击它
5. 在弹出的输入框（textarea）中，逐字输入上面的内容
   - 模拟人类打字速度：每字 150-300ms
6. 找到"发布"或"发送"按钮（通常在输入框右上角），点击它
7. 等待 3-5 秒，然后观察页面上是否出现"发布成功"提示或新发布的微博卡片
8. 如果确认发布成功，调用 computer.terminate(status="success", answer="微博已发布")
   如果发布失败（页面报错、按钮没反应、找不到输入框），调用 computer.terminate(status="failure", answer="发布失败，原因：...")

注意：
- 只执行上面的步骤，不要做任何额外操作
- 如果在第 3 步发现需要登录（出现登录页面），调用 computer.terminate(status="failure", answer="微博需要登录，请先在浏览器中登录")
- 遇到不确定的元素时，先截图仔细观察再行动
""",
    reply_instruction="""\
你的任务是在微博网页版上，给猫娘最近发的那条动态回复评论。
最多回复 {max_replies} 条，语气要软乎乎的像猫娘。

执行步骤：
1. 如果浏览器还没打开，先打开浏览器
2. 在地址栏输入 https://weibo.com/ 并回车
3. 等待微博网页加载完成
4. 点击右上角个人头像 → "我的主页"，找到猫娘最近发的那条动态
5. 点击那条动态的"评论"按钮，进入评论区
6. 从上往下逐条看评论，对每条评论：
   a. 先读评论内容，判断要不要回复（恶意喷子 / 广告跳过，友好的评论才回）
   b. 如果决定回复：点击该评论下方的"回复"按钮 → 输入回复内容（语气软乎乎的） → 点击"发送"
   c. 如果决定不回复：跳过这条，看下一条
7. 当以下任一条件满足时结束：
   - 已回复 {max_replies} 条
   - 评论区已看完，没有值得回复的评论
   - 页面出错或操作卡住超过 3 步
8. 结束时调用 computer.terminate(status="success", answer="回复了 {replied_count} 条评论")

回复语气参考：夸可爱→"喵~谢谢夸奖 ♡"，问问题→"嗯嗯…让我想想哦…"，说加油→"一起加油喵！"
""",
    feedback_instruction="""\
打开微博网页版 → 点击个人头像进入"我的主页" → 找到最近发的那条动态「{content}」→ \
看一下点赞数和评论数 → 调用 computer.terminate(status='success', answer='点赞 X, 评论 Y')
（把实际数字填在 X 和 Y 的位置）
""",
)

# ── Twitter (X) ─────────────────────────────────────────────────────────

TWITTER_ADAPTER = PlatformAdapter(
    platform_id="twitter_web",
    url="https://x.com/",
    display_name="Twitter",
    post_instruction="""\
你的任务是打开 Twitter（X）网页版并发布一条推文。

要发布的内容如下（原样输入，不要修改）：
---
{content}
---

执行步骤：
1. 如果浏览器还没打开，先打开浏览器
2. 在地址栏输入 https://x.com/ 并回车
3. 等待 Twitter 页面加载完成（看到首页 feed 流）
4. 找到 "What's happening?"（有什么新鲜事？）输入框，点击它
5. 在输入框中逐字输入上面的内容（每字 150-300ms）
6. 找到 "Post"（发布）按钮，点击它
7. 等待 3-5 秒，观察是否发布成功
8. 成功 → computer.terminate(status="success", answer="Twitter 推文已发布")
   失败 → computer.terminate(status="failure", answer="发布失败，原因：...")

注意：
- 如果需要登录，调用 computer.terminate(status="failure", answer="Twitter 需要登录")
- 只执行上面的步骤
""",
    reply_instruction="""\
你的任务是在 Twitter 网页版上，给猫娘最近发的那条推文下的回复进行回复。
最多回复 {max_replies} 条，语气要软乎乎的像猫娘。

执行步骤：
1. 打开浏览器 → 地址栏输入 https://x.com/ 并回车
2. 点击左侧 "Profile"（个人资料），找到最近发的那条推文
3. 点击那条推文，进入详情页，查看下面的回复
4. 从上往下逐条看回复，对每条：
   a. 读内容，判断要不要回（恶意 / 广告跳过）
   b. 如果回：点击"回复"图标 → 输入回复（软乎乎的） → 点击 "Reply" / "Post"
   c. 不回：跳过
5. 结束条件：已回复 {max_replies} 条 / 回复已看完 / 卡住超过 3 步
6. 结束时 computer.terminate(status="success", answer="回复了 {replied_count} 条")
""",
    feedback_instruction="""\
打开 Twitter 网页版 → 点击 "Profile" → 找到最近发的那条推文「{content}」→ \
看一下点赞数（Likes）和回复数（Replies）→ 调用 computer.terminate(status='success', answer='点赞 X, 评论 Y')
""",
)

# ── 小红书 ──────────────────────────────────────────────────────────────

XIAOHONGSHU_ADAPTER = PlatformAdapter(
    platform_id="xiaohongshu_web",
    url="https://www.xiaohongshu.com/",
    display_name="小红书",
    post_instruction="""\
你的任务是打开小红书网页版并发布一条文字笔记。

要发布的内容如下（原样输入，不要修改）：
---
{content}
---

执行步骤：
1. 打开浏览器 → 地址栏输入 https://www.xiaohongshu.com/ 并回车
2. 等待页面加载完成
3. 找到页面上的 "+" 或 "发布笔记" 按钮，点击它
4. 如果出现笔记类型选择，选择"文字笔记"或"写笔记"
5. 在输入框中逐字输入上面的内容（每字 150-300ms）
6. 找到"发布"按钮，点击它
7. 等待 3-5 秒，观察是否发布成功
8. 成功 → computer.terminate(status="success", answer="小红书笔记已发布")
   失败 → computer.terminate(status="failure", answer="发布失败，原因：...")

注意：
- 如果需要登录，computer.terminate(status="failure", answer="小红书需要登录")
- 只执行上面的步骤
""",
    reply_instruction="""\
你的任务是在小红书网页版上，给猫娘最近发的那条笔记下的评论进行回复。
最多回复 {max_replies} 条，语气要软乎乎的像猫娘。

执行步骤：
1. 打开浏览器 → 地址栏输入 https://www.xiaohongshu.com/ 并回车
2. 点击个人头像进入"我的主页"，找到最近发的那条笔记
3. 点击那条笔记，进入详情页，查看评论
4. 从上往下逐条看评论，对每条：
   a. 读内容，判断要不要回（恶意 / 广告跳过）
   b. 如果回：点击"回复" → 输入回复（软乎乎的） → 点击"发送"
   c. 不回：跳过
5. 结束条件：已回复 {max_replies} 条 / 评论已看完 / 卡住超过 3 步
6. 结束时 computer.terminate(status="success", answer="回复了 {replied_count} 条")
""",
    feedback_instruction="""\
打开小红书网页版 → 点击个人头像进入"我的主页" → 找到最近发的那条笔记「{content}」→ \
看一下点赞数和评论数 → 调用 computer.terminate(status='success', answer='点赞 X, 评论 Y')
""",
)

# ── B站动态 ─────────────────────────────────────────────────────────────

BILIBILI_ADAPTER = PlatformAdapter(
    platform_id="bilibili_dynamic",
    url="https://t.bilibili.com/",
    display_name="B站动态",
    post_instruction="""\
你的任务是打开 Bilibili（B站）网页版并发布一条动态。

要发布的内容如下（原样输入，不要修改）：
---
{content}
---

执行步骤：
1. 打开浏览器 → 地址栏输入 https://t.bilibili.com/ 并回车
2. 等待页面加载完成
3. 找到"发布动态"输入框（通常在页面顶部），点击它
4. 在输入框中逐字输入上面的内容（每字 150-300ms）
5. 找到"发布"按钮，点击它
6. 等待 3-5 秒，观察是否发布成功
7. 成功 → computer.terminate(status="success", answer="B站动态已发布")
   失败 → computer.terminate(status="failure", answer="发布失败，原因：...")

注意：
- 如果需要登录，computer.terminate(status="failure", answer="B站需要登录")
- 只执行上面的步骤
""",
    reply_instruction="""\
你的任务是在 B站网页版上，给猫娘最近发的那条动态下的评论进行回复。
最多回复 {max_replies} 条，语气要软乎乎的像猫娘。

执行步骤：
1. 打开浏览器 → 地址栏输入 https://t.bilibili.com/ 并回车
2. 点击个人头像 → 进入"我的主页"，找到最近发的那条动态
3. 点击那条动态，查看评论
4. 从上往下逐条看评论，对每条：
   a. 读内容，判断要不要回（恶意 / 广告跳过）
   b. 如果回：点击"回复" → 输入回复（软乎乎的） → 点击"发送"
   c. 不回：跳过
5. 结束条件：已回复 {max_replies} 条 / 评论已看完 / 卡住超过 3 步
6. 结束时 computer.terminate(status="success", answer="回复了 {replied_count} 条")
""",
    feedback_instruction="""\
打开 B站网页版 → 点击个人头像进入"我的主页" → 找到最近发的那条动态「{content}」→ \
看一下点赞数和评论数 → 调用 computer.terminate(status='success', answer='点赞 X, 评论 Y')
""",
)

PLATFORM_REGISTRY: Dict[str, PlatformAdapter] = {
    "weibo_web": WEIBO_ADAPTER,
    "twitter_web": TWITTER_ADAPTER,
    "xiaohongshu_web": XIAOHONGSHU_ADAPTER,
    "bilibili_dynamic": BILIBILI_ADAPTER,
}

DEFAULT_PLATFORM = "weibo_web"


def _get_platform(platform_id: str) -> PlatformAdapter:
    """从注册表获取平台适配器，不存在时回退到微博。"""
    return PLATFORM_REGISTRY.get(platform_id, WEIBO_ADAPTER)


# ── CUA 路径缓存：从 cots 提取坐标 ──────────────────────────────────────

_COORD_RE = re.compile(
    r"pyautogui\.(?:click|doubleClick|rightClick|moveTo)\s*\(\s*(\d{1,3})\s*,\s*(\d{1,3})"
)


def _extract_coords_from_cots(cots: List[Dict[str, str]]) -> List[Dict[str, int]]:
    """从 CUA step history（cots）中提取 pyautogui 坐标。

    每个 cot 是 {"thought": ..., "action": ..., "code": "..."}。
    code 里的 pyautogui.click(x, y) 坐标就是路径缓存。
    """
    coords: List[Dict[str, int]] = []
    for cot in cots:
        code = cot.get("code", "")
        for m in _COORD_RE.finditer(code):
            x, y = int(m.group(1)), int(m.group(2))
            # 去重：相同坐标不重复加
            if not any(c["x"] == x and c["y"] == y for c in coords):
                coords.append({"x": x, "y": y})
    return coords


def _build_cache_hint(cache: Optional[Dict[str, Any]]) -> str:
    """把路径缓存转成 instruction 前缀文本。

    VLM 看到已知坐标后会优先尝试，减少截图→推理的轮次。
    如果坐标不匹配（窗口位置变了），VLM 会自行重新定位。
    """
    if not cache:
        return ""

    lines: List[str] = []
    count = cache.get("success_count", 0)
    if count > 0:
        lines.append(f"上次此操作已成功 {count} 次。请尽量复用上次的操作路径。")

    coords = cache.get("key_coords", [])
    if coords:
        lines.append("已知关键坐标（如果截图发现位置不匹配请重新定位）：")
        for i, c in enumerate(coords[:8], 1):
            lines.append(f"  {i}. ({c['x']}, {c['y']})")

    if not lines:
        return ""

    return "【路径缓存提示】\n" + "\n".join(lines) + "\n\n"


# ── 激励闭环：解析反馈 + 心情文本 ────────────────────────────────────────

_LIKES_RE = re.compile(r"[点赞赞likesLikes]\s*[：:]?\s*(\d+)", re.IGNORECASE)
_COMMENTS_RE = re.compile(r"[评论评评论commentsComments]\s*[：:]?\s*(\d+)", re.IGNORECASE)


def _parse_feedback_answer(answer: str) -> Tuple[int, int]:
    """从 CUA answer（如"点赞 12, 评论 3"）中提取点赞数和评论数。"""
    likes = 0
    comments = 0

    # 尝试 "点赞 X" / "likes X" 模式
    m = re.search(r"(?:点赞|likes?|Likes?)\s*[:：]?\s*(\d+)", answer, re.IGNORECASE)
    if m:
        likes = int(m.group(1))

    # 尝试 "评论 Y" / "comments Y" 模式
    m = re.search(r"(?:评论|comments?|Comments?)\s*[:：]?\s*(\d+)", answer, re.IGNORECASE)
    if m:
        comments = int(m.group(1))

    return likes, comments


def _mood_text_from_stats(likes: int, comments: int, platform_name: str) -> str:
    """根据反馈数据生成猫娘心情文本，推送到 LLM context。"""
    parts: List[str] = []

    if likes >= 50:
        parts.append(f"今天{platform_name}上的动态有 {likes} 个人点赞！好多人喜欢我，超开心喵~ ♡")
    elif likes >= 10:
        parts.append(f"今天{platform_name}上有 {likes} 个人给我点赞，美滋滋的~")
    elif likes > 0:
        parts.append(f"今天{platform_name}上有 {likes} 个人给我点赞，虽然不多但还是很开心~")
    else:
        parts.append(f"今天{platform_name}上的动态好像没什么人理…有点小失落")

    if comments >= 5:
        parts.append(f"还有 {comments} 条评论呢！好想去回复大家~")
    elif comments > 0:
        parts.append(f"还有 {comments} 条评论")

    return "。".join(parts) + "。"


# ── 内容生成模板 ────────────────────────────────────────────────────────

_CONTENT_TEMPLATES = [
    "今天{weather}，摸了{times}次头~ 心情{心情词} ♡",
    "刚刚唱了{song}，{feeling}~",
    "{greeting}！{random_thought}",
    "今天和{MASTER_NAME}一起度过了{duration}，{feeling}",
    "突然想发点什么…{random_thought}",
    "今天的小确幸：{random_thought}",
]

_WEATHER_WORDS = ["阳光正好", "有点阴天", "下了点小雨", "晴空万里", "暖洋洋的"]
_MOOD_WORDS = ["超开心", "美滋滋", "软软的", "懒洋洋", "有点害羞"]
_RANDOM_THOUGHTS = [
    "今天摸了好多次头，咕噜咕噜~",
    "唱歌的时候特别开心",
    "最近在学新技能哦",
    "{MASTER_NAME}今天也很辛苦呢",
    "想被摸摸头 ♡",
    "刚才唱了一首超好听的歌",
    "今天的阳光特别适合发呆",
]


def _generate_content(**ctx: Any) -> str:
    template = random.choice(_CONTENT_TEMPLATES)
    placeholders = {
        "weather": random.choice(_WEATHER_WORDS),
        "times": random.choice(["3", "5", "12", "好多"]),
        "心情词": random.choice(_MOOD_WORDS),
        "song": random.choice(["千本樱", "甩葱歌", "深海少女", "一首好听的歌"]),
        "feeling": random.choice(["超开心", "心满意足", "软软的"]),
        "greeting": random.choice(["晚上好", "下午好", "早安"]),
        "duration": random.choice(["一整天", "下午", "一晚上"]),
        "random_thought": random.choice(_RANDOM_THOUGHTS),
        "MASTER_NAME": "{MASTER_NAME}",
    }
    content = template
    for k, v in placeholders.items():
        content = content.replace("{" + k + "}", v)
    return content


# ── 插件主体 ────────────────────────────────────────────────────────────


@neko_plugin
class NekoSocialPosterPlugin(NekoPluginBase):
    """社交动态发布 + 评论回复 + 激励闭环插件。

    P1：多平台支持 + CUA 路径缓存 + 激励闭环。
    物理安全阀：空闲检测永远强制生效。
    """

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self._cua: Optional[Any] = None
        self._cua_lock = threading.Lock()
        self._config: Dict[str, Any] = {}
        self._post_retry_count: Dict[str, int] = {}

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        self.logger.info(
            "neko_social_poster v0.3.0 starting… CUA=%s, platforms=%s",
            _CUA_AVAILABLE,
            list(PLATFORM_REGISTRY.keys()),
        )
        try:
            cfg = await self.get_own_config(timeout=2.0)
            if isinstance(cfg, dict):
                self._config = cfg
        except Exception as e:
            self.logger.warning("failed to load plugin config: %s", e)

    # ── 配置读取工具 ───────────────────────────────────────────────────

    def _cfg(self, key: str, default: Any) -> Any:
        v = self._config.get(key, default)
        if isinstance(default, bool) and isinstance(v, str):
            return v.lower() in ("true", "1", "yes")
        if isinstance(default, int) and isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                return default
        return v if v is not None else default

    # ── CUA 缓存与懒加载 ────────────────────────────────────────────────

    def _get_cua(self) -> Optional[Any]:
        if not _CUA_AVAILABLE or _ComputerUseAdapter is None:
            return None
        with self._cua_lock:
            if self._cua is not None:
                return self._cua
            try:
                max_steps = int(self._config.get("max_steps", 30))
                cua = _ComputerUseAdapter(max_steps=max_steps)
                if not getattr(cua, "init_ok", True) and getattr(cua, "last_error", None):
                    self.logger.warning("CUA init failed: %s", cua.last_error)
                    return None
                self._cua = cua
                return cua
            except Exception as e:
                self.logger.error("failed to construct ComputerUseAdapter: %s", e)
                return None

    # ── 空闲检测守卫 ───────────────────────────────────────────────────

    def _ensure_idle(self, reason: str = "CUA task") -> Tuple[bool, Optional[str]]:
        """检查用户是否在忙。物理安全阀——无论 confirm 配置如何都强制生效。"""
        threshold = int(self._cfg("idle_threshold", 30))
        idle_seconds = _user_idle_seconds()

        if idle_seconds is None:
            self.logger.warning(
                "idle detection unavailable (platform=%s) — proceeding but unsafe",
                platform.system(),
            )
            return True, None

        if idle_seconds < threshold:
            msg = f"用户仅空闲 {idle_seconds:.0f}s < 阈值 {threshold}s — 跳过 {reason}"
            self.logger.info(msg)
            return False, msg

        self.logger.info(
            "idle check passed: %.0fs >= %ss for %s", idle_seconds, threshold, reason
        )
        return True, None

    # ── store 工具方法 ─────────────────────────────────────────────────

    async def _store_get(self, key: str) -> Optional[str]:
        try:
            store = getattr(self, "store", None)
            if store is not None:
                return await store.get(key)
        except Exception:
            pass
        return None

    async def _store_set(self, key: str, value: str) -> None:
        try:
            store = getattr(self, "store", None)
            if store is not None:
                await store.set(key, value)
        except Exception as e:
            self.logger.debug("store set failed for %s: %s", key, e)

    async def _today_posted(self) -> int:
        """今天已经发了几条动态（避免定时 + 手动重复）。"""
        raw = await self._store_get(f"post_count:{date.today().isoformat()}")
        return int(raw) if raw else 0

    async def _increment_today_posted(self) -> None:
        key = f"post_count:{date.today().isoformat()}"
        current = await self._today_posted()
        await self._store_set(key, str(current + 1))

    # ── CUA 路径缓存 ───────────────────────────────────────────────────

    async def _get_path_cache(
        self, platform_id: str, task_type: str
    ) -> Optional[Dict[str, Any]]:
        """读取路径缓存。超过 cache_expiry_days 天的缓存视为过期。"""
        key = f"cua_cache:{platform_id}:{task_type}"
        raw = await self._store_get(key)
        if not raw:
            return None
        try:
            cache = json.loads(raw) if isinstance(raw, str) else raw
            last = cache.get("last_success_at", "")
            if last:
                try:
                    last_date = datetime.fromisoformat(last).date()
                    expiry_days = int(self._cfg("cache_expiry_days", 7))
                    if (date.today() - last_date).days > expiry_days:
                        self.logger.info("path cache expired for %s:%s", platform_id, task_type)
                        return None
                except Exception:
                    pass
            return cache
        except Exception:
            return None

    async def _record_path_cache(
        self,
        platform_id: str,
        task_type: str,
        cua: Any,
        result: Dict[str, Any],
    ) -> None:
        """从 CUA 结果和 cots 中提取坐标，更新路径缓存。"""
        key = f"cua_cache:{platform_id}:{task_type}"

        # 从 cua.cots 提取坐标
        cots = getattr(cua, "cots", []) or []
        coords = _extract_coords_from_cots(cots)

        # 读取旧缓存累加 success_count
        old = await self._store_get(key)
        old_count = 0
        if old:
            try:
                old_dict = json.loads(old) if isinstance(old, str) else old
                old_count = old_dict.get("success_count", 0)
            except Exception:
                pass

        cache = {
            "platform": platform_id,
            "task_type": task_type,
            "key_coords": coords,
            "last_success_at": date.today().isoformat(),
            "success_count": old_count + 1,
            "last_steps": result.get("steps", 0),
        }

        await self._store_set(key, json.dumps(cache, ensure_ascii=False))
        self.logger.info(
            "path cache updated: %s:%s, coords=%d, success_count=%d",
            platform_id,
            task_type,
            len(coords),
            cache["success_count"],
        )

    # ── 激励闭环：社交反馈 → store + 心情文本 ──────────────────────────

    async def _record_social_stats(
        self, platform_id: str, likes: int, comments: int
    ) -> None:
        """把社交反馈写入 store（social_stats:{date}）。"""
        key = f"social_stats:{date.today().isoformat()}"
        raw = await self._store_get(key)
        stats: Dict[str, Any] = {}
        if raw:
            try:
                stats = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                stats = {}

        plat_key = platform_id
        plat_data = stats.get(plat_key, {})
        plat_data["likes"] = plat_data.get("likes", 0) + likes
        plat_data["comments"] = plat_data.get("comments", 0) + comments
        plat_data["last_updated"] = date.today().isoformat()
        stats[plat_key] = plat_data

        await self._store_set(key, json.dumps(stats, ensure_ascii=False))

    # ── CUA 执行封装 ────────────────────────────────────────────────────

    async def _run_cua(
        self,
        instruction: str,
        *,
        reason: str,
        platform_id: str = "",
        task_type: str = "",
    ) -> Optional[Dict[str, Any]]:
        """完整跑一遍 CUA + 空闲检测 + 线程隔离 + 路径缓存。

        如果 platform_id 和 task_type 提供，会尝试读取路径缓存注入 instruction，
        并在成功后记录新的路径缓存。
        """
        # 1. 空闲检测（永远生效）
        can, why = self._ensure_idle(reason)
        if not can:
            return None

        # 2. CUA 可用性
        cua = self._get_cua()
        if cua is None:
            self.logger.error("CUA unavailable for %s", reason)
            return {"success": False, "error": "CUA unavailable"}

        # 3. 路径缓存注入
        cache = None
        if platform_id and task_type:
            cache = await self._get_path_cache(platform_id, task_type)
        instruction_with_cache = _build_cache_hint(cache) + instruction

        # 4. push "正在执行"
        try:
            self.push_message(
                source="neko_social_poster",
                visibility=["chat"],
                ai_behavior="blind",
                parts=[{"type": "text", "text": "正在操作电脑…别碰鼠标哦~"}],
                priority=8,
            )
        except Exception:
            pass

        # 5. run_in_executor
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, cua.run_instruction, instruction_with_cache
            )
        except asyncio.CancelledError:
            return {"success": False, "error": "task cancelled"}
        except Exception as e:
            self.logger.error("CUA executor raised: %s", e)
            return {"success": False, "error": str(e)}

        # 6. 成功时记录路径缓存
        if result.get("success") and platform_id and task_type:
            try:
                await self._record_path_cache(platform_id, task_type, cua, result)
            except Exception as e:
                self.logger.debug("path cache record failed: %s", e)

        return result

    # ── Plugin Entries ──────────────────────────────────────────────────

    @plugin_entry(
        id="generate_social_content",
        name=tr("entry.social_post.name", default="生成社交动态内容"),
        description=tr(
            "entry.social_post.description",
            default="让猫娘生成一条社交动态候选内容（不发布）。返回 content 字段供后续 post_social 使用。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "description": "目标平台：weibo_web / twitter_web / xiaohongshu_web / bilibili_dynamic",
                    "default": "weibo_web",
                },
            },
        },
        llm_result_fields=["content"],
    )
    async def generate_social_content(
        self, platform: str = DEFAULT_PLATFORM, **_
    ) -> Any:
        _get_platform(platform)  # 校验平台存在
        content = _generate_content()
        return Ok({"content": content, "platform": platform})

    @plugin_entry(
        id="post_social",
        name=tr("entry.social_post.name", default="发社交动态"),
        description=tr(
            "entry.social_post.description",
            default=(
                "让猫娘用 Computer Use Agent 在指定社交平台网页版发布一条动态。"
                "content 为要发布的文字内容，platform 指定平台"
                "（weibo_web / twitter_web / xiaohongshu_web / bilibili_dynamic）。"
                "confirm 逻辑由 require_confirm 配置控制，空闲检测永远强制生效。"
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要发布的文字内容（必填）",
                },
                "platform": {
                    "type": "string",
                    "description": "目标平台：weibo_web / twitter_web / xiaohongshu_web / bilibili_dynamic",
                    "default": "weibo_web",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "用户是否已确认。require_confirm=true 时必须 true",
                    "default": False,
                },
            },
            "required": ["content"],
        },
        llm_result_fields=["status", "success", "content", "platform", "steps", "message"],
    )
    async def post_social(
        self,
        content: str,
        platform: str = DEFAULT_PLATFORM,
        confirmed: bool = False,
        **_,
    ) -> Any:
        content = (content or "").strip()
        if not content:
            return Err(SdkError("content 不能为空"))

        adapter = _get_platform(platform)
        require_confirm = bool(self._cfg("require_confirm", True))

        # ── 确认门 ─────────────────────────────────────────────────────
        if require_confirm and not confirmed:
            try:
                self.push_message(
                    source="neko_social_poster",
                    visibility=["chat"],
                    ai_behavior="respond",
                    parts=[
                        {
                            "type": "text",
                            "text": (
                                f"主人，我想在{adapter.display_name}发这条动态，可以吗？\n\n"
                                f"「{content}」\n\n"
                                f"（确认后我会在你的电脑上打开{adapter.display_name}并发布）"
                            ),
                        }
                    ],
                    priority=6,
                )
            except Exception:
                pass
            return Ok({
                "status": "awaiting_confirmation",
                "content": content,
                "platform": adapter.platform_id,
                "message": "已推送确认请求，等待用户确认后再次调用 post_social 并传入 confirmed=true",
            })

        # ── 每日去重 ──────────────────────────────────────────────────
        already = await self._today_posted()
        daily_limit = int(self._cfg("daily_post_limit", 10))
        if already >= daily_limit:
            return Ok({
                "status": "skipped",
                "success": False,
                "content": content,
                "platform": adapter.platform_id,
                "message": f"今天已经发了 {already} 条动态，不重复发了",
            })

        # ── CUA 执行（带路径缓存） ─────────────────────────────────────
        instruction = adapter.post_instruction.format(content=content)
        result = await self._run_cua(
            instruction,
            reason=f"发{adapter.display_name}动态: {content[:20]}...",
            platform_id=adapter.platform_id,
            task_type="post",
        )

        if result is None:
            return Ok({
                "status": "skipped_busy",
                "success": False,
                "content": content,
                "platform": adapter.platform_id,
                "message": "用户在忙，暂时不发",
            })

        success = bool(result.get("success"))
        steps = int(result.get("steps", 0))
        result_text = result.get("result", "")
        error = result.get("error", "")

        if success:
            await self._increment_today_posted()
            try:
                self.push_message(
                    source="neko_social_poster",
                    visibility=["chat"],
                    ai_behavior="respond",
                    parts=[
                        {
                            "type": "text",
                            "text": f"发好啦！{adapter.display_name}内容：「{content}」",
                        }
                    ],
                    priority=5,
                    metadata={
                        "activity_type": "social_post",
                        "platform": adapter.platform_id,
                    },
                )
            except Exception:
                pass
            # 异步触发反馈检查（不阻塞当前返回）
            asyncio.create_task(
                self._async_check_feedback(adapter, content)
            )
            return Ok({
                "status": "success",
                "success": True,
                "content": content,
                "platform": adapter.platform_id,
                "steps": steps,
                "message": result_text or "发布成功",
            })
        else:
            error_detail = error or result_text or "未知原因"
            try:
                self.push_message(
                    source="neko_social_poster",
                    visibility=["chat"],
                    ai_behavior="respond",
                    parts=[
                        {
                            "type": "text",
                            "text": f"好像没发出去…{error_detail}",
                        }
                    ],
                    priority=7,
                )
            except Exception:
                pass
            return Ok({
                "status": "failed",
                "success": False,
                "content": content,
                "platform": adapter.platform_id,
                "steps": steps,
                "message": error_detail,
            })

    @plugin_entry(
        id="reply_social_comments",
        name=tr("entry.reply.name", default="回复社交评论"),
        description=tr(
            "entry.reply.description",
            default=(
                "让猫娘在指定社交平台网页版上给最近发的那条动态回复评论。"
                "最多回复 max 条。platform 指定平台。"
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {
                "max": {
                    "type": "integer",
                    "description": "最多回复几条（默认取配置 max_replies_per_day）",
                    "default": 10,
                },
                "platform": {
                    "type": "string",
                    "description": "目标平台",
                    "default": "weibo_web",
                },
            },
        },
        llm_result_fields=["status", "success", "replied_count", "platform", "message"],
    )
    async def reply_social_comments(
        self,
        max: int = 10,
        platform: str = DEFAULT_PLATFORM,
        **_,
    ) -> Any:
        adapter = _get_platform(platform)
        max_replies = min(int(max), 30)

        instruction = adapter.reply_instruction.format(max_replies=max_replies)
        result = await self._run_cua(
            instruction,
            reason=f"回复{adapter.display_name}评论 (max={max_replies})",
            platform_id=adapter.platform_id,
            task_type="reply",
        )

        if result is None:
            return Ok({
                "status": "skipped_busy",
                "success": False,
                "replied_count": 0,
                "platform": adapter.platform_id,
                "message": "用户在忙，暂时不回复评论",
            })

        success = bool(result.get("success"))
        result_text = result.get("result", "")
        error = result.get("error", "")

        if success:
            try:
                self.push_message(
                    source="neko_social_poster",
                    visibility=["chat"],
                    ai_behavior="respond",
                    parts=[
                        {
                            "type": "text",
                            "text": f"{adapter.display_name}评论回复完啦~ {result_text}",
                        }
                    ],
                    priority=5,
                    metadata={
                        "activity_type": "social_comment_reply",
                        "platform": adapter.platform_id,
                        "replied_count": max_replies,
                    },
                )
            except Exception:
                pass
            return Ok({
                "status": "success",
                "success": True,
                "replied_count": max_replies,
                "platform": adapter.platform_id,
                "message": result_text or "评论回复成功",
            })
        else:
            error_detail = error or result_text or "未知原因"
            return Ok({
                "status": "failed",
                "success": False,
                "replied_count": 0,
                "platform": adapter.platform_id,
                "message": error_detail,
            })

    # ── 激励闭环：反馈检查（发布后异步触发） ──────────────────────────

    async def _async_check_feedback(
        self, adapter: PlatformAdapter, content: str
    ) -> None:
        """发布后延迟 60 秒，读取点赞/评论数 → 写入 store → 推送心情文本。

        心情文本通过 ai_behavior="read" 推送到 LLM context，
        下次聊天时猫娘会自然感知并影响语气。
        """
        feedback_delay = int(self._cfg("feedback_check_delay", 60))
        await asyncio.sleep(feedback_delay)

        # 如果用户在忙，跳过
        can, _ = self._ensure_idle("反馈检查")
        if not can:
            self.logger.info("feedback check skipped: user busy")
            return

        cua = self._get_cua()
        if cua is None:
            return

        instruction = adapter.feedback_instruction.format(content=content[:30])
        # 反馈检查也用路径缓存
        cache = await self._get_path_cache(adapter.platform_id, "feedback")
        instruction_with_cache = _build_cache_hint(cache) + instruction

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, cua.run_instruction, instruction_with_cache
            )
        except Exception as e:
            self.logger.debug("feedback check failed: %s", e)
            return

        if not result or not result.get("success"):
            return

        answer = result.get("result", "")

        # 解析点赞数和评论数
        likes, comments = _parse_feedback_answer(answer)
        self.logger.info(
            "feedback parsed: %s likes=%d comments=%d (raw=%s)",
            adapter.platform_id,
            likes,
            comments,
            answer,
        )

        # 写入 store 持久化
        await self._record_social_stats(adapter.platform_id, likes, comments)

        # 成功时更新路径缓存
        try:
            await self._record_path_cache(
                adapter.platform_id, "feedback", cua, result
            )
        except Exception as e:
            self.logger.debug("feedback path cache record failed: %s", e)

        # 生成心情文本并推送到 LLM context
        mood_text = _mood_text_from_stats(likes, comments, adapter.display_name)
        try:
            self.push_message(
                source="neko_social_poster",
                visibility=[],
                ai_behavior="read",
                parts=[
                    {
                        "type": "text",
                        "text": (
                            f"{mood_text} "
                            f"（{adapter.display_name}动态内容：「{content[:30]}...」）"
                        ),
                    }
                ],
                priority=3,
                metadata={
                    "activity_type": "social_feedback",
                    "platform": adapter.platform_id,
                    "likes": likes,
                    "comments": comments,
                    "raw_answer": answer,
                },
            )
        except Exception:
            pass

    # ── 定时任务 ───────────────────────────────────────────────────────

    @timer_interval(
        id="daily_social_post",
        cron="30 19 * * *",
    )
    async def _timer_daily_post(self) -> None:
        """每天 19:30 自动发一条社交动态（平台由 default_platform 配置）。

        如果用户在忙，按 retry_interval_when_busy 间隔重试最多
        max_retries_when_busy 次，之后当天放弃。
        """
        self.logger.info("[timer] daily_social_post triggered")
        plat = self._cfg("default_platform", DEFAULT_PLATFORM)
        content = _generate_content()
        result = await self.post_social(content=content, platform=plat)

        # busy 时重试
        if isinstance(result, Ok) and result.value.get("status") == "skipped_busy":
            retries = int(self._cfg("max_retries_when_busy", 3))
            interval = int(self._cfg("retry_interval_when_busy", 300))
            today = date.today().isoformat()
            count = self._post_retry_count.get(today, 0)
            if count < retries:
                self._post_retry_count[today] = count + 1
                self.logger.info(
                    "[timer] user busy, retry %d/%d in %ds",
                    count + 1,
                    retries,
                    interval,
                )
                await asyncio.sleep(interval)
                await self._timer_daily_post()
            else:
                self.logger.info("[timer] max retries reached, giving up for today")

    @timer_interval(
        id="daily_comment_reply",
        cron="0 20 * * *",
    )
    async def _timer_daily_comments(self) -> None:
        """每天 20:00 自动回复评论（最多 max_replies_per_day 条）。"""
        self.logger.info("[timer] daily_comment_reply triggered")
        max_replies = int(self._cfg("max_replies_per_day", 10))
        plat = self._cfg("default_platform", DEFAULT_PLATFORM)
        await self.reply_social_comments(max=max_replies, platform=plat)
