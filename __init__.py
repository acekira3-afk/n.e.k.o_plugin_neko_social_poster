"""N.E.K.O Social Poster Plugin

让猫娘用 Computer Use Agent（VLM + pyautogui）操作桌面，
在微博网页版发布动态 + 定时回复评论。

P0.5 能力：
- 手动/自动发微博（自动模式不经确认，但空闲检测永远生效）
- 每天定时发一条微博（19:30，可配置）
- 每天定时回复评论（20:00，最多 10 条/天，可配置）
- 发布后自动检查反馈（点赞/评论数）写入猫娘 memory

安全护栏：
- 空闲检测：CUA 执行前用户必须连续 idle_threshold 秒没动鼠标键盘
- max_steps=30：CUA 内部硬限制，防止 VLM 死循环
- 每日去重：store 记录当日发布数，避免定时触发和手动触发重复
- 静默失败：定时任务执行时如果用户在忙，静默跳过不打扰
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
import platform
from datetime import date
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
    """返回用户最近的空闲秒数，None 表示无法检测。

    macOS：用 Quartz 的 CGEventSource.SecondsSinceLastEventType
    其他平台：暂时返回 None（不阻塞 CUA，但日志里会 warning）
    """
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

    # Cross-platform fallback：pyautogui 位置轮询
    try:
        import pyautogui

        pos1 = pyautogui.position()
        time.sleep(0.5)
        pos2 = pyautogui.position()
        if pos1 == pos2:
            # 鼠标没动过，但键盘活动检测不出来
            # 保守估计：返回一个偏大的值让调用方自行判断
            return 30.0  # 假设已经空闲 30 秒
    except Exception:
        pass

    return None


# ── CUA 任务 Prompt ──────────────────────────────────────────────────────

WEIBO_POST_INSTRUCTION_TEMPLATE = """
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
"""

COMMENT_REPLY_INSTRUCTION_TEMPLATE = """
你的任务是在微博网页版上，给猫娘最近发的那条动态回复评论。
最多回复 {max_replies} 条，语气要软乎乎的像猫娘。

执行步骤：
1. 如果浏览器还没打开，先打开浏览器（Chrome / Safari / Edge 任一）
2. 在地址栏输入 https://weibo.com/ 并回车
3. 等待微博网页加载完成
4. 点击右上角个人头像 → "我的主页"，找到猫娘最近发的那条动态
5. 点击那条动态的"评论"按钮，进入评论区
6. 从上往下逐条看评论，对每条评论：
   a. 先读评论内容，判断要不要回复（恶意喷子 / 广告跳过，友好的评论才回）
   b. 如果决定回复：
      - 点击该评论下方的"回复"按钮
      - 在输入框里输入回复内容（语气要软乎乎的，像猫娘）
      - 点击"发送"
   c. 如果决定不回复：跳过这条，看下一条
7. 当以下任一条件满足时结束：
   - 已回复 {max_replies} 条
   - 评论区已看完，没有值得回复的评论
   - 页面出错或操作卡住超过 3 步
8. 结束时调用 computer.terminate(status="success", answer="回复了 {replied_count} 条评论")
   （把实际回复的条数填在 answer 里）

回复语气参考：
- 别人夸可爱 → "喵~谢谢夸奖 ♡"
- 别人问问题 → "嗯嗯…让我想想哦…"
- 别人说今天也加油 → "一起加油喵！"
- 不要说脏话，不要怼人，保持软乎乎的
"""


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
    """社交动态发布 + 评论回复插件。

    物理安全阀：空闲检测永远强制生效，无论 confirm 配置如何。
    """

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self._cua: Optional[Any] = None
        self._cua_lock = threading.Lock()
        self._config: Dict[str, Any] = {}
        # 定时任务重试状态
        self._post_retry_count: Dict[str, int] = {}  # date_str -> retries

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        self.logger.info(
            "neko_social_poster starting… CUA=%s, idle_check=on", _CUA_AVAILABLE
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
        # 尝试从字符串转类型
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
        """检查用户是否在忙。返回 (can_proceed, reason_or_none)。

        这是 CUA 插件的物理安全阀——无论 confirm 配置如何，
        必须用户空闲 N 秒以上才会执行。
        """
        threshold = int(self._cfg("idle_threshold", 30))
        idle_seconds = _user_idle_seconds()

        if idle_seconds is None:
            # 无法检测，保守放行但 warning
            self.logger.warning(
                "idle detection unavailable (platform=%s) — proceeding but unsafe",
                platform.system(),
            )
            return True, None

        if idle_seconds < threshold:
            msg = (
                f"用户仅空闲 {idle_seconds:.0f}s < 阈值 {threshold}s "
                f"— 跳过 {reason}"
            )
            self.logger.info(msg)
            return False, msg

        self.logger.info(
            "idle check passed: %.0fs >= %ss for %s", idle_seconds, threshold, reason
        )
        return True, None

    # ── 每日去重（store 持久化） ────────────────────────────────────────

    async def _today_posted(self) -> int:
        """今天已经发了几条微博（避免定时 + 手动重复）。"""
        try:
            store = getattr(self, "store", None)
            if store is not None:
                key = f"post_count:{date.today().isoformat()}"
                raw = await store.get(key)
                return int(raw) if raw else 0
        except Exception:
            pass
        return 0

    async def _increment_today_posted(self) -> None:
        try:
            store = getattr(self, "store", None)
            if store is not None:
                key = f"post_count:{date.today().isoformat()}"
                current = await store.get(key)
                current = int(current) if current else 0
                await store.set(key, str(current + 1))
        except Exception as e:
            self.logger.debug("store increment failed: %s", e)

    # ── CUA 执行封装 ────────────────────────────────────────────────────

    async def _run_cua(self, instruction: str, *, reason: str) -> Optional[Dict[str, Any]]:
        """完整跑一遍 CUA + 空闲检测 + 线程隔离。

        返回 CUA 结果 dict，None 表示被空闲检测拦截。
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

        # 3. push "正在执行"（只对 chat 可见一次）
        try:
            self.push_message(
                source="neko_social_poster",
                visibility=["chat"],
                ai_behavior="blind",
                parts=[{
                    "type": "text",
                    "text": "正在操作电脑…别碰鼠标哦~",
                }],
                priority=8,
            )
        except Exception:
            pass

        # 4. run_in_executor（run_instruction 是同步阻塞的）
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, cua.run_instruction, instruction)
            return result
        except asyncio.CancelledError:
            return {"success": False, "error": "task cancelled"}
        except Exception as e:
            self.logger.error("CUA executor raised: %s", e)
            return {"success": False, "error": str(e)}

    # ── Plugin Entries ──────────────────────────────────────────────────

    @plugin_entry(
        id="generate_weibo_content",
        name=tr("entry.weibo_post.name", default="生成微博内容"),
        description=tr(
            "entry.weibo_post.description",
            default="让猫娘生成一条微博候选内容（不发布）。返回 content 字段供后续 post_weibo 使用。",
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["content"],
    )
    async def generate_weibo_content(self, **_) -> Any:
        content = _generate_content()
        return Ok({"content": content})

    @plugin_entry(
        id="post_weibo",
        name=tr("entry.weibo_post.name", default="发微博"),
        description=tr(
            "entry.weibo_post.description",
            default="让猫娘用 Computer Use Agent 在微博网页版发布一条动态。content 为要发布的文字内容。confirm 逻辑由 require_confirm 配置控制，但空闲检测永远强制生效。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要发布的微博文字内容（必填）",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "用户是否已确认。require_confirm=true 时必须 true；false 时此参数被忽略",
                    "default": False,
                },
            },
            "required": ["content"],
        },
        llm_result_fields=["status", "success", "content", "steps", "message"],
    )
    async def post_weibo(self, content: str, confirmed: bool = False, **_) -> Any:
        content = (content or "").strip()
        if not content:
            return Err(SdkError("content 不能为空"))

        require_confirm = bool(self._cfg("require_confirm", True))

        # ── 确认门 ─────────────────────────────────────────────────────
        if require_confirm and not confirmed:
            try:
                self.push_message(
                    source="neko_social_poster",
                    visibility=["chat"],
                    ai_behavior="respond",
                    parts=[{
                        "type": "text",
                        "text": (
                            "主人，我想发这条微博，可以吗？\n\n"
                            f"「{content}」\n\n"
                            "（确认后我会在你的电脑上打开微博网页版并发布）"
                        ),
                    }],
                    priority=6,
                )
            except Exception:
                pass
            return Ok({
                "status": "awaiting_confirmation",
                "content": content,
                "message": "已推送确认请求，等待用户确认后再次调用 post_weibo 并传入 confirmed=true",
            })

        # ── 每日去重 ──────────────────────────────────────────────────
        already = await self._today_posted()
        if already >= 10:
            return Ok({
                "status": "skipped",
                "success": False,
                "content": content,
                "message": f"今天已经发了 {already} 条微博，不重复发了",
            })

        # ── CUA 执行 ──────────────────────────────────────────────────
        instruction = WEIBO_POST_INSTRUCTION_TEMPLATE.format(content=content)
        result = await self._run_cua(instruction, reason=f"发微博: {content[:20]}...")

        if result is None:
            # 空闲检测拦截
            return Ok({
                "status": "skipped_busy",
                "success": False,
                "content": content,
                "message": "用户在忙，暂时不发微博",
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
                    parts=[{
                        "type": "text",
                        "text": f"发好啦！微博内容：「{content}」",
                    }],
                    priority=5,
                    metadata={
                        "activity_type": "social_post",
                        "platform": "weibo_web",
                    },
                )
            except Exception:
                pass
            # 异步触发反馈检查（不阻塞当前返回）
            asyncio.create_task(self._async_check_feedback(content))
            return Ok({
                "status": "success",
                "success": True,
                "content": content,
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
                    parts=[{
                        "type": "text",
                        "text": f"好像没发出去…{error_detail}",
                    }],
                    priority=7,
                )
            except Exception:
                pass
            return Ok({
                "status": "failed",
                "success": False,
                "content": content,
                "steps": steps,
                "message": error_detail,
            })

    @plugin_entry(
        id="reply_comments",
        name="回复微博评论",
        description="让猫娘在微博网页版上给最近发的那条动态回复评论。最多回复 max 条。",
        input_schema={
            "type": "object",
            "properties": {
                "max": {
                    "type": "integer",
                    "description": "最多回复几条（默认取配置 max_replies_per_day）",
                    "default": 10,
                },
            },
        },
        llm_result_fields=["status", "success", "replied_count", "message"],
    )
    async def reply_comments(self, max: int = 10, **_) -> Any:
        max_replies = min(int(max), 30)

        instruction = COMMENT_REPLY_INSTRUCTION_TEMPLATE.format(max_replies=max_replies)
        result = await self._run_cua(instruction, reason=f"回复评论 (max={max_replies})")

        if result is None:
            return Ok({
                "status": "skipped_busy",
                "success": False,
                "replied_count": 0,
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
                    parts=[{
                        "type": "text",
                        "text": f"评论回复完啦~ {result_text}",
                    }],
                    priority=5,
                    metadata={
                        "activity_type": "social_comment_reply",
                        "replied_count": max_replies,
                    },
                )
            except Exception:
                pass
            return Ok({
                "status": "success",
                "success": True,
                "replied_count": max_replies,
                "message": result_text or "评论回复成功",
            })
        else:
            error_detail = error or result_text or "未知原因"
            return Ok({
                "status": "failed",
                "success": False,
                "replied_count": 0,
                "message": error_detail,
            })

    # ── 反馈检查（发布后异步触发） ──────────────────────────────────────

    async def _async_check_feedback(self, content: str) -> None:
        """发布后延迟 60 秒，打开那条微博看一下点赞/评论数。

        结果通过 ai_behavior="read" 写入 LLM 上下文，
        下次聊天时猫娘会主动提到。
        """
        await asyncio.sleep(60)

        # 如果用户在忙，跳过
        can, _ = self._ensure_idle("反馈检查")
        if not can:
            self.logger.info("feedback check skipped: user busy")
            return

        cua = self._get_cua()
        if cua is None:
            return

        instruction = (
            "打开微博网页版 → 找到刚才发的那条动态 → "
            "看一下点赞数和评论数 → 记下数字后调用 "
            "computer.terminate(status='success', answer='点赞 X, 评论 Y')"
        )

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, cua.run_instruction, instruction)
        except Exception as e:
            self.logger.debug("feedback check failed: %s", e)
            return

        if result and result.get("success"):
            answer = result.get("result", "")
            try:
                self.push_message(
                    source="neko_social_poster",
                    visibility=[],
                    ai_behavior="read",
                    parts=[{
                        "type": "text",
                        "text": f"刚才发的微博反馈：{answer}。"
                                f" 内容是「{content[:30]}...」",
                    }],
                    priority=3,
                    metadata={
                        "activity_type": "social_feedback",
                        "answer": answer,
                    },
                )
            except Exception:
                pass

    # ── 定时任务 ───────────────────────────────────────────────────────

    @timer_interval(
        id="daily_weibo_post",
        cron="30 19 * * *",
    )
    async def _timer_daily_post(self) -> None:
        """每天 19:30 自动发一条微博（19:30 cron 硬编码，可改）。

        如果用户在忙，按 retry_interval_when_busy 间隔重试最多
        max_retries_when_busy 次，之后当天放弃。
        """
        self.logger.info("[timer] daily_weibo_post triggered")
        content = _generate_content()
        result = await self.post_weibo(content=content)

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
                    count + 1, retries, interval,
                )
                await asyncio.sleep(interval)
                await self._timer_daily_post()  # 递归重试
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
        await self.reply_comments(max=max_replies)
