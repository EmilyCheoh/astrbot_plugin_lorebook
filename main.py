"""
Lorebook - 世界书插件

基于正则关键词匹配的轻量世界书实现：
1. 清理上一轮注入到对话历史中的世界书标签和认知提示
2. 解析手动控制指令（【注入/插入/不注入/不插入】），从消息和 prompt 中移除控制块
3. 扫描当前用户消息，对所有已启用世界书条目做正则匹配，合入手动指定条目
4. 过滤手动禁止条目后执行 cooldown
5. 展开 constant 和单层 links，再次过滤手动禁止条目
6. 将剩余条目按 priority 升序排列，拼装后注入到用户消息之后
7. 若配置了认知提示模板，将提示文本拼在世界书标签的正前方

每本世界书通过 AstrBot 管理面板配置（JSON 文本框），支持动态添加/编辑/删除。

与 FirstWindowInject / PromptTags 兼容：
- 清理阶段 priority=3，在 FirstWindowInject(2) 之前执行
- 注入阶段 priority=-498，在 FirstWindowInject(-499) 和 PromptTags(-500) 之前执行
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
RESEND_PREFIX = "【重发】"

# 手动控制指令
CONTROL_BODY = r"【(?P<action>注入|插入|不注入|不插入)\s+(?P<names>[^】]+)】"
CONTROL_AT_START = re.compile(rf"^\s*(?P<control>{CONTROL_BODY})")
CONTROL_AT_END = re.compile(rf"(?P<control>{CONTROL_BODY})\s*$")
CONTROL_NAME_SPLIT_RE = re.compile(r"[\s,，]+")


@register(
    "Lorebook",
    "FelisAbyssalis",
    "基于正则关键词匹配的世界书插件 - 当用户消息命中条目关键词时自动注入对应内容",
    "2.6.0",
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
        # 预设对话带来的初始 context 条数（用于新窗口检测）
        self._initial_ctx_count = int(config.get("initial_context_count", 0))
        # stay state: tracks which stay entries have already been injected
        # {session_id: {book_name:entry_name, ...}}
        self._stayed: dict[str, set[str]] = {}

        # 重发状态快照: {session_id: {turn, cooldown, stayed, cognition_note}}
        self._pre_turn_states: dict[str, dict[str, Any]] = {}

        # 认知提示
        self._cognition_note_enabled: bool = bool(
            config.get("cognition_note_enabled", False)
        )
        self._cognition_note_template: str = (
            str(config.get("cognition_note_template", ""))
            .replace("\\n", "\n")
            .strip()
        )
        # 上一轮注入的认知提示文本，用于下轮清理
        self._last_cognition_notes: dict[str, str] = {}

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

            # 注入位置：始终使用 user_message_after（忽略 JSON 中的配置值）

            # XML 标签名
            tag_name = str(book.get("tag_name", "Additional-Info")).strip()
            if not tag_name or TAG_NAME_INVALID.search(tag_name):
                logger.warning(
                    f"世界书插件 [{book_name}]: "
                    f"标签名称为空或包含非法字符 (<, >, 换行)，"
                    f"回退到默认值 Additional-Info"
                )
                tag_name = "Additional-Info"

            header = f"<{tag_name}>"
            footer = f"</{tag_name}>"

            # variant: used for stay entries (persists across turns)
            p_tag_name = f"{tag_name}."
            p_header = f"<{p_tag_name}>"
            p_footer = f"</{p_tag_name}>"

            # cleanup regex targets the BASE (non-dotted) tag for regular entries
            cleanup_re = re.compile(
                re.escape(header) + r".*?" + re.escape(footer),
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
                    links = [
                        s.strip() for s in links_raw.split(",") if s.strip()
                    ]
                elif isinstance(links_raw, list):
                    links = [
                        str(s).strip() for s in links_raw if str(s).strip()
                    ]
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
                "tag_name": tag_name,
                "header": header,
                "footer": footer,
                "header_text": header_text,
                "entries": entries,
                # variant for stay entries
                "p_tag_name": p_tag_name,
                "p_header": p_header,
                "p_footer": p_footer,
                "cleanup_re": cleanup_re,
            })

            logger.info(
                f"世界书插件 [{book_name}]: 已加载 {len(entries)} 个条目 "
                f"(标签: {tag_name})"
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
    # 重发状态快照
    # -------------------------------------------------------------------

    def _capture_pre_turn_state(self, session_id: str) -> dict[str, Any]:
        """捕获当前轮开始前的世界书状态快照（用于重发恢复）。"""
        return {
            "turn": self._session_turns.get(session_id),
            "cooldown": dict(self._cooldown_state.get(session_id, {})),
            "stayed": set(self._stayed.get(session_id, set())),
            "cognition_note": self._last_cognition_notes.get(session_id),
        }

    def _restore_pre_turn_state(self, session_id: str) -> bool:
        """从快照恢复世界书状态到上一轮开始前。

        Returns:
            True if restored, False if no snapshot exists.
        """
        snapshot = self._pre_turn_states.get(session_id)
        if snapshot is None:
            return False

        # turn
        if snapshot["turn"] is None:
            self._session_turns.pop(session_id, None)
        else:
            self._session_turns[session_id] = snapshot["turn"]

        # cooldown
        if snapshot["cooldown"]:
            self._cooldown_state[session_id] = dict(snapshot["cooldown"])
        else:
            self._cooldown_state.pop(session_id, None)

        # stayed
        if snapshot["stayed"]:
            self._stayed[session_id] = set(snapshot["stayed"])
        else:
            self._stayed.pop(session_id, None)

        # cognition_note
        if snapshot["cognition_note"] is not None:
            self._last_cognition_notes[session_id] = snapshot["cognition_note"]
        else:
            self._last_cognition_notes.pop(session_id, None)

        return True

    # -------------------------------------------------------------------
    # 手动控制指令
    # -------------------------------------------------------------------

    @staticmethod
    def _extract_manual_controls(
        text: str,
    ) -> tuple[str, set[str], set[str], list[str]]:
        """从用户消息首尾提取手动控制指令。

        只解析位于正文开头或结尾的完整控制块。允许两端各有控制块，
        也允许同一端连续出现多个控制块。正文中间出现的相似文本按
        普通正文保留。

        Returns:
            (clean_text, forced_names, blocked_names, removed_controls)
        """
        working = text
        forced: set[str] = set()
        blocked: set[str] = set()
        removed: list[str] = []

        def record(match: re.Match) -> None:
            action = match.group("action")
            names = {
                name
                for name in CONTROL_NAME_SPLIT_RE.split(
                    match.group("names").strip()
                )
                if name
            }
            if action in ("注入", "插入"):
                forced.update(names)
            else:
                blocked.update(names)
            removed.append(match.group("control"))

        while True:
            changed = False

            start_match = CONTROL_AT_START.match(working)
            if start_match:
                record(start_match)
                working = working[start_match.end():]
                changed = True

            end_match = CONTROL_AT_END.search(working)
            if end_match:
                record(end_match)
                working = working[:end_match.start()]
                changed = True

            if not changed:
                break

        return working.strip(), forced, blocked, removed

    # -------------------------------------------------------------------
    # 匹配
    # -------------------------------------------------------------------

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
    def _dedupe_entries(
        entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按条目名去重，保持输入顺序。"""
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for entry in entries:
            name = entry["name"]
            if name not in seen:
                seen.add(name)
                result.append(entry)
        return result

    @staticmethod
    def _expand_matches(
        seed_entries: list[dict[str, Any]],
        all_entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """在 seed 条目基础上追加 constant 条目和 links，按 priority 升序排列。

        1. 若有任何 seed 条目，则追加所有 constant=True 的条目
        2. 遍历已命中条目的 links 列表，追加被引用的条目（单层，不递归）
        """
        if not seed_entries:
            return []

        entry_by_name: dict[str, dict[str, Any]] = {
            e["name"]: e for e in all_entries
        }

        matched_names: set[str] = {e["name"] for e in seed_entries}
        final: list[dict[str, Any]] = list(seed_entries)

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

    # -------------------------------------------------------------------
    # 编号与查询
    # -------------------------------------------------------------------

    def _indexed_entries(self):
        """按加载顺序为所有条目生成全局连续编号（从 1 开始）。"""
        index = 1
        for book in self._lorebooks:
            for entry in book["entries"]:
                yield index, book, entry
                index += 1

    def _format_lorebook_overview(self) -> str:
        """格式化世界书条目总览。"""
        if not self._lorebooks:
            return "当前没有已加载的世界书条目。"

        lines: list[str] = ["📚 世界书列表"]
        current_book_name = None

        for idx, book, entry in self._indexed_entries():
            if book["name"] != current_book_name:
                current_book_name = book["name"]
                lines.append(f"\n《{current_book_name}》")
            lines.append(f"{idx}. {entry['name']}")

        return "\n".join(lines)

    def _format_lorebook_entry_detail(self, index: int) -> str | None:
        """格式化指定编号条目的详细信息。"""
        for idx, book, entry in self._indexed_entries():
            if idx == index:
                regex_patterns = [p.pattern for p in entry["keywords"]]
                lines = [
                    f"📖 条目详情 #{idx}",
                    "",
                    f"世界书: {book['name']}",
                    f"编号: {idx}",
                    f"条目名: {entry['name']}",
                    f"Regex: {regex_patterns}",
                    f"Priority: {entry['priority']}",
                    f"Constant: {entry['constant']}",
                    f"Stay: {entry['stay']}",
                    f"Cooldown: {entry['cooldown']}",
                    f"Links: {entry['links']}",
                    f"Tag Name: {book['tag_name']}",
                    f"Header Text: {book['header_text'] or '(无)'}",
                    "",
                    "Content:",
                    entry["content"],
                ]
                return "\n".join(lines)
        return None

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
        """从 ProviderRequest 的所有位置中清除上一轮注入的普通条目标签。

        只清理原始（无点后缀）标签；stay 条目使用带点后缀的标签名，
        不会被匹配到，因此自然留驻在上下文历史中。
        """
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
    # 认知提示清理
    # -------------------------------------------------------------------

    @staticmethod
    def _strip_note(text: str, note: str) -> str:
        """从字符串中移除认知提示文本。"""
        cleaned = text.replace(note, "")
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _clean_cognition_note(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """从 ProviderRequest 中清除上一轮注入的认知提示。"""
        note = self._last_cognition_notes.pop(session_id, "")
        if not note:
            return 0

        removed = 0

        # 清理 prompt
        if hasattr(req, "prompt") and isinstance(req.prompt, str):
            if note in req.prompt:
                req.prompt = self._strip_note(req.prompt, note)
                removed += 1

        # 清理 contexts
        if hasattr(req, "contexts") and req.contexts:
            for i, msg in enumerate(req.contexts):
                if isinstance(msg, str) and note in msg:
                    cleaned = self._strip_note(msg, note)
                    req.contexts[i] = cleaned if cleaned else ""
                    removed += 1
                elif isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, str) and note in content:
                        cleaned = self._strip_note(content, note)
                        msg_copy = msg.copy()
                        msg_copy["content"] = cleaned
                        req.contexts[i] = msg_copy
                        removed += 1
                    elif isinstance(content, list):
                        for j, part in enumerate(content):
                            if (
                                isinstance(part, dict)
                                and part.get("type") == "text"
                                and isinstance(part.get("text"), str)
                                and note in part["text"]
                            ):
                                cleaned = self._strip_note(
                                    part["text"], note
                                )
                                part_copy = part.copy()
                                part_copy["text"] = cleaned
                                content[j] = part_copy
                                removed += 1

        return removed

    # -------------------------------------------------------------------
    # 注入辅助
    # -------------------------------------------------------------------

    @staticmethod
    def _inject_text(req: ProviderRequest, text: str) -> None:
        """将文本追加到用户消息之后。"""
        prompt = req.prompt or ""
        req.prompt = f"{prompt}\n\n{text}" if prompt else text

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
        上一轮注入的世界书标签和认知提示。
        """
        if not self._lorebooks:
            return

        try:
            session_id = event.unified_msg_origin or "unknown"

            # 清理认知提示
            self._clean_cognition_note(req, session_id)

            # 清理世界书标签
            self._clean_contexts(req)

        except Exception as e:
            logger.error(f"世界书插件 [清理]: {e}", exc_info=True)

    @filter.on_llm_request(priority=-498)
    async def handle_inject(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """
        [注入阶段] priority=-498，在 PromptTags(-500) 之前执行。

        清理 -> 解析手动控制 -> regex + 手动 seed -> blocked 过滤 ->
        cooldown -> constant/links 展开 -> 再次 blocked 过滤 ->
        stay/regular 分流 -> 注入
        """
        try:
            user_text = event.message_str or ""
            if not user_text:
                return

            session_id = event.unified_msg_origin or "unknown"

            # ------ 重发检测 ------
            is_resend = user_text.startswith(RESEND_PREFIX)
            if is_resend:
                user_text = user_text.removeprefix(RESEND_PREFIX)
                if isinstance(req.prompt, str):
                    req.prompt = req.prompt.replace(RESEND_PREFIX, "", 1)

            # ------ 手动控制指令 ------
            user_text, forced_names, blocked_names, controls = (
                self._extract_manual_controls(user_text)
            )

            if isinstance(req.prompt, str):
                for control in controls:
                    req.prompt = req.prompt.replace(control, "", 1)

            # 内部标记已清理完毕；无世界书时不需要继续匹配
            if not self._lorebooks:
                return

            # ------ 编号解析 ------
            if forced_names or blocked_names:
                index_to_name: dict[int, str] = {
                    idx: entry["name"]
                    for idx, _book, entry in self._indexed_entries()
                }

                def _resolve(names: set[str]) -> set[str]:
                    resolved: set[str] = set()
                    for name in names:
                        if name.isdecimal() and int(name) > 0:
                            resolved.add(
                                index_to_name.get(int(name), name)
                            )
                        else:
                            resolved.add(name)
                    return resolved

                forced_names = {n.lower() for n in _resolve(forced_names)}
                blocked_names = {n.lower() for n in _resolve(blocked_names)}

                # 记录未知条目名
                known_names_lower = {
                    entry["name"].lower()
                    for book in self._lorebooks
                    for entry in book["entries"]
                }
                unknown = (forced_names | blocked_names) - known_names_lower
                if unknown:
                    logger.warning(
                        f"世界书插件 [手动控制]: "
                        f"未找到条目: {sorted(unknown)}"
                    )

                if forced_names:
                    logger.info(
                        f"世界书插件 [手动注入]: "
                        f"{sorted(forced_names)}"
                    )
                if blocked_names:
                    logger.info(
                        f"世界书插件 [手动禁止]: "
                        f"{sorted(blocked_names)}"
                    )

            # ------ 窗口重置检测 ------
            ctx_count = len(req.contexts) if req.contexts else 0
            prev_turn = self._session_turns.get(session_id, 0)
            if prev_turn > 0 and ctx_count <= self._initial_ctx_count:
                self._session_turns.pop(session_id, None)
                self._cooldown_state.pop(session_id, None)
                self._stayed.pop(session_id, None)
                self._last_cognition_notes.pop(session_id, None)
                self._pre_turn_states.pop(session_id, None)
                logger.info("世界书插件 [窗口已重置]")

            # ------ 重发恢复 -> 快照保存（顺序不可调换） ------
            if is_resend:
                restored = self._restore_pre_turn_state(session_id)
                if restored:
                    logger.info(
                        "世界书插件 [重发]: 已恢复上一轮开始前的状态"
                    )
                else:
                    logger.warning(
                        "世界书插件 [重发]: "
                        "没有可恢复的状态，按普通消息处理"
                    )

            self._pre_turn_states[session_id] = (
                self._capture_pre_turn_state(session_id)
            )

            stayed_set = self._stayed.setdefault(session_id, set())

            # 每条用户消息 = 一轮，无论内容是否命中任何条目
            turn = self._session_turns.get(session_id, 0) + 1
            self._session_turns[session_id] = turn

            # ------ 收集所有世界书的注入文本和认知标签名 ------
            injection_parts: list[str] = []
            cognition_tag_names: list[str] = []

            for lb in self._lorebooks:
                book_name = lb["name"]

                # 第一步：regex 匹配
                regex_matched = self._regex_match_entries(
                    user_text, lb["entries"]
                )

                # 第二步：合入手动指定条目
                manual_entries = [
                    entry
                    for entry in lb["entries"]
                    if entry["name"].lower() in forced_names
                ]

                seed = self._dedupe_entries(
                    regex_matched + manual_entries
                )

                # 第三步：第一次 blocked 过滤（在 cooldown 之前）
                seed = [
                    e for e in seed
                    if e["name"].lower() not in blocked_names
                ]

                if not seed:
                    continue

                # 第四步：cooldown（stay 条目 cooldown=0，不受影响）
                seed, cooled = self._apply_cooldown(
                    seed, book_name, session_id, turn
                )

                if cooled:
                    logger.info(
                        f"世界书插件 [{book_name}] [冷却]: "
                        f"跳过 {len(cooled)} 个冷却中的条目: "
                        f"{[e['name'] for e in cooled]}"
                    )

                if not seed:
                    continue

                # 第五步：展开 constant + links
                matched = self._expand_matches(seed, lb["entries"])

                # 第六步：第二次 blocked 过滤（拦截展开带出的条目）
                matched = [
                    e for e in matched
                    if e["name"].lower() not in blocked_names
                ]

                if not matched:
                    continue

                # 记录本书的 tag name 用于认知提示
                cognition_tag_names.append(lb["tag_name"])

                # 第七步：分流 stay vs regular，收集注入文本
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

                # stay 在前，regular 在后
                if stay_entries:
                    injection = self._format_injection(
                        stay_entries,
                        lb["p_header"],
                        lb["p_footer"],
                        lb["header_text"],
                    )
                    injection_parts.append(injection)

                    names = [e["name"] for e in stay_entries]
                    logger.info(
                        f"世界书插件 [{book_name}] [留驻注入]: "
                        f"{len(stay_entries)} 个条目: {names}"
                    )

                if regular_entries:
                    injection = self._format_injection(
                        regular_entries,
                        lb["header"],
                        lb["footer"],
                        "" if stay_entries else lb["header_text"],
                    )
                    injection_parts.append(injection)

                    names = [e["name"] for e in regular_entries]
                    logger.info(
                        f"世界书插件 [{book_name}] [注入]: "
                        f"{len(regular_entries)} 个条目: {names}"
                    )

            if not injection_parts:
                return

            # 认知提示：拼在所有世界书标签的正前方
            if (
                self._cognition_note_enabled
                and self._cognition_note_template
                and cognition_tag_names
            ):
                # 去重并保持触发顺序
                seen: set[str] = set()
                unique_names: list[str] = []
                for name in cognition_tag_names:
                    if name not in seen:
                        seen.add(name)
                        unique_names.append(name)

                tag_list = ", ".join(f"`{n}`" for n in unique_names)
                note = self._cognition_note_template.replace(
                    "{tag_name}", tag_list
                )
                injection_parts.insert(0, note)
                self._last_cognition_notes[session_id] = note

            # 一次性注入
            combined = "\n\n".join(injection_parts)
            self._inject_text(req, combined)

        except Exception as e:
            logger.error(f"世界书插件 [注入]: {e}", exc_info=True)

    # -------------------------------------------------------------------
    # 查询命令
    # -------------------------------------------------------------------

    @filter.command_group("lorebook")
    def lorebook(self):
        pass

    @lorebook.command("list")
    async def lorebook_list(self, event: AstrMessageEvent):
        """列出所有已加载的世界书条目。"""
        yield event.plain_result(self._format_lorebook_overview())

    @lorebook.command("view")
    async def lorebook_view(
        self, event: AstrMessageEvent, index: str = ""
    ):
        """查看世界书条目详情。"""
        raw = index.strip()
        if not raw:
            yield event.plain_result(self._format_lorebook_overview())
            return

        if not raw.isdecimal() or int(raw) <= 0:
            yield event.plain_result("用法：/lorebook view <编号>")
            return

        result = self._format_lorebook_entry_detail(int(raw))
        if result is None:
            total = sum(
                len(book["entries"]) for book in self._lorebooks
            )
            if total:
                result = (
                    f"未找到编号 {raw}；"
                    f"当前有效编号范围为 1-{total}。"
                )
            else:
                result = "当前没有已加载的世界书条目。"

        yield event.plain_result(result)

    # -------------------------------------------------------------------
    # 生命周期
    # -------------------------------------------------------------------

    async def terminate(self):
        self._lorebooks = []
        self._session_turns = {}
        self._cooldown_state = {}
        self._stayed = {}
        self._last_cognition_notes = {}
        self._pre_turn_states = {}
        logger.info("世界书插件已停止")
