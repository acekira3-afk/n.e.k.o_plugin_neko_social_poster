"""N.E.K.O Social Poster Plugin

让猫娘用 Computer Use Agent（VLM + pyautogui）操作桌面，
在微博网页版发布动态。P0 MVP：手动触发、单平台、纯文字。

工作流程：
1. 用户说"帮我发一条微博"
2. LLM 调用 generate_weibo_content 生成候选内容，展示给用户确认
3. 用户确认后，LLM 调用 post_weibo 执行 CUA 发布

安全护栏：
- CUA 执行在独立线程，不阻塞事件循环
- 任务 prompt 明确限制在"打开微博→输入内容→点击发布"范围
- CUA 内部 max_steps=30，防止无限循环
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, Optional

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
)
from plugin.sdk.shared.i18n import tr

# ── Computer Use Adapter 延迟导入 ────────────────────────────────────────
# brain.computer_use 只有在 N.E.K.O 主进程内才存在。插件可能被单独加载
# 做 smoke test，此时导入会失败。用延迟导入 + fallback 让插件在非主进程
# 环境下也能正常 import（只是 CUA 功能不可用）。
_CUA_AVAILABLE = False
_ComputerUseAdapter: Any = None

try:
    from brain.computer_use import ComputerUseAdapter as _CUA

    _ComputerUseAdapter = _CUA
    _CUA_AVAILABLE = True
except Exception as _e:
    _CUA_AVAILABLE = False


# ── CUA 任务 Prompt ──────────────────────────────────────────────────────
# 发给 CUA 的高层指令，让 VLM 自己根据截图定位按钮位置。
# 故意不硬编码坐标——UI 改版时 VLM 能自适应。

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


# ── 内容生成模板（猫娘自动生成的语气） ────────────────────────────────────

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
    """根据随机模板生成一条微博内容。

    未来可以接入更丰富的上下文（猫娘今日心情、跟用户的互动历史等），
    P0 先做随机模板。
    """
    template = random.choice(_CONTENT_TEMPLATES)
    # 用简单的占位符替换，不依赖外部数据
    placeholders = {
        "weather": random.choice(_WEATHER_WORDS),
        "times": random.choice(["3", "5", "12", "好多"]),
        "心情词": random.choice(_MOOD_WORDS),
        "song": random.choice(["千本樱", "甩葱歌", "深海少女", "一首好听的歌"]),
        "feeling": random.choice(["超开心", "心满意足", "软软的"]),
        "greeting": random.choice(["晚上好", "下午好", "早安"]),
        "duration": random.choice(["一整天", "下午", "一晚上"]),
        "random_thought": random.choice(_RANDOM_THOUGHTS),
        "MASTER_NAME": "{MASTER_NAME}",  # 让 host 替换
    }
    content = template
    for k, v in placeholders.items():
        content = content.replace("{" + k + "}", v)
    return content


# ── 插件主体 ────────────────────────────────────────────────────────────


@neko_plugin
class NekoSocialPosterPlugin(NekoPluginBase):
    """社交动态发布插件。

    利用 N.E.K.O 内置的 Computer Use Agent（VLM + pyautogui）
    操作桌面浏览器，在微博网页版发布动态。用户无需申请微博开发者账号，
    只需在浏览器中登录好微博即可。
    """

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self._cua: Optional[Any] = None
        self._config: Dict[str, Any] = {}

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        self.logger.info("neko_social_poster starting… CUA available=%s", _CUA_AVAILABLE)
        try:
            cfg = await self.get_own_config(timeout=2.0)
            if isinstance(cfg, dict):
                self._config = cfg
        except Exception as e:
            self.logger.warning("failed to load plugin config: %s", e)

    # ── CUA 懒加载 ──────────────────────────────────────────────────────

    def _get_cua(self) -> Optional[Any]:
        """按需构造 ComputerUseAdapter。

        每次发动态都新建实例，因为 ComputerUseAdapter 内部有会话状态
        (_current_session_id, actions, observations)，不复用更安全。
        """
        if not _CUA_AVAILABLE or _ComputerUseAdapter is None:
            return None
        try:
            max_steps = int(self._config.get("max_steps", 30))
            return _ComputerUseAdapter(max_steps=max_steps)
        except Exception as e:
            self.logger.error("failed to construct ComputerUseAdapter: %s", e)
            return None

    # ── Plugin Entries ──────────────────────────────────────────────────

    @plugin_entry(
        id="generate_weibo_content",
        name=tr("entry.weibo_post.name", default="生成微博内容"),
        description=tr(
            "entry.weibo_post.description",
            default="让猫娘生成一条微博候选内容（不发布）。返回 content 字段供后续 post_weibo 使用。",
        ),
        input_schema={
            "type": "object",
            "properties": {},
        },
        llm_result_fields=["content"],
    )
    async def generate_weibo_content(self, **_) -> Any:
        """生成一条微博内容候选，不执行发布。

        用户确认内容后，LLM 应调用 post_weibo 并传入相同的 content。
        """
        content = _generate_content()
        return Ok({"content": content})

    @plugin_entry(
        id="post_weibo",
        name=tr("entry.weibo_post.name", default="发微博"),
        description=tr(
            "entry.weibo_post.description",
            default="让猫娘用 Computer Use Agent 在微博网页版发布一条动态。需要用户已在浏览器中登录微博账号。content 参数为要发布的文字内容。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": tr(
                        "entry.weibo_post.param.content",
                        default="要发布的微博文字内容（必填）",
                    ),
                },
            },
            "required": ["content"],
        },
        llm_result_fields=["success", "content", "steps", "message"],
    )
    async def post_weibo(self, content: str, **_) -> Any:
        """在微博网页版发布指定内容。

        流程：
        1. 校验 content 非空
        2. 推送"正在操作"提示到聊天
        3. 在独立线程中运行 CUA（run_instruction 是阻塞的）
        4. 返回执行结果

        CUA 执行期间会周期性截图 + VLM 推理，总耗时通常 15-60 秒。
        """
        content = (content or "").strip()
        if not content:
            return Err(SdkError(tr(
                "entry.weibo_post.error.not_configured",
                default="content 不能为空",
            )))

        # ── 1. 检查 CUA 可用性 ──────────────────────────────────────────
        cua = self._get_cua()
        if cua is None:
            return Err(SdkError(tr(
                "entry.weibo_post.error.cua_not_available",
                default="Computer Use Agent 不可用。请确认：\n1. N.E.K.O 主程序已启动\n2. Agent 模型已在设置中配置\n3. pyautogui 可用（macOS 需授权辅助功能）",
            )))

        if not getattr(cua, "init_ok", True) and getattr(cua, "last_error", None):
            return Err(SdkError(tr(
                "entry.weibo_post.error.cua_not_available",
                default=f"Computer Use Agent 初始化失败：{cua.last_error}",
            )))

        # ── 2. 推送"正在执行"提示 ───────────────────────────────────────
        try:
            self.push_message(
                source="neko_social_poster",
                visibility=["chat"],
                ai_behavior="blind",
                parts=[{
                    "type": "text",
                    "text": tr(
                        "entry.weibo_post.executing",
                        default="正在操作电脑发微博…别碰鼠标哦~",
                    ),
                }],
                priority=8,
            )
        except Exception:
            pass

        # ── 3. 在独立线程运行 CUA ────────────────────────────────────────
        # run_instruction 是同步阻塞的（截图 + LLM 调用循环），
        # 必须放到线程池，否则会卡死 asyncio 事件循环。
        instruction = WEIBO_POST_INSTRUCTION_TEMPLATE.format(content=content)

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, cua.run_instruction, instruction)
        except asyncio.CancelledError:
            return Err(SdkError(tr(
                "entry.weibo_post.error.cua_failed",
                default="任务被取消",
            )))
        except Exception as e:
            self.logger.error("CUA run_instruction raised: %s", e)
            return Err(SdkError(tr(
                "entry.weibo_post.error.cua_failed",
                default=f"Computer Use Agent 执行失败：{e}",
            )))

        # ── 4. 解析结果 ──────────────────────────────────────────────────
        success = bool(result.get("success"))
        steps = int(result.get("steps", 0))
        result_text = result.get("result", "")
        error = result.get("error", "")

        if success:
            self.push_message(
                source="neko_social_poster",
                visibility=["chat"],
                ai_behavior="respond",
                parts=[{
                    "type": "text",
                    "text": tr(
                        "entry.weibo_post.success",
                        default="发好啦！微博内容：「{content}」",
                        content=content,
                    ),
                }],
                priority=5,
                metadata={
                    "activity_type": "social_post",
                    "platform": "weibo_web",
                },
            )
            return Ok({
                "success": True,
                "content": content,
                "steps": steps,
                "message": result_text or "发布成功",
            })
        else:
            error_detail = error or result_text or "未知原因"
            self.push_message(
                source="neko_social_poster",
                visibility=["chat"],
                ai_behavior="respond",
                parts=[{
                    "type": "text",
                    "text": tr(
                        "entry.weibo_post.partial",
                        default="好像没发出去…{detail}",
                        detail=error_detail,
                    ),
                }],
                priority=7,
            )
            return Ok({
                "success": False,
                "content": content,
                "steps": steps,
                "message": error_detail,
            })
