"""
Lorebook - 世界书插件

基于正则关键词匹配的轻量世界书实现。每轮 LLM 请求前：
1. 清理上一轮注入到对话历史中的世界书标签
2. 扫描当前用户消息，对所有已启用条目做正则匹配
3. 对命中条目做 RAG 去重：若条目内容已被 RAG 注入到 prompt 中则跳过
4. 将剩余条目按 priority 升序排列，拼装后注入到指定位置

条目通过 AstrBot 管理面板配置（template_list），支持动态添加/编辑/删除。

与 FirstWindowInject / PromptTags / LivingMemory 兼容：
- 清理阶段 priority=3，在 FirstWindowInject(2) 之前执行
- 注入阶段 priority=-498，在 FirstWindowInject(-499) 和 PromptTags(-500) 之后执行
- 使用独立的标签名称（默认 Additional-Info），不会与其他插件的正则交叉匹配

F(A) = A(F)
"""

import re
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

TAG_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

VALID_POSITIONS = ("user_message_before", "user_message_after", "system_prompt")


@register(
    "Lorebook",
    "FelisAbyssalis",
    "基于正则关键词匹配的世界书插件 - 当用户消息命中条目关键词时自动注入对应内容",
    "1.0.0",
    "https://github.com/EmilyCheoh/astrbot_plugin_lorebook",
)
class LorebookPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 全局开关
        self._enabled = bool(config.get("enabled", True))

        # 注入位置
        pos = str(config.get("injection_position", "user_message_after")).strip()
        self._position = pos if pos in VALID_POSITIONS else "user_message_after"

        # XML 标签名
        tag_name = str(config.get("tag_name", "Additional-Info")).strip()
        if not tag_name or not TAG_NAME_PATTERN.match(tag_name):
            logger.warning(
                "Lorebook: 标签名称为空或包含非法字符，回退到默认值 Additional-Info"
            )
            tag_name = "Additional-Info"
        self._tag_name = tag_name
        self._header = f"<{tag_name}>"
        self._footer = f"</{tag_name}>"
        self._cleanup_re = re.compile(
            re.escape(self._header) + r".*?" + re.escape(self._footer),
            flags=re.DOTALL,
        )

        # 从配置加载条目
        self._entries: list[dict[str, Any]] = []
        self._load_entries()

        if self._enabled:
            logger.info(
                f"Lorebook 插件初始化完成 "
                f"(条目: {len(self._entries)}, "
                f"标签: {self._tag_name}, "
                f"位置: {self._position})"
            )
        else:
            logger.info("Lorebook 插件已加载但全局开关关闭")

    # -------------------------------------------------------------------
    # 条目加载
    # -------------------------------------------------------------------

    def _load_entries(self) -> None:
        """从插件配置（template_list）中加载所有已启用且合法的条目。"""
        self._entries = []

        raw_entries = self.config.get("entries", [])
        if not isinstance(raw_entries, list):
            return

        for i, entry in enumerate(raw_entries):
            if not isinstance(entry, dict):
                continue

            enabled = bool(entry.get("enabled", True))
            if not enabled:
                continue

            name = str(entry.get("name", "")).strip()
            if not name:
                name = f"unnamed_{i}"

            keywords_raw = entry.get("keywords", [])
            if not isinstance(keywords_raw, list) or not keywords_raw:
                continue

            content = str(entry.get("content", ""))
            content = content.replace("\\n", "\n").strip()
            if not content:
                continue

            priority = int(entry.get("priority", 5))

            # 预编译正则
            compiled = []
            for kw in keywords_raw:
                kw_str = str(kw).strip()
                if not kw_str:
                    continue
                try:
                    compiled.append(re.compile(kw_str))
                except re.error as e:
                    logger.warning(
                        f"Lorebook: 条目 [{name}] 关键词 '{kw_str}' "
                        f"正则编译失败: {e}"
                    )

            if not compiled:
                continue

            self._entries.append({
                "name": name,
                "keywords": compiled,
                "content": content,
                "priority": priority,
            })

            logger.debug(
                f"Lorebook: 已加载条目 [{name}] "
                f"(优先级: {priority}, 关键词数: {len(compiled)})"
            )

        logger.info(f"Lorebook: 共加载 {len(self._entries)} 个有效条目")

    # -------------------------------------------------------------------
    # 匹配
    # -------------------------------------------------------------------

    @staticmethod
    def _normalize(text: str) -> str:
        """将连续空白压缩为单个空格，用于去重比较。"""
        return re.sub(r"\s+", " ", text).strip()

    def _match_entries(self, text: str) -> list[dict[str, Any]]:
        """扫描文本，返回所有关键词命中的条目，按 priority 升序排列。"""
        matched = []
        for entry in self._entries:
            for pattern in entry["keywords"]:
                if pattern.search(text):
                    matched.append(entry)
                    break

        matched.sort(key=lambda e: e["priority"])

        return matched

    def _dedup_against_rag(
        self,
        matched: list[dict[str, Any]],
        prompt: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """过滤掉已被 RAG 注入到 prompt 中的条目。

        Returns:
            (kept, skipped) — 保留的条目列表和被跳过的条目列表。
        """
        if not prompt:
            return matched, []

        normalized_prompt = self._normalize(prompt)

        kept: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for entry in matched:
            if self._normalize(entry["content"]) in normalized_prompt:
                skipped.append(entry)
            else:
                kept.append(entry)

        return kept, skipped

    # -------------------------------------------------------------------
    # 格式化
    # -------------------------------------------------------------------

    def _format_injection(self, entries: list[dict[str, Any]]) -> str:
        """将匹配到的条目拼装为 XML 标签包裹的注入内容。"""
        sections = []
        for i, entry in enumerate(entries, 1):
            sections.append(f"#{i}\n{entry['content']}")

        body = "\n\n".join(sections)
        return f"{self._header}\n{body}\n{self._footer}\n"

    # -------------------------------------------------------------------
    # 清理逻辑
    # -------------------------------------------------------------------

    def _clean_string(self, text: str) -> str:
        cleaned = self._cleanup_re.sub("", text)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _clean_contexts(self, req: ProviderRequest) -> int:
        """从 ProviderRequest 的所有位置中清除上一轮注入的标签。"""
        removed = 0

        if hasattr(req, "system_prompt") and req.system_prompt:
            if (
                isinstance(req.system_prompt, str)
                and self._header in req.system_prompt
                and self._footer in req.system_prompt
            ):
                original = req.system_prompt
                req.system_prompt = self._clean_string(original)
                if req.system_prompt != original:
                    removed += 1

        if hasattr(req, "prompt") and req.prompt:
            if (
                isinstance(req.prompt, str)
                and self._header in req.prompt
                and self._footer in req.prompt
            ):
                original = req.prompt
                req.prompt = self._clean_string(original)
                if req.prompt != original:
                    removed += 1

        if hasattr(req, "contexts") and req.contexts:
            filtered = []
            for msg in req.contexts:
                if isinstance(msg, str):
                    if self._header in msg and self._footer in msg:
                        cleaned = self._clean_string(msg)
                        if not cleaned:
                            removed += 1
                            continue
                        if cleaned != msg:
                            removed += 1
                            filtered.append(cleaned)
                            continue
                    filtered.append(msg)

                elif isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        if self._header in content and self._footer in content:
                            cleaned = self._clean_string(content)
                            if not cleaned:
                                removed += 1
                                continue
                            if cleaned != content:
                                removed += 1
                                msg_copy = msg.copy()
                                msg_copy["content"] = cleaned
                                filtered.append(msg_copy)
                                continue
                        filtered.append(msg)

                    elif isinstance(content, list):
                        cleaned_parts = []
                        has_changes = False
                        for part in content:
                            if (
                                isinstance(part, dict)
                                and part.get("type") == "text"
                            ):
                                text_val = part.get("text", "")
                                if isinstance(text_val, str):
                                    if (
                                        self._header in text_val
                                        and self._footer in text_val
                                    ):
                                        ct = self._clean_string(text_val)
                                        if not ct:
                                            has_changes = True
                                            continue
                                        if ct != text_val:
                                            has_changes = True
                                            removed += 1
                                            part_copy = part.copy()
                                            part_copy["text"] = ct
                                            cleaned_parts.append(part_copy)
                                            continue
                            cleaned_parts.append(part)

                        if not cleaned_parts:
                            removed += 1
                            continue
                        if has_changes:
                            msg_copy = msg.copy()
                            msg_copy["content"] = cleaned_parts
                            filtered.append(msg_copy)
                            continue
                        filtered.append(msg)
                else:
                    filtered.append(msg)

            req.contexts = filtered

        return removed

    # -------------------------------------------------------------------
    # 注入辅助
    # -------------------------------------------------------------------

    def _inject_text(self, req: ProviderRequest, text: str) -> None:
        """将文本注入到配置指定的位置。"""
        if self._position == "user_message_before":
            req.prompt = text + "\n\n" + (req.prompt or "")

        elif self._position == "system_prompt":
            req.system_prompt = (req.system_prompt or "") + "\n\n" + text

        else:  # user_message_after
            prompt = req.prompt or ""
            rag_marker = "<RAG-Faiss-Memory>"
            rag_pos = prompt.find(rag_marker)
            if rag_pos > 0:
                before_rag = prompt[:rag_pos].rstrip()
                from_rag = prompt[rag_pos:]
                req.prompt = before_rag + "\n\n" + text + "\n\n" + from_rag
            else:
                req.prompt = prompt + "\n\n" + text

    # -------------------------------------------------------------------
    # 钩子
    # -------------------------------------------------------------------

    @filter.on_llm_request(priority=3)
    async def handle_cleanup(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """
        [清理阶段] priority=3，在所有其他插件之前执行。

        从 req.prompt / req.system_prompt / req.contexts 中清除
        上一轮注入的世界书标签。
        """
        if not self._enabled:
            return

        try:
            removed = self._clean_contexts(req)
            if removed > 0:
                session_id = event.unified_msg_origin or "unknown"
                logger.info(
                    f"[{session_id}] Lorebook [清理]: "
                    f"已清理 {removed} 处历史注入"
                )
        except Exception as e:
            logger.error(f"Lorebook [清理]: {e}", exc_info=True)

    @filter.on_llm_request(priority=-498)
    async def handle_inject(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """
        [注入阶段] priority=-498，在所有其他插件之后执行。

        扫描当前用户消息，匹配世界书条目，将命中内容注入到指定位置。
        """
        if not self._enabled:
            return

        try:
            if not self._entries:
                return

            user_text = event.message_str or ""
            if not user_text:
                return

            matched = self._match_entries(user_text)
            if not matched:
                return

            # 去重：跳过已被 RAG 注入的条目
            matched, skipped = self._dedup_against_rag(matched, req.prompt or "")

            session_id = event.unified_msg_origin or "unknown"

            if skipped:
                logger.info(
                    f"[{session_id}] Lorebook [去重]: "
                    f"跳过 {len(skipped)} 个已被 RAG 覆盖的条目: "
                    f"{[e['name'] for e in skipped]}"
                )

            if not matched:
                return

            injection = self._format_injection(matched)
            self._inject_text(req, injection)

            names = [e["name"] for e in matched]
            logger.info(
                f"[{session_id}] Lorebook [注入]: "
                f"匹配到 {len(matched)} 个条目: {names}"
            )

        except Exception as e:
            logger.error(f"Lorebook [注入]: {e}", exc_info=True)

    # -------------------------------------------------------------------
    # 生命周期
    # -------------------------------------------------------------------

    async def terminate(self):
        self._enabled = False
        self._entries = []
        logger.info("Lorebook 插件已停止")
