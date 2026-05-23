"""
Lorebook - 世界书插件

基于正则关键词匹配的轻量世界书实现。：
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

import json
import re
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

TAG_NAME_INVALID = re.compile(r"[<>\n\r]")

VALID_POSITIONS = ("user_message_before", "user_message_after", "system_prompt")


@register(
    "Lorebook",
    "FelisAbyssalis",
    "基于正则关键词匹配的世界书插件 - 当用户消息命中条目关键词时自动注入对应内容",
    "2.3.0",
    "https://github.com/EmilyCheoh/astrbot_plugin_lorebook",
)
class LorebookPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self._lorebooks: list[dict[str, Any]] = []
        # cooldown state: turn-based
        self._session_turns: dict[str, int] = {}
        self._cooldown_state: dict[str, dict[str, int]] = {}
        # stay state: tracks which stay entries have already been injected
        # {session_id: {book_name:entry_name, ...}}
        self._stayed: dict[str, set[str]] = {}
        self._load_lorebooks()

        if self._lorebooks:
            names = [lb["name"] for lb in self._lorebooks]
            logger.info(f"世界书插件初始化完成 (已加载世界书: {names})")
        else:
            logger.info("世界书插件已加载但没有启用的世界书")

    # -------------------------------------------------------------------
    # 世界书加载
    # -------------------------------------------------------------------

    @staticmethod
    def _parse_enabled(val: Any) -> bool:
        """解析 enabled 字段：支持 bool / "T"/"F" / "true"/"false"。"""
        if isinstance(val, bool):
            return val
        s = str(val).strip().upper()
        return s in ("T", "TRUE", "1")

    @staticmethod
    def _parse_regex(val: Any) -> list[str]:
        """解析 regex 字段：支持字符串（整体作为单个 pattern）或字符串数组（每项为独立 pattern）。"""
        if isinstance(val, list):
            return [str(v).strip() for v in val if str(v).strip()]
        if isinstance(val, str) and val.strip():
            return [val.strip()]
        return []

    def _load_lorebooks(self) -> None:
        """从插件配置的 JSON 文本字段中加载所有已启用的世界书及其条目。"""
        self._lorebooks = []

        raw_json = self.config.get("lorebooks_json", "[]")
        if not isinstance(raw_json, str) or not raw_json.strip():
            return

        try:
            books = json.loads(raw_json)
        except json.JSONDecodeError as e:
            logger.error(f"世界书插件: JSON 解析失败: {e}")
            return

        if not isinstance(books, list):
            logger.error("世界书插件: JSON 顶层必须是数组")
            return

        for bi, book in enumerate(books):
            if not isinstance(book, dict):
                continue

            if not self._parse_enabled(book.get("enabled", True)):
                continue

            book_name = str(book.get("book_name", "")).strip()
            if not book_name:
                book_name = f"lorebook_{bi}"

            # 注入位置
            pos = str(book.get("injection_position", "user_message_after")).strip()
            position = pos if pos in VALID_POSITIONS else "user_message_after"

            # XML 标签名
            tag_name = str(book.get("tag_name", "Additional-Info")).strip()
            if not tag_name or TAG_NAME_INVALID.search(tag_name):
                logger.warning(
                    f"世界书插件 [{book_name}]: "
                    f"标签名称为空或包含非法字符 (<, >, 换行)，回退到默认值 Additional-Info"
                )
                tag_name = "Additional-Info"

            header = f"<{tag_name}>"
            footer = f"</{tag_name}>"

            # 💜 variant: used for regular (non-stay) entries
            p_tag_name = f"{tag_name}."
            p_header = f"<{p_tag_name}>"
            p_footer = f"</{p_tag_name}>"
            p_cleanup_re = re.compile(
                re.escape(p_header) + r".*?" + re.escape(p_footer),
                flags=re.DOTALL,
            )

            # 标签头部说明文本
            header_text = str(book.get("header_text", "")).strip()

            # 加载条目——支持对象 {"entry_1": {...}} 或数组 [{...}]
            entries_raw = book.get("entries", {})
            if isinstance(entries_raw, dict):
                entry_list = list(entries_raw.values())
            elif isinstance(entries_raw, list):
                entry_list = entries_raw
            else:
                entry_list = []

            entries: list[dict[str, Any]] = []
            for entry_obj in entry_list:
                if not isinstance(entry_obj, dict):
                    continue

                if not self._parse_enabled(entry_obj.get("enabled", True)):
                    continue

                entry_name = str(entry_obj.get("name", "")).strip()
                if not entry_name:
                    continue

                regex_raw = self._parse_regex(entry_obj.get("regex", []))
                if not regex_raw:
                    continue

                content = str(entry_obj.get("content", ""))
                content = content.replace("\\n", "\n").strip()
                if not content:
                    continue

                try:
                    priority = int(entry_obj.get("priority", 5))
                except (ValueError, TypeError):
                    priority = 5

                # 预编译正则
                compiled = []
                for pattern_str in regex_raw:
                    try:
                        compiled.append(re.compile(pattern_str))
                    except re.error as e:
                        logger.warning(
                            f"世界书插件 [{book_name}/{entry_name}]: "
                            f"正则 '{pattern_str}' 编译失败: {e}"
                        )

                if not compiled:
                    continue

                # 常驻标记
                constant = self._parse_enabled(
                    entry_obj.get("constant", False)
                )

                # 留驻标记
                stay = self._parse_enabled(
                    entry_obj.get("stay", False)
                )

                # 链接条目名
                links_raw = entry_obj.get("links", [])
                if isinstance(links_raw, str):
                    links = [s.strip() for s in links_raw.split(",") if s.strip()]
                elif isinstance(links_raw, list):
                    links = [str(s).strip() for s in links_raw if str(s).strip()]
                else:
                    links = []

                # 冷却轮数
                try:
                    cooldown = int(entry_obj.get("cooldown", 0))
                except (ValueError, TypeError):
                    cooldown = 0
                if cooldown < 0:
                    cooldown = 0

                entries.append({
                    "name": entry_name,
                    "keywords": compiled,
                    "content": content,
                    "priority": priority,
                    "constant": constant,
                    "stay": stay,
                    "links": links,
                    "cooldown": 0 if stay else cooldown,
                })

                logger.debug(
                    f"世界书插件 [{book_name}]: 已加载条目 [{entry_name}] "
                    f"(优先级: {priority}, 正则数: {len(compiled)})"
                )

            if not entries:
                logger.info(f"世界书插件 [{book_name}]: 没有有效条目，跳过")
                continue

            self._lorebooks.append({
                "name": book_name,
                "position": position,
                "tag_name": tag_name,
                "header": header,
                "footer": footer,
                "header_text": header_text,
                "entries": entries,
                # 💜 variant for cleanup
                "p_tag_name": p_tag_name,
                "p_header": p_header,
                "p_footer": p_footer,
                "p_cleanup_re": p_cleanup_re,
            })

            logger.info(
                f"世界书插件 [{book_name}]: 已加载 {len(entries)} 个条目 "
                f"(标签: {tag_name}, 位置: {position})"
            )

        logger.info(f"世界书插件: 共加载 {len(self._lorebooks)} 本世界书")

    # -------------------------------------------------------------------
    # 冷却
    # -------------------------------------------------------------------

    def _apply_cooldown(
        self,
        matched: list[dict[str, Any]],
        book_name: str,
        session_id: str,
        current_turn: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """过滤掉处于冷却期的条目，并为本次注入的条目记录注入轮次。

        冷却基于对话轮次：注入后的 N 轮内（无论用户消息是否命中该条目）
        均视为冷却期。轮次在 handle_inject 入口处递增。

        Returns:
            (kept, cooled_down)
        """
        state = self._cooldown_state.setdefault(session_id, {})

        kept: list[dict[str, Any]] = []
        cooled_down: list[dict[str, Any]] = []

        for entry in matched:
            cd = entry.get("cooldown", 0)
            if cd <= 0:
                kept.append(entry)
                continue

            key = f"{book_name}:{entry['name']}"
            last_injected = state.get(key, 0)
            if last_injected > 0 and (current_turn - last_injected) <= cd:
                cooled_down.append(entry)
            else:
                kept.append(entry)
                state[key] = current_turn

        return kept, cooled_down

    # -------------------------------------------------------------------
    # 匹配
    # -------------------------------------------------------------------

    @staticmethod
    def _normalize(text: str) -> str:
        """将连续空白压缩为单个空格，用于去重比较。"""
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _regex_match_entries(
        text: str, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """扫描文本，返回所有关键词命中的条目（纯 regex，不含 constant/links）。"""
        matched: list[dict[str, Any]] = []
        for entry in entries:
            for pattern in entry["keywords"]:
                if pattern.search(text):
                    matched.append(entry)
                    break
        return matched

    @staticmethod
    def _expand_matches(
        regex_matched: list[dict[str, Any]],
        all_entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """在 regex 命中基础上追加 constant 条目和 links，按 priority 升序排列。

        1. 若有任何条目命中，则追加所有 constant=True 的条目
        2. 遍历已命中条目的 links 列表，追加被引用的条目（单层，不递归）
        """
        if not regex_matched:
            return []

        entry_by_name: dict[str, dict[str, Any]] = {
            e["name"]: e for e in all_entries
        }

        matched_names: set[str] = {e["name"] for e in regex_matched}
        final: list[dict[str, Any]] = list(regex_matched)

        # 1) 追加常驻条目
        for entry in all_entries:
            if entry["constant"] and entry["name"] not in matched_names:
                final.append(entry)
                matched_names.add(entry["name"])

        # 2) 解析链接（单层）
        for entry in list(final):
            for linked_name in entry.get("links", []):
                if linked_name not in matched_names:
                    linked = entry_by_name.get(linked_name)
                    if linked:
                        final.append(linked)
                        matched_names.add(linked_name)
                    else:
                        logger.warning(
                            f"世界书插件: 条目 [{entry['name']}] "
                            f"链接了不存在的条目 [{linked_name}]"
                        )

        final.sort(key=lambda e: e["priority"])

        return final

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

    @staticmethod
    def _format_injection(
        entries: list[dict[str, Any]],
        header: str,
        footer: str,
        header_text: str = "",
    ) -> str:
        """将匹配到的条目拼装为 XML 标签包裹的注入内容。"""
        if len(entries) == 1:
            body = entries[0]["content"]
        else:
            sections = []
            for i, entry in enumerate(entries, 1):
                sections.append(f"#{i}\n{entry['content']}")
            body = "\n\n".join(sections)

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
        """从 ProviderRequest 的所有位置中清除上一轮注入的 💜 标签。

        只清理带 💜 后缀的标签；stay 条目使用原始标签名，
        不会被匹配到，因此自然留驻在上下文历史中。
        """
        removed = 0

        for lb in self._lorebooks:
            header = lb["p_header"]
            footer = lb["p_footer"]
            cleanup_re = lb["p_cleanup_re"]

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
                # logger.info(
                #     f"世界书插件 [清理]: "
                #     f"已清理 {removed} 处历史注入"
                # )
        except Exception as e:
            logger.error(f"世界书插件 [清理]: {e}", exc_info=True)

    @filter.on_llm_request(priority=-498)
    async def handle_inject(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """
        [注入阶段] priority=-498，在所有其他插件之后执行。

        扫描当前用户消息，匹配各世界书条目，将命中内容注入到指定位置。
        stay 条目使用原始标签注入一次后留驻；普通条目使用 💜
        后缀标签，每轮清理后重新注入。
        """
        if not self._lorebooks:
            return

        try:
            user_text = event.message_str or ""
            if not user_text:
                return

            session_id = event.unified_msg_origin or "unknown"
            stayed_set = self._stayed.setdefault(session_id, set())

            # 每条用户消息 = 一轮，无论内容是否命中任何条目
            turn = self._session_turns.get(session_id, 0) + 1
            self._session_turns[session_id] = turn

            for lb in self._lorebooks:
                book_name = lb["name"]

                # 第一步：纯 regex 匹配
                regex_matched = self._regex_match_entries(
                    user_text, lb["entries"]
                )
                if not regex_matched:
                    continue

                # 第二步：对 regex 命中条目过冷却（stay 条目 cooldown=0，不受影响）
                regex_matched, cooled = self._apply_cooldown(
                    regex_matched, book_name, session_id, turn
                )

                if cooled:
                    logger.info(
                        f"世界书插件 [{book_name}] [冷却]: "
                        f"跳过 {len(cooled)} 个冷却中的条目: "
                        f"{[e['name'] for e in cooled]}"
                    )

                if not regex_matched:
                    continue

                # 第三步：冷却后有存活 → 展开 constant + links
                matched = self._expand_matches(
                    regex_matched, lb["entries"]
                )

                # 第四步：去重——跳过已被 RAG 注入的条目
                matched, skipped = self._dedup_against_rag(
                    matched, req.prompt or ""
                )

                if skipped:
                    logger.info(
                        f"世界书插件 [{book_name}] [去重]: "
                        f"跳过 {len(skipped)} 个已被 RAG 覆盖的条目: "
                        f"{[e['name'] for e in skipped]}"
                    )

                if not matched:
                    continue

                # 第五步：分流 stay vs regular
                stay_entries: list[dict[str, Any]] = []
                regular_entries: list[dict[str, Any]] = []

                for entry in matched:
                    if entry.get("stay"):
                        key = f"{book_name}:{entry['name']}"
                        if key not in stayed_set:
                            stay_entries.append(entry)
                            stayed_set.add(key)
                        # else: already injected & persisted, skip
                    else:
                        regular_entries.append(entry)

                book_prefix = f"{book_name}:"

                # 注入 stay 条目（原始标签，注入一次后留驻）
                if stay_entries:
                    first_stay = not any(
                        k.startswith(book_prefix)
                        for k in stayed_set - {
                            f"{book_name}:{e['name']}"
                            for e in stay_entries
                        }
                    )
                    injection = self._format_injection(
                        stay_entries,
                        lb["header"],
                        lb["footer"],
                        lb["header_text"] if first_stay else "",
                    )
                    self._inject_text(req, injection, lb["position"])

                    names = [e["name"] for e in stay_entries]
                    logger.info(
                        f"世界书插件 [{book_name}] [留驻注入]: "
                        f"{len(stay_entries)} 个条目: {names}"
                    )

                # 注入 regular 条目（💜 标签，每轮清理重注入）
                if regular_entries:
                    # 同书已有留驻内容时不再重复 header_text
                    book_has_stayed = any(
                        k.startswith(book_prefix)
                        for k in stayed_set
                    )
                    injection = self._format_injection(
                        regular_entries,
                        lb["p_header"],
                        lb["p_footer"],
                        "" if book_has_stayed else lb["header_text"],
                    )
                    self._inject_text(req, injection, lb["position"])

                    names = [e["name"] for e in regular_entries]
                    logger.info(
                        f"世界书插件 [{book_name}] [注入]: "
                        f"{len(regular_entries)} 个条目: {names}"
                    )

        except Exception as e:
            logger.error(f"世界书插件 [注入]: {e}", exc_info=True)

    # -------------------------------------------------------------------
    # 生命周期
    # -------------------------------------------------------------------

    async def terminate(self):
        self._lorebooks = []
        self._session_turns = {}
        self._cooldown_state = {}
        self._stayed = {}
        logger.info("世界书插件已停止")
