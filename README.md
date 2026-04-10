# Lorebook - 世界书插件

基于正则关键词匹配的轻量世界书实现。当用户消息命中条目关键词时，自动将对应内容注入到 LLM 请求中，下一轮自动清理。

## 工作原理

每轮 LLM 请求前执行两个阶段：

1. **清理阶段** (priority=3)：从对话历史中移除上一轮注入的 `<Additional-Info>` 标签
2. **注入阶段** (priority=-498)：扫描当前用户消息，正则匹配所有已启用条目，将命中内容按优先级排序后注入

## 插件间优先级

```
清理阶段（数字越大越先执行）
  priority=3   Lorebook 清理
  priority=2   FirstWindowInject 清理
  priority=1   PromptTags 清理
  priority=0   LivingMemory

注入阶段（数字越小越先执行）
  priority=-498  Lorebook 注入
  priority=-500  PromptTags 注入
  priority=-499  FirstWindowInject 注入
```

## 条目管理

在 AstrBot 管理面板中直接添加/编辑/删除条目。每个条目包含：

| 字段 | 说明 |
|------|------|
| 名称 | 条目标识，用于日志 |
| 启用 | 条目开关 |
| 优先级 | 1-10，1 = 最高优先（最靠前注入），10 = 最低 |
| 正则触发 | 关键词/正则列表，任一匹配即触发 |
| 注入内容 | 触发时注入的内容 |

### 关键词正则示例

- `猫` — 包含"猫"
- `猫|喵` — 包含"猫"或"喵"
- `(?i)hello` — 不区分大小写
- `\b周末\b` — 精确匹配

## 全局配置项

- **启用世界书**：全局开关
- **注入位置**：user_message_before / user_message_after / system_prompt
- **XML 标签名称**：默认 `Additional-Info`
- **最大注入条目数**：每轮最多注入多少条目（默认 10，0 = 不限制）
