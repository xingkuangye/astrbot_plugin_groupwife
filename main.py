from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api import logger
from botpy.http import Route
from datetime import date
import hashlib
import random

@register("astrbot_plugin_groupwife", "星星旁の旷野", "每日固定双向配对的群友老婆抽取插件，成员变化时仅重配落单。", "1.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        config = config or {}
        self.beta_config = bool(config.get("beta_config", False))
        self.inactive_days = max(0, int(config.get("inactive_days", 3)))

        self.text = [str(t) for t in config.get("text", []) or []]
        self.enable_keyboard = bool(config.get("enable_keyboard", True))
        self.retry_button_label = str(config.get("retry_button_label", "✨群友老婆"))
        self.retry_command = str(config.get("retry_command", "/wife"))
        self.menu_button_label = str(config.get("menu_button_label", "📋菜单"))
        self.menu_command = str(config.get("menu_command", "/菜单"))

        self.beta_feedback_template = str(
            config.get(
                "beta_feedback_template",
                "/反馈 wife.{id} [在这里填写你想要反馈的内容]",
            )
        )
        self.beta_report_template = str(
            config.get(
                "beta_report_template",
                "/举报 wife.{id} [在这里填写你想要举报的原因]",
            )
        )

    def _normalize_group_members(self, raw_members) -> set:
        """将持久化成员结构转换为 set[str] 以便计算。"""
        if isinstance(raw_members, set):
            return {str(m) for m in raw_members}
        if isinstance(raw_members, (list, tuple)):
            return {str(m) for m in raw_members}
        if isinstance(raw_members, dict):
            return {str(k) for k in raw_members.keys()}
        return set()

    async def _get_group_members(self, group_id: str) -> set:
        """读取群成员（兼容旧格式），内部统一为 set[str]。"""
        raw_members = await self.get_kv_data(f"group_members_{group_id}", [])
        return self._normalize_group_members(raw_members)

    async def _save_group_members(self, group_id: str, group_members: set):
        """保存群成员，使用 list 以支持 JSON 序列化。"""
        await self.put_kv_data(f"group_members_{group_id}", sorted(group_members, key=str))

    async def _append_beta_notice(self, event: AstrMessageEvent, message: str) -> str:
        """按配置附加测试版提示文案。

        @param event AstrMessageEvent 消息事件对象
        @param message str 原始消息
        @return str 追加测试提示后的消息
        """
        if not self.beta_config:
            return message
        
        msg_id = await self.get_kv_data("groupwife_now_msg_id", 0) + 1
        await self.put_kv_data("groupwife_now_msg_id", msg_id)

        feedback_cmd = self.beta_feedback_template.replace("{id}", str(msg_id))
        report_cmd = self.beta_report_template.replace("{id}", str(msg_id))

        try:
            openid = event.message_obj.raw_message.group_openid
        except AttributeError:
            openid = event.get_sender_id()

        message += f"""
***
> 您当前正在使用测试版本的AL_1S机器人
> 如果您遇到了问题，请点击<qqbot-cmd-input text=\"{feedback_cmd}\" show=\"反馈\" reference=\"true\" />
> 如果您看到了不良信息，请点击<qqbot-cmd-input text=\"{report_cmd}\" show=\"举报\" reference=\"true\" />
> 感谢您的支持~
> _测试ID：{openid}_"""

        # 记录测试 ID，便于用户反馈或举报时回溯原始消息。
        
        await self.put_kv_data(f"groupwife_{msg_id}_originmessage", message)

        return message

    @filter.command("get_origin_message")
    async def get_origin_message(self, event: AstrMessageEvent, message_id: int):
        """获取原始消息内容，供测试反馈使用。"""
        if not self.beta_config:
            return

        message = await self.get_kv_data(f"groupwife_{message_id}_originmessage", None)
        if message is None:
            await self._send_markdown_message(event, f"未找到原始消息内容（ID: {message_id}）。")
            return

        chain = event.plain_result(f"原始消息内容（ID: {message_id}）：\n\n{message}")
        # 再手动关掉 markdown
        chain.use_markdown_ = False
        yield chain

    async def _send_markdown_message(self, event: AstrMessageEvent, message: str):
        """使用与 yunshi 一致的官方 API 发送 Markdown 消息。"""
        is_private = event.is_private_chat()
        payload = {
            "msg_type": 2,
            "msg_id": event.message_obj.message_id,
            "markdown": {
                "content": message,
            },
        }

        if self.enable_keyboard:
            buttons = [
                {
                    "render_data": {"label": self.menu_button_label, "style": 1},
                    "action": {
                        "type": 2,
                        "permission": {"type": 2},
                        "data": self.menu_command,
                    },
                }
            ]
            if not is_private:
                buttons.insert(
                    0,
                    {
                        "render_data": {"label": self.retry_button_label, "style": 1},
                        "action": {
                            "type": 2,
                            "permission": {"type": 2},
                            "data": self.retry_command,
                        },
                    },
                )

            payload["keyboard"] = {
                "content": {
                    "rows": [
                        {
                            "buttons": buttons
                        }
                    ]
                }
            }

        if is_private:
            user_openid = event.message_obj.raw_message.author.user_openid
            route = Route("POST", f"/v2/users/{user_openid}/messages")
            await event.bot.api._http.request(route, json={**payload})
            return

        group_openid = event.message_obj.raw_message.group_openid
        await event.bot.api.post_group_message(group_openid=group_openid, **payload)

    def _prune_inactive_members(
        self,
        group_members: set,
        last_seen_map: dict,
        today: date,
        inactive_days: int = 3,
    ) -> tuple:
        """清理超过指定天数未发言成员。

        @param group_members set 当前群成员集合
        @param last_seen_map dict 成员最后发言日期映射（uid -> yyyy-mm-dd）
        @param today date 当前日期
        @param inactive_days int 超过该天数则踢出（默认 3）
        @return tuple (filtered_members, filtered_last_seen)
        """
        filtered_members = set()
        filtered_last_seen = {}

        for member in group_members:
            last_seen_str = last_seen_map.get(member)
            if not isinstance(last_seen_str, str):
                continue

            try:
                last_seen_date = date.fromisoformat(last_seen_str)
            except ValueError:
                continue

            if (today - last_seen_date).days <= inactive_days:
                filtered_members.add(member)
                filtered_last_seen[member] = last_seen_str

        return filtered_members, filtered_last_seen

    def _members_signature(self, group_members: set) -> str:
        """生成成员签名，用于检测成员集合是否变化。

        @param group_members set 当前群成员集合
        @return str 稳定签名（同成员集合必定相同）
        """
        # 对成员做稳定排序后再哈希，避免 set 无序导致同一批成员得到不同签名。
        members = sorted(group_members, key=str)
        return hashlib.sha256(",".join(map(str, members)).encode("utf-8")).hexdigest()

    def _build_daily_pairs(self, group_members: set, group_id: str, date_key: str) -> dict:
        """全量构建当天配对表（固定且双向一致）。

        @param group_members set 当前群成员集合
        @param group_id str 群号
        @param date_key str 日期键（yyyy-mm-dd）
        @return dict 双向配对结果，落单时为自映射
        """
        # 构造稳定随机种子：同一天、同群、同成员集合下，结果固定不变。
        members = sorted(group_members, key=str)
        seed_source = f"{group_id}:{date_key}:{','.join(map(str, members))}"
        seed = int(hashlib.sha256(seed_source.encode("utf-8")).hexdigest(), 16)

        rng = random.Random(seed)
        shuffled = members[:]
        rng.shuffle(shuffled)

        pairs = {}
        for i in range(0, len(shuffled) - 1, 2):
            a = shuffled[i]
            b = shuffled[i + 1]
            pairs[a] = b
            pairs[b] = a

        # 奇数人数时会有 1 人落单，保持自映射避免非对称结果。
        if len(shuffled) % 2 == 1:
            pairs[shuffled[-1]] = shuffled[-1]

        return pairs

    def _rebuild_with_existing_pairs(
        self,
        group_members: set,
        cached_pairs: dict,
        group_id: str,
        date_key: str,
    ) -> dict:
        """成员变化时增量重建：保留有效旧配对，仅重配落单成员。

        @param group_members set 当前群成员集合
        @param cached_pairs dict 缓存中的历史配对
        @param group_id str 群号
        @param date_key str 日期键（yyyy-mm-dd）
        @return dict 更新后的双向配对结果
        """
        # members: 当前仍在群内的成员列表（稳定排序）。
        members = sorted(group_members, key=str)
        pairs = {}
        used = set()

        # 保留仍然有效的旧配对（双方都在群里且互相指向）。
        for member in members:
            if member in used:
                continue
            partner = cached_pairs.get(member)
            if (
                partner in group_members
                and partner != member
                and cached_pairs.get(partner) == member
                and partner not in used
            ):
                pairs[member] = partner
                pairs[partner] = member
                used.add(member)
                used.add(partner)

        # unmatched: 新增成员或原配已失效的成员，只对这部分人重新配对。
        unmatched = [m for m in members if m not in used]
        seed_source = f"{group_id}:{date_key}:unmatched:{','.join(map(str, unmatched))}"
        seed = int(hashlib.sha256(seed_source.encode("utf-8")).hexdigest(), 16)
        rng = random.Random(seed)
        rng.shuffle(unmatched)

        for i in range(0, len(unmatched) - 1, 2):
            a = unmatched[i]
            b = unmatched[i + 1]
            pairs[a] = b
            pairs[b] = a

        if len(unmatched) % 2 == 1:
            pairs[unmatched[-1]] = unmatched[-1]

        return pairs

    async def _get_or_build_daily_pairs(self, group_id: str, group_members: set, date_key: str) -> dict:
        """读取或构建当天配对缓存。

        @param group_id str 群号
        @param group_members set 当前群成员集合
        @param date_key str 日期键（yyyy-mm-dd）
        @return dict 当天配对结果
        """
        # 缓存结构:
        # {
        #   "members_signature": "...",
        #   "pairs": {"uid_a": "uid_b", "uid_b": "uid_a", ...}
        # }
        cache_key = f"group_daily_pairs_{group_id}_{date_key}"
        current_signature = self._members_signature(group_members)
        cache = await self.get_kv_data(cache_key, None)

        if isinstance(cache, dict):
            cached_signature = cache.get("members_signature")
            cached_pairs = cache.get("pairs")
            # 成员签名一致：直接复用缓存，不触发重算。
            if cached_signature == current_signature and isinstance(cached_pairs, dict):
                return cached_pairs

            if isinstance(cached_pairs, dict):
                # 成员有变化：保留仍有效的旧配对，只重配落单成员。
                pairs = self._rebuild_with_existing_pairs(
                    group_members,
                    cached_pairs,
                    str(group_id),
                    date_key,
                )
                await self.put_kv_data(
                    cache_key,
                    {
                        "members_signature": current_signature,
                        "pairs": pairs,
                    },
                )
                return pairs

            # 没有缓存或缓存格式异常：执行首次全量配对。
        pairs = self._build_daily_pairs(group_members, str(group_id), date_key)
        await self.put_kv_data(
            cache_key,
            {
                "members_signature": current_signature,
                "pairs": pairs,
            },
        )
        return pairs

    # 监听所有消息并记录发言成员
    # @param event AstrMessageEvent 消息事件对象
    # @return None
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_message(self, event: AstrMessageEvent):
        """记录群成员"""
        if event.is_private_chat():
            return
        
        # 获取消息上下文 -> member_id, group_id
        member_id = str(event.get_sender_id())
        group_id = str(event.get_group_id())
        today_key = date.today().isoformat()

        # 记录群成员集合 -> group_members
        group_members = await self._get_group_members(group_id)
        group_members.add(member_id)
        await self._save_group_members(group_id, group_members)

        # 记录最后发言日期 -> last_seen_map
        last_seen_key = f"group_members_last_seen_{group_id}"
        last_seen_map = await self.get_kv_data(last_seen_key, {})
        if not isinstance(last_seen_map, dict):
            last_seen_map = {}
        last_seen_map[member_id] = today_key
        await self.put_kv_data(last_seen_key, last_seen_map)

    # 处理群友老婆指令
    # @param event AstrMessageEvent 消息事件对象
    # @return MessageEventResult 抽取结果消息
    @filter.command("wife", alias="群友老婆")
    async def wife(self, event: AstrMessageEvent):
        """群友老婆指令：当天固定、双向一致，成员变更时仅重配落单。"""
        # 1) 仅支持群聊，私聊直接提示并返回。
        if event.is_private_chat():
            message = "该指令仅支持群聊使用。"
            message = await self._append_beta_notice(event, message)
            await self._send_markdown_message(event, message)
            return
        
        # 2) 获取上下文信息 -> group_id, sender_id
        group_id = str(event.get_group_id())
        sender_id = str(event.get_sender_id())

        # 3) 读取并更新群成员集合 -> group_members
        group_members = await self._get_group_members(group_id)
        group_members.add(sender_id)

        # 4) 读取并更新最后发言记录 -> last_seen_map
        date_key = date.today().isoformat()
        last_seen_key = f"group_members_last_seen_{group_id}"
        last_seen_map = await self.get_kv_data(last_seen_key, {})
        if not isinstance(last_seen_map, dict):
            last_seen_map = {}
        last_seen_map[sender_id] = date_key

        # 5) 清理超过 3 天未发言成员，只保留活跃候选。
        pruned_members, pruned_last_seen = self._prune_inactive_members(
            group_members,
            last_seen_map,
            date.today(),
            inactive_days=self.inactive_days,
        )
        group_members = pruned_members
        await self._save_group_members(group_id, group_members)
        await self.put_kv_data(last_seen_key, pruned_last_seen)

        # 6) 兜底：如果成员为空则无法抽取。
        if not group_members:
            message = f"""## 抽取失败
此错误只应在调试环境下出现
如果您在正式环境中遇到此错误，请点击下方反馈按钮进行反馈"""
            message = await self._append_beta_notice(event, message)
            await self._send_markdown_message(event, message)
            return

        # 7) 读取或构建当天配对 -> daily_pairs
        #    - 成员未变化 -> 直接复用当天缓存
        #    - 成员变化   -> 保留旧配对，仅重配落单
        daily_pairs = await self._get_or_build_daily_pairs(str(group_id), group_members, date_key)

        # 8) 读取当前用户的匹配结果 -> wife_id
        wife_id = daily_pairs.get(sender_id, sender_id)

        # 9) 自己匹配自己代表今日落单（通常因为人数为奇数）。
        if wife_id == sender_id:
            message = f"""## 抽取失败
爱丽丝找不到更多的老师啦
要多多与爱丽丝互动哦~"""
            message = await self._append_beta_notice(event, message)
            await self._send_markdown_message(event, message)
            return

        platform = self.context.get_platform_inst(event.get_platform_id())
        if hasattr(platform, 'appid'):
            appid = platform.appid
        else:
            # 或者从配置里拿
            appid = platform.config.get("appid", "")

        avatar_url = f"https://q.qlogo.cn/qqapp/{appid}/{wife_id}/640"
            
        # 10) 输出抽取结果。
        message = ""
        if self.text:
            for t in self.text:
                message += f"**{t}**\n"
        message += f"""## 老婆来咯~
![img #60px #60px]({avatar_url})
<@{wife_id}>
是您今日的老婆哦！
```说明
<内容仅供娱乐|请勿当真>
```
"""
        message = await self._append_beta_notice(event, message)
        await self._send_markdown_message(event, message)
