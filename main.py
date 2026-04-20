"""
Lorebook - 世界书插件

基于正则关键词匹配的轻量世界书实现。支持多本世界书，每本包含独立的注入设置
和最多 20 个条目。每轮 LLM 请求前：
1. 清理上一轮注入到对话历史中的世界书标签
2. 扫描当前用户消息，对所有已启用世界书的已启用条目做正则匹配
3. 对命中条目做 RAG 去重：若条目内容已被 RAG 注入到 prompt 中则跳过
4. 将剩余条目按 priority 升序排列，拼装后注入到指定位置

每本世界书通过 AstrBot 管理面板配置（template_list），支持动态添加/编辑/删除。

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

MAX_ENTRIES = 20


@register(
    "Lorebook",
    "FelisAbyssalis",
    "基于正则关键词匹配的世界书插件 - 当用户消息命中条目关键词时自动注入对应内容",
    "1.2.0",
    "https://github.com/EmilyCheoh/astrbot_plugin_lorebook",
)
class LorebookPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self._lorebooks: list[dict[str, Any]] = []
        self._load_lorebooks()

        if self._lorebooks:
            names = [lb["name"] for lb in self._lorebooks]
            logger.info(f"Lorebook 插件初始化完成 (已加载世界书: {names})")
        else:
            logger.info("Lorebook 插件已加载但没有启用的世界书")

    # -------------------------------------------------------------------
    # 世界书加载
    # -------------------------------------------------------------------

    def _load_lorebooks(self) -> None:
        """从插件配置（template_list）中加载所有已启用的世界书及其条目。"""
        self._lorebooks = []

        raw_books = self.config.get("lorebooks", [])
        if not isinstance(raw_books, list):
            return

        for bi, book in enumerate(raw_books):
            if not isinstance(book, dict):
                continue

            enabled = bool(book.get("enabled", True))
            if not enabled:
                continue

            book_name = str(book.get("book_name", "")).strip()
            if not book_name:
                book_name = f"lorebook_{bi}"

            # 注入位置
            pos = str(book.get("injection_position", "user_message_after")).strip()
            position = pos if pos in VALID_POSITIONS else "user_message_after"

            # XML 标签名
            tag_name = str(book.get("tag_name", "Additional-Info")).strip()
            if not tag_name or not TAG_NAME_PATTERN.match(tag_name):
                logger.warning(
                    f"Lorebook [{book_name}]: "
                    f"标签名称为空或包含非法字符，回退到默认值 Additional-Info"
                )
                tag_name = "Additional-Info"

            header = f"<{tag_name}>"
            footer = f"</{tag_name}>"
            cleanup_re = re.compile(
                re.escape(header) + r".*?" + re.escape(footer),
                flags=re.DOTALL,
            )

            # 标签头部说明文本
            header_text = str(book.get("header_text", "")).strip()

            # 加载条目 (1 ~ MAX_ENTRIES)
            entries: list[dict[str, Any]] = []
            for i in range(1, MAX_ENTRIES + 1):
                prefix = f"entry_{i}_"

                entry_enabled = bool(book.get(f"{prefix}enabled", True))
                if not entry_enabled:
                    continue

                entry_name = str(book.get(f"{prefix}name", "")).strip()
                if not entry_name:
                    continue

                keywords_raw = book.get(f"{prefix}keywords", [])
                if not isinstance(keywords_raw, list) or not keywords_raw:
                    continue

                content = str(book.get(f"{prefix}content", ""))
                content = content.replace("\\n", "\n").strip()
                if not content:
                    continue

                priority = int(book.get(f"{prefix}priority", 5))

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
                            f"Lorebook [{book_name}/{entry_name}]: "
                            f"关键词 '{kw_str}' 正则编译失败: {e}"
                        )

                if not compiled:
                    continue

                entries.append({
                    "name": entry_name,
                    "keywords": compiled,
                    "content": content,
                    "priority": priority,
                })

                logger.debug(
                    f"Lorebook [{book_name}]: 已加载条目 [{entry_name}] "
                    f"(优先级: {priority}, 关键词数: {len(compiled)})"
                )

            if not entries:
                logger.info(f"Lorebook [{book_name}]: 没有有效条目，跳过")
                continue

            self._lorebooks.append({
                "name": book_name,
                "position": position,
                "tag_name": tag_name,
                "header": header,
                "footer": footer,
                "cleanup_re": cleanup_re,
                "header_text": header_text,
                "entries": entries,
            })

            logger.info(
                f"Lorebook [{book_name}]: 已加载 {len(entries)} 个条目 "
                f"(标签: {tag_name}, 位置: {position})"
            )

        logger.info(f"Lorebook: 共加载 {len(self._lorebooks)} 本世界书")

    # -------------------------------------------------------------------
    # 匹配
    # -------------------------------------------------------------------

    @staticmethod
    def _normalize(text: str) -> str:
        """将连续空白压缩为单个空格，用于去重比较。"""
        return re.sub(r"\s+", " ", text).strip()

    def _match_entries(
        self, text: str, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """扫描文本，返回所有关键词命中的条目，按 priority 升序排列。"""
        matched = []
        for entry in entries:
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

    def _format_injection(
        self, entries: list[dict[str, Any]], lorebook: dict[str, Any]
    ) -> str:
        """将匹配到的条目拼装为 XML 标签包裹的注入内容。"""
        if len(entries) == 1:
            body = entries[0]["content"]
        else:
            sections = []
            for i, entry in enumerate(entries, 1):
                sections.append(f"#{i}\n{entry['content']}")
            body = "\n\n".join(sections)

        header = lorebook["header"]
        footer = lorebook["footer"]
        header_text = lorebook["header_text"]

        if header_text:
            return f"{header}\n{header_text}\n\n{body}\n{footer}\n"
        return f"{header}\n{body}\n{footer}\n"

    # -------------------------------------------------------------------
    # 清理逻辑
    # -------------------------------------------------------------------

    def _clean_string(self, text: str, cleanup_re: re.Pattern) -> str:
        cleaned = cleanup_re.sub("", text)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _clean_contexts(self, req: ProviderRequest) -> int:
        """从 ProviderRequest 的所有位置中清除上一轮注入的世界书标签。"""
        removed = 0

        for lb in self._lorebooks:
            header = lb["header"]
            footer = lb["footer"]
            cleanup_re = lb["cleanup_re"]

            if hasattr(req, "system_prompt") and req.system_prompt:
                if (
                    isinstance(req.system_prompt, str)
                    and header in req.system_prompt
                    and footer in req.system_prompt
                ):
                    original = req.system_prompt
                    req.system_prompt = self._clean_string(original, cleanup_re)
                    if req.system_prompt != original:
                        removed += 1

            if hasattr(req, "prompt") and req.prompt:
                if (
                    isinstance(req.prompt, str)
                    and header in req.prompt
                    and footer in req.prompt
                ):
                    original = req.prompt
                    req.prompt = self._clean_string(original, cleanup_re)
                    if req.prompt != original:
                        removed += 1

            if hasattr(req, "contexts") and req.contexts:
                filtered = []
                for msg in req.contexts:
                    if isinstance(msg, str):
                        if header in msg and footer in msg:
                            cleaned = self._clean_string(msg, cleanup_re)
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
                            if header in content and footer in content:
                                cleaned = self._clean_string(content, cleanup_re)
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
                                            header in text_val
                                            and footer in text_val
                                        ):
                                            ct = self._clean_string(
                                                text_val, cleanup_re
                                            )
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

    def _inject_text(
        self, req: ProviderRequest, text: str, position: str
    ) -> None:
        """将文本注入到配置指定的位置。"""
        if position == "user_message_before":
            req.prompt = text + "\n\n" + (req.prompt or "")

        elif position == "system_prompt":
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
        if not self._lorebooks:
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

        扫描当前用户消息，匹配各世界书条目，将命中内容注入到指定位置。
        """
        if not self._lorebooks:
            return

        try:
            user_text = event.message_str or ""
            if not user_text:
                return

            session_id = event.unified_msg_origin or "unknown"

            for lb in self._lorebooks:
                matched = self._match_entries(user_text, lb["entries"])
                if not matched:
                    continue

                # 去重：跳过已被 RAG 注入的条目
                matched, skipped = self._dedup_against_rag(
                    matched, req.prompt or ""
                )

                if skipped:
                    logger.info(
                        f"[{session_id}] Lorebook [{lb['name']}] [去重]: "
                        f"跳过 {len(skipped)} 个已被 RAG 覆盖的条目: "
                        f"{[e['name'] for e in skipped]}"
                    )

                if not matched:
                    continue

                injection = self._format_injection(matched, lb)
                self._inject_text(req, injection, lb["position"])

                names = [e["name"] for e in matched]
                logger.info(
                    f"[{session_id}] Lorebook [{lb['name']}] [注入]: "
                    f"匹配到 {len(matched)} 个条目: {names}"
                )

        except Exception as e:
            logger.error(f"Lorebook [注入]: {e}", exc_info=True)

    # -------------------------------------------------------------------
    # 生命周期
    # -------------------------------------------------------------------

    async def terminate(self):
        self._lorebooks = []
        logger.info("Lorebook 插件已停止")
