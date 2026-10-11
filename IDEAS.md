# IDEAS —— 可以直接拿走的五个点子

> 这份文件是这个仓库**唯一真正的交付物**。代码你一行都不用看，这五条拿走就行。
> English TL;DR at the bottom.
>
> 全部来自 2026-10 的一次一手实验（Hermes Desktop + 一份 1213 条的真实私人记忆库），
> 不是论文、不是综述。数字都是当时实测的；下面会写明哪些能复现、哪些只是当时的观察。

---

## 1. 上下文压缩是不可逆的；索引是可逆的

**主流做法**：老对话喂给模型总结成一段"纪要"，原文丢掉。
**问题**：纪要是**别人对这段对话的判断**。它漏了数字、错了措辞，而原文已经烧掉 ——
**谁也没法证伪**。典型的翻车现场：模型拿摘要当原文回答问题，被追问细节时开始编。

**做法**：老的轮次换成**一行带门牌号的目录条目**（`…｜原文 #9402267`），原文留在库里，
需要时按键取回（Hermes 自带 `session_search(session_id, around_message_id=…)` 就是那个钥匙）。

**要点**：把"可逆"当成硬指标，而不是"摘要质量好不好"。
摘要质量再高也是判断，判断就可以错；可逆就不会错到底。

---

## 2. 向量只负责"别漏"，"别错"交给看得见上下文的模型

用"相似度分数 + 一个门槛"决定这条记忆该不该给模型看，**注定调不好** ——
因为**分数没有上下文、没有世界知识**。同一个分数，这轮是金矿，下轮是纯噪音。
当时的实测：同领域的"相关"与"不相关"样本，分数是**重叠**的（0.55 / 0.56 分不开）。

**做法**：
- 宽门槛、多给候选（当时是 8 条），开头明说"**不一定相关，自己挑**"；
- 本地交叉编码器只用来把候选**排个序**（实测进前 3 从 60% → 73.3%），不用它做取舍；
- 真正的裁决交给**看得见整段对话的模型**自己。

---

## 3. 只"推"不够，要"推 + 拉"

每轮自动塞几条，那是"推"。模型**需要时能自己查**，才是一双完整的手：

- `search_memory(query, k, pool)` —— 拉（模型主动查）
- `save_memory(content, reason, importance, msg_id)` —— 写（模型主动记）

两个细节值得抄：
- **每轮限次数**（防它狂查/狂写），轮次结束自动重置；
- 结果里**老实说分低**：「分都不高（最高 0.42，精确串也没命中）—— 可能压根没有这条记忆，
  别硬用这些。」 这一句比多给十条记忆都值钱。

---

## 4. 重点池：用"每轮都带"对付"绝不能再漏"

少量条目、**每轮无条件注入**（不看相关度、不看门槛）。每条带**来源指针**，三层对得上：

```
内容  ↔  当时的要点行（索引）  ↔  原文（按 #id 取回）
```

两个写入入口：
1. 模型随时写（`save_memory`）；
2. **收尾自动挑**：从已经生成好的要点里筛「纠正类 / 用户硬约束」，**零模型成本**、幂等、
   每次最多 2 条 —— 这是给"模型自己会漏"准备的**第二双眼睛**。

⚠️ 这个池子**必须配上限 + 淘汰或归档机制**（见下面坑 3）。作者的版本当时正好撞上了上限。

---

## 5. 让模型知道自己读的是"目录"，不是"原文"

索引条目一律带 `｜原文 #id`。这不是装饰 ——
**模型知道自己在看目录时，才会去取原文，而不是硬答。**

配套的一句：索引条目要**多维度、半开放** —— 主题是什么、内核是什么、发生了什么重要的事、
对用户的重要程度、对下一步的重要程度。这些维度永远穷举不完，所以结构要能随时加字段。
**取舍标准**：无意义的信息不要；真正重要的是「发现了什么 bug、出现了什么重点、这段对话要干什么」。

---

## 踩过的六个坑（数字都标了口径）

1. **向量分不清极性**：「X 是 A」和「X 不是 A」相似度一样高
   → **不能**用向量决定"该删哪条记忆"。
2. **重复是生成端的病**：靠"前 40 字相同"这种便宜的去重键，在真实池子上只认得出**千分之几**
   的真重复（口径：把余弦 ≥0.85 当成"真重复"，在 1213 条记忆池上，前 40 字键召回了 0.35%，
   前 24 字键 2.18%；故意让键变短召回会高一点，但那就是换一个漏法）。
   反向检查倒是不错：它认出来的那几对基本没错（精度好、召回几乎为零）。
   → **去重要在生成端做**，插件层去重只是门口拦一道。
3. **重点池会涨爆**：每轮无条件注入 = 池子涨大就是**上下文税**；一旦撞上限，
   **超出的那些既进不了上下文、也搜不到**（当时的实测：池子 12 条正好撞上"最多注入 12 条"，
   而且 12 条重要度全是 5，等于没排序）。必须有"按相关度取 + 淘汰/归档"，不能"先全带"。
4. **短句向量干净，长文糊**：实测短句自匹配中位 0.886，长文最低 0.576（**连自己都匹配不上**）。
   → 索引要"一行 + 类型标签"，别拿长摘要去算向量。
5. **模型自己会漏**：一整轮 300+ 条消息里，模型**一次都没主动记**
   → 所以必须有"自动挑"这第二双眼睛。
6. **便宜的去重键是假安全感**：改长度只是换一个漏法（见坑 2）；
   真正要用向量去重时，又撞上坑 1（分不清极性）。

---

## English TL;DR

1. **Compression is lossy; an index is reversible.** Replace retired rounds with a one-line entry
   carrying an address (`… | original #9402267`) and keep the original in the DB. Make
   *reversibility* the hard requirement — a summary is somebody's judgement, and judgement can be
   wrong while the original can no longer be checked.
2. **Let vectors handle "don't miss"; let the model handle "don't be wrong."** Similarity scores
   carry no context — measured: relevant and irrelevant samples overlap (0.55 vs 0.56). Use a wide
   gate, hand over a handful of candidates with an explicit "these may not be relevant, pick
   yourself", and let a reranker only *order* them, never *decide*.
3. **Push alone is not enough — add pull.** Two tools: `search_memory` (read) and `save_memory`
   (write), with per-turn quotas and an honest low-score warning instead of silently padding.
4. **A small "important" pool injected every turn** beats relevance filtering for things that must
   never be missed, with provenance kept on three levels (content ↔ index line ↔ original).
   Give it a cap and an eviction path, or it becomes a permanent context tax.
5. **Tell the model it is reading an index, not the original.** That is what makes it go fetch the
   original instead of answering from a summary.

**Six pitfalls:** (1) embeddings cannot tell "X is A" from "X is not A" — never let a vector decide
deletions; (2) duplication is a *generation-side* disease, and cheap prefix-based dedup keys
recover well under 1% of real duplicates; (3) an always-injected pool needs a cap *and* an eviction
path; (4) short lines embed cleanly, long text does not (self-similarity 0.886 vs 0.576);
(5) models forget to write memories on their own — you need a second, non-model pass;
(6) cheap dedup keys are false confidence.
