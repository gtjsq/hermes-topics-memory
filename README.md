# hermes-topics-memory

> ⛔ **请不要安装这个。这是一份灵感展示，不是能用的软件。**
> 理由见下面第一节 —— 四条，都是实测出来的，不是客套话。
>
> 想直接拿走点子 → [`IDEAS.md`](IDEAS.md)（一页纸）
> 想看完整论证 → [`DESIGN.zh-CN.md`](DESIGN.zh-CN.md)（灵魂）
> **这两份才是这个仓库真正要交付的东西**；代码只是"我们确实这么做过了"的证据。
>
> English readers → [English section](#english) at the bottom.
> Not affiliated with, endorsed by, or supported by Nous Research.

用**可回溯的索引**替换掉有损的上下文压缩：退场的老对话变成**一行带门牌号的目录条目**
（`…｜原文 #9402267`），原文留在库里，需要时按号取回 ——
而不是把原文烧成一段摘要、再塞回工作台。

我当时的原话（保留原始表述）：**"AI 只有拥有记忆系统并强介入才算完整体。"**

---

## 一、为什么你不该装

### 1. 装上等于装了个空壳

插件启动后第一件事，是找你的记忆库文件（`library.json`）。不在，它就把自己整个标成
**不可用**。而那份记忆库是 1213 条私人笔记、5120 条标签、206 条对话索引 ——
**不可能随仓库发布**。

### 2. 真正让它跑起来的东西不在这份仓库里

我自用的流水线有三百多个文件，路径全写死，里面混着大量跑完就废的实验脚本
（连我自己都没整理）。仓库里的 `pipeline/` 只有一个说明文件。
**所以这个仓库是「能读，不能复现」。**

### 3. 它会占掉两个单选位置，还关掉宿主的自动压缩

官方规定：记忆后端与上下文引擎**同一时刻只能各有一个**。它两个都占了
（`memory.provider: topics` + `context.engine: handoff`），并把压缩门槛改成 `1.0`
（原生自动压缩永不触发）。
它和别的记忆插件不是"可能冲突"，是**机制上互斥** —— 装了它，别的记忆插件就上不来了。

### 4. 而且还有一批配套文件我没跟着发布

所以就算你照着第五节装，它也会**静默少掉四样能力**（不报错，只是不工作）：

| 少掉的 | 你会看到什么 |
|---|---|
| 每轮收尾的流水线（生成要点 / 更新池子 / 挑重点） | 池子停止更新；而且**连错误都看不见**（启动时把报错丢进了垃圾桶） |
| 精确串召回（`inverted-index.json`） | 问「0.8.14 在哪条」这类问题，答案在库里却查不出来 |
| 候选表模式开关 `CANDIDATES_ON` | 插件退回老的主题路由分支：没有候选表、没有精排 |
| 向量服务 `embed-server.mjs` | 服务活着时没事；一旦它空闲自退，就再也拉不起来，向量检索静默降级成关键词 |

**这些是设计事实，不是"没写完的 TODO"。** 我没打算修 —— 因为这东西的目的不是给人用。

---

## 二、那这里有什么

| 在仓库里 | 不在仓库里 |
|---|---|
| 插件的实现（`memory/topics/`）—— 拿来读 | **任何真实记忆数据**（池子是私人笔记，见 `samples/`） |
| 本地向量服务的参考实现（`service/`） | 向量**模型权重**（要自己下） |
| 池子的**结构说明**（`pipeline/README.md`、`samples/README.md`） | 生成池子的脚本（我自用、没整理，见上面第 2 条） |
| 一页纸的点子（`IDEAS.md`）、设计理念（`DESIGN.zh-CN.md`）、越界清单（`HOST-INTERNALS.md`） | 你的 `config.yaml`（里面有 API key） |

## 三、先读这两份

- [`IDEAS.md`](IDEAS.md) —— **五个可以直接拿走的点子**（一页，中文 + English TL;DR）
- [`DESIGN.zh-CN.md`](DESIGN.zh-CN.md) —— 我原始的完整论证：
  为什么是「索引 + 拉取 + 重点池」，而不是「压缩 + 推送」

## 四、状态

Alpha / 研究产物。只在 **Hermes Desktop 0.21.0-cn.18 (Windows)** 上跑通过，别的环境**没试过**。
它踩在宿主未承诺的内部细节上 —— 详见 [`DISCLAIMER.md`](DISCLAIMER.md)。
"能跑"不等于"合规"，越界的地方在 [`HOST-INTERNALS.md`](HOST-INTERNALS.md) 里逐条列出来了。

## 五、如果你还是想试（不推荐）

**下面的步骤是故意不写全的。** 缺的那部分（池子、流水线、模型）不在这份仓库里，
所以照做也只会得到一个空壳。真要试，请先读 [`DISCLAIMER.md`](DISCLAIMER.md) 和 `samples/README.md`。

大致形状（**不是能被照抄的步骤**）：

1. 自己弄一个中文向量模型（我用的是 `bge-small-zh-v1.5`）。放哪儿要看你自己的环境，
   **别照抄文档里写的路径** —— 代码是按"启动时的工作目录"找模型的。
2. 把 `memory/topics/`（`plugin.yaml` + `__init__.py`）放进 `<HERMES_HOME>/plugins/topics/`。
3. 自己建池子（`pipeline/README.md` 只给了流程，没给脚本），
   然后在插件的运行目录里建一个空文件 `CANDIDATES_ON`。
4. 重启 Hermes。日志里应该出现 `候选表：粗筛门槛 0.42 → …`。

## 六、许可

MIT —— 见 [`LICENSE`](LICENSE)。
**这意味着你可以拿走、改、甚至闭源商用。** 我知道，也接受这一点：
这东西是个半成品，能被人拿去用，比锁起来好。
（以后再发布的重要成果可能会换一个限制商用的许可；但**已经发布的这些版本会一直是 MIT**。）

向量模型有自己的许可（BGE 系列请看 Hugging Face 上的 model card）。

---

## English

> ⛔ **Do not install this.** It is a design sketch, not software. Four measured reasons below.

A memory-provider + context-index plugin for **Hermes Agent** that replaces lossy context
compression with a **reversible index**: retired rounds become a one-line entry carrying an
address (`… | original #9402267`) while the original text stays in the database.
Not affiliated with, endorsed by, or supported by Nous Research.

**Why you should not install it**

1. **Without the memory pool it is a shell.** The plugin checks for `library.json` on startup and
   marks itself unavailable if it is missing. That pool is 1213 private notes — it cannot ship.
2. **The thing that makes it run is not in this repo.** My pipeline is ~300 files with
   hardcoded paths. `pipeline/` here contains a description only: readable, not reproducible.
3. **It occupies two single-slot positions** (memory provider + context engine) and sets
   `compression.threshold: 1.0`, disabling the host's own compression. It is mechanically
   incompatible with other memory plugins, not merely "possibly conflicting".
4. **Some supporting files were never published**, so installing it silently loses four
   capabilities (no errors — just silence).

**Start here instead:** [`IDEAS.md`](IDEAS.md) — five ideas you can take and use
(reversible indexing; vectors for recall and the model for precision; push *and* pull;
an always-injected "important" pool with provenance; telling the model it reads an index).
Then [`DESIGN.zh-CN.md`](DESIGN.zh-CN.md) for the full argument (Chinese).

**License:** MIT — you may take it, change it, and even ship it closed-source. I know
and is fine with that; it is a half-finished experiment, and being used beats being locked up.
