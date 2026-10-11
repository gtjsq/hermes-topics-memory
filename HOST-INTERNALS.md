# 本仓库碰了宿主的哪些内部点

> 给 **Hermes 团队**和后来者看的清单：哪些是**官方接口**（正当）、哪些是**越界**（不保证）。
> 版本基准：**Hermes Desktop 0.21.0-cn.18 (Windows)**。
> 作者把这份清单单独写出来，是因为"能跑"不等于"合规" —— 越界的地方应该被一眼看到。

---

## 一、正当的部分：走的是官方插件接口

| 用到的 | 依据 |
|---|---|
| `agent.memory_provider.MemoryProvider` | 官方文档《Memory Provider Plugins》给出的 ABC |
| `ctx.register_memory_provider(provider)` | 同上 |
| `sync_turn(self, user_content, assistant_content, *, session_id="", messages=None)` | **官方 ABC 的签名**（实测：写成 `*a/**kw` 会让这个钩子静默失效） |
| `prefetch(self, query, *, session_id="") -> str` | 官方 ABC |
| `get_tool_schemas(self) -> list[dict]` | 官方 ABC，且是**必需**的抽象方法 |
| `handle_tool_call(self, tool_name, args, **kwargs)` | 与 `agent.memory_manager.MemoryManager.handle_tool_call` 的调用签名一致（扫宿主字节码核对过） |
| `on_session_end` / `on_pre_compress` / `shutdown` | 官方生命周期钩子 |

**结论**：插件主体是**正规插件**，不是 monkey-patch。

---

## 二、越界的部分：非公开 API，官方未承诺

| 碰的东西 | 怎么碰的 | 一变就坏在哪 |
|---|---|---|
| `messages.api_content` 字段 | **写**（放索引行 / 放记忆注入） | 宿主靠 `turn_context.substitute_api_content` 把它盖到 `content` 上；替换逻辑一改，索引就白写 |
| `message_summaries` 表 | **读**（取要点）+ **写**（外部脚本产出） | 内部表；结构或语义一变，要点/索引链路断 |
| `context.engine` 槽位 | **占用**（装了自定义 context engine） | 官方支持这个槽，但本仓库的引擎会"让位"并**关掉压缩** |
| `compression.threshold` | **改成 `1.0`**（永不自动压缩） | 直接改变宿主默认行为 |
| `messages.active` / `compacted` | **读**（判断某轮是否已退场） | 内部字段 |
| `get_tool_schemas` 的调用次数 | **依赖**（靠"被调 2 次"判断工具真的发给了模型） | 调用时机一变，这个判据失效 |

---

## 三、给上游的两条建议

如果这套思路要变成官方支持的能力，最小需要两个孔：

1. **一个"可回溯的上下文索引"扩展点**
   现在官方只有"压缩"这一条路：老上下文要么留着，要么被总结成一段摘要（**原文不可逆地丢掉**）。
   本仓库证明另一条路可行 —— 老轮次换成**一行带 `#id` 的索引**，原文留在库里，需要时取回。
   如果官方给一个正式的挂点，就不必像本仓库这样写 `api_content`。

2. **把"记忆注入"与"索引替换"分开**
   现在两者**共用 `api_content` 一个字段** → 谁后写谁赢（本仓库实测到过互相覆盖）。
   如果官方分成两个字段（或给一个明确的优先级约定），第三方插件就不用互相抢了。

---

## 四、一句话

**本仓库不是来教官方怎么做的，是把一条走通了的路（连同它的越界点）摊开给人看。**
