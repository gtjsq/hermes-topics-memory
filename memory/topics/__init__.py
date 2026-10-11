"""topics —— 索引式记忆后端：把退场的老轮次换成可回溯的一行索引；向量只做粗筛、由模型裁决。

它有两套工作方式，靠开关文件 `CANDIDATES_ON` 切换：

  A. 候选表模式（开关存在时 —— **这是实际在用的那套**）
     1. 问题交给本地 Node 服务算向量（热调用 3~11 ms；服务懒启动、空闲自退）
     2. 三个池子（记忆 / 标签 / 被裁对话）各按宽门槛粗筛，合成一张候选表
        （门槛 0.42、最多 8 条、总计 ≤560 字；开头明说「不一定相关，自己挑」）
     3. 先本地交叉编码器给每池前几条精排；精确串（倒排）命中的直接置顶、不参与混排
        —— 向量不擅长「精确」，混排只会互相拖累
     4. 剩下的名额**按池轮转**补：池与池之间不比分数
        （池子越大，最大值天然越高，那是大数定律，不是相关性）
     5. 另加一块「重点记忆」：不看相关度、**每轮无条件注入**（安全绳，绝不能漏）

  B. 主题路由模式（开关不存在时的老路）
     跟 31 个主题质心比，取最近的 2 个主题 → 只在这 2 个主题的库文档里排余弦取前 3 条
     → 每条截 100 字、合计 ≤300 字。一条都没命中就返回空串（硬规矩：零命中就零注入）

另外两个钩子（都已实现，不是「留到以后」）：
  - `sync_turn`：整段对话写完的那一刻被叫一次 → 起一趟短命进程去备菜，**立刻返回**
    （不起常驻循环、不用定时器）
  - `handle_tool_call`：模型自己来查（`search_memory`）/ 自己来记（`save_memory`）

安全与降级：
  - 三个池子只读；`state.db` **只读**（`_latest_msg_id` / `_summary_of` 拿来源锚点用）
  - 向量服务挂了 → 退回关键词路由（精度会掉，但不崩）
  - library.json 不在 → `is_available()` 返回 False，整个后端不会被启用
    （**注意**：这一条意味着「没有池子 = 装了等于空壳」）
  - 任何异常都被兜住，最多写一行日志

为什么必须注册记忆后端：实测过——`$HERMES_HOME\\plugins\\` 下的插件
**只有调了 `ctx.register_memory_provider(...)` 才会被加载**，只注册别的角色根本不会被 import。
"""
from __future__ import annotations

import array
import json
import math
import os
import re
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

# ---- 路径（发布版：全部可配置，不写死任何人的机器）--------------------------------
# 数据根：优先环境变量 HERMES_HOME；没有就退回 ~/.hermes
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))

# 插件自己的运行目录（日志 / 状态 / 开关文件）
#   优先用宿主官方的 plugin_data_dir（<hermes home>/plugin-data/<name>/）——
#   官方文档明确写着：**不要把运行状态写进插件目录**（plugins update/remove 会把它删掉）。
#   拿不到官方 API 就退回环境变量 TOPICS_DATA_DIR，再退回默认位置。
try:
    from plugins.plugin_storage import plugin_data_dir as _plugin_data_dir
    PLUGIN_DATA = Path(_plugin_data_dir("topics"))
except BaseException:
    PLUGIN_DATA = Path(os.environ.get("TOPICS_DATA_DIR")
                       or (HERMES_HOME / "plugin-data" / "topics"))

# 三个检索池（由 pipeline/ 里的脚本产出；缺哪个就少哪个能力，不会崩）
DATA = HERMES_HOME / "topics-data" / "library.json"
# 第二个池：被滚动压缩裁掉的对话原文（离线索引，见 build_conv_index_v2.py）
CONV = HERMES_HOME / "topics-data" / "conversations.json"
# 标签池：由 build 脚本产出的多通道条目（主题/要点，每条带类型与重要度）
LABELS = HERMES_HOME / "topics-data" / "mem-multichannel.json"

# 可选外挂（不存在 → 对应功能自动失效：向量检索降级成关键词，精确串召回关掉）
TOPICS_EXT = Path(os.environ.get("TOPICS_EXT_DIR") or (HERMES_HOME / "topics-ext"))
SERVICE = Path(os.environ.get("TOPICS_EMBED_SERVER") or (TOPICS_EXT / "embed-server.mjs"))
INVERTED = Path(os.environ.get("TOPICS_INVERTED") or (TOPICS_EXT / "inverted-index.json"))
RUN_FOREVER = Path(os.environ.get("TOPICS_RUN_FOREVER") or (TOPICS_EXT / "run-forever.ps1"))

LOG = PLUGIN_DATA / "topics.log"
SERVICE_LOG = PLUGIN_DATA / "service-out.log"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"

ROUTE_TOPICS = 2        # 路由取最近的几个主题（考卷③定：2 个）
TOP_N = 3              # 最多注入几条
PER_ITEM_CHARS = 100    # 每条截断字数
TOTAL_CHARS = 300       # 合计上限

# 相关性门槛（标定见 calibrate_gate.py / 标定报告）。
# 为什么必须有：不加门槛时，插件对「今天晚饭吃什么」也注入了 3 条无关记忆 ——
# 因为路由永远能选出"最近的主题"，于是永远有命中、永远注入，
# 正好犯下设计文档点名要治的病（每轮硬塞没用的东西进上下文）。
# 标定结果（库文档最大余弦）：
#   0.62 → 挡掉正样本 0.0% / 挡掉负样本 95.0%
#   0.64 → 挡掉正样本 0.0% / 挡掉负样本 100.0%
#   0.65 → 挡掉正样本 1.2% / 挡掉负样本 100.0%   ← 取它
# 理由：按「精度优先」，用 80 条正样本里牺牲 1 条，换无关查询全挡住。
# 已知弱点：负样本只有 20 条、且是我手写的（不是标注工共识）。
GATE = 0.65

# 对话池单独一个门槛（2026-10-10 实测标定，不能跟记忆库共用）：
#   对话池的真匹配集中在 0.539~0.606（长篇解释的切片，换种问法分就低）
#   无关对照（"今天晚饭吃什么"）最高只有 0.319
#   取 0.56：真匹配都进来，无关的仍有 0.2 的余量。
#   （2026-10-11 修：注释原来写"取 0.52"、代码一直是 0.56 —— 标注与实现对不上。
#     以代码为准；0.56 落在真匹配区间 0.539~0.606 的中段，比 0.52 更稳。）
GATE_CONV = 0.56

# ==========================================================================
# 候选表模式（用户 2026-10-10 拍板：「保留注入，但给更多候选让它自己刷」）
#
# 为什么改（用户指出我把两件事搞混了）：
#   我之前用「向量分数 + 门槛」替模型拍板 —— 那是**替它做裁决**，是分类任务。
#   真正要做的是**让模型知道哪些记忆被需要**。标量没有上下文、没有世界知识，
#   所以门槛怎么调都分不开同领域的相关与不相关（实测 0.55 / 0.56 重叠）。
#
# 分工反转：
#   向量 → 只做**粗筛**（宽门槛、多给候选，宁可多给，别漏）
#   模型 → 做**裁决**（它看得到上下文，自己刷掉不要的）
#
# 这一模式下：门槛明显放低、条数给多、每条只留一句 + 类型·重要度标签，
# 并在开头明说「不一定相关，自己挑」。
# ==========================================================================
CAND_FLAG = PLUGIN_DATA / "CANDIDATES_ON"   # 建这个文件 = 开候选表模式
CAND_N = 8               # 给几条候选
CAND_ITEM_CHARS = 34     # 每条正文截断
CAND_TOTAL_CHARS = 560   # 候选表总预算（比原来 300 宽，因为这是"给模型刷"的）
CAND_GATE = 0.42         # 粗筛门槛：明显放低，向量只负责别漏
CAND_HEADER = "[记忆候选 · 不一定相关，需要哪条自己用，不需要就忽略]"
# 每个池子保底给几条（2026-10-10 修「池子偏置」）
#   症状：三个池混在一起按分数排前 8，结果 8 条**全来自 1213 条的池子**，
#         分数挤在 0.645~0.685 —— 不是因为它更相关，只是因为**它基数大**，
#         样本一多，"最大值偏高"是必然的（纯粹是大数定律，不是相关性）。
#   修法：先给每个池子留位子（保底），剩下的名额再按分数抢。
#         名额不够的池子把剩的让出来，给分数高的池子。
CAND_PER_POOL = 3        # 每个池子保底 3 条 → 3 池共 9 ≥ CAND_N=8，基本每池都有份
# 每个池子送进精排的条数。原来是 _prefetch_candidates 里的局部变量，
# 结果上面的候选宽度取不到它（UnboundLocalError，实测踩过）——提到模块级。
RERANK_PER_POOL = 8
# 精排开关（本地 bge-reranker-base，交叉编码器，严格离线）：
#   离线真考卷实测（15 道手写题 + 指定答案）：正确答案进前 3 —— 60.0% → 73.3%。
#   代价：每轮 prefetch 多约 300ms（实测 230ms → 550ms）。
#   关掉它（False）就退回"纯向量粗筛"，便于对比进退（用户规矩④）。
#   环境变量 TOPICS_RERANK=0/1 可临时覆盖，用于 A/B 对比。
CAND_RERANK = os.environ.get("TOPICS_RERANK", "1") not in ("0", "false", "False", "")

# ---- ② 倒排索引（2026-10-10 加）----
# 补的缺口：向量只认"意思像"，认不出**精确串**——"0.8.14 在哪条""哪个文件被碰过"
# 这类问题，答案明明写在索引里却召不回来。
# 由 pipeline/ 里的倒排脚本产出（纯本地、零模型成本、查询 <0.1ms）。
# 离线基准（8 道精确串提问，答案手工指定）：前 3 命中 6/8。
# 它在候选表里的定位：**精确命中优先**（分数直接置顶、且对话池只留命中项），
# 因为向量对"精确"这件事本来就不擅长，混排只会互相拖累。
# INVERTED 已在文件头的「可选外挂」处定义（可用环境变量 TOPICS_INVERTED 覆盖）
INV_MIN_SCORE = 2.0      # 至少命中这些 IDF 权重才算"够精确"，防一个常用词就置顶
# 与 build_inverted.py 的切词保持一致（那边是契约，改一边必须改另一边）
INV_RE_LATIN = re.compile(r"[A-Za-z0-9_\-\.\\/:@+]{2,}")
INV_RE_CJK = re.compile(r"[\u4e00-\u9fff]{2,}")
INV_STOP = {"the", "and", "for", "with", "this", "that"}


def inv_tokens(text):
    """查询端切词（与 build_inverted.py 的查询端一致：路径拆段、版本号去前导 v）。"""
    t = text or ""
    out = []
    for m in INV_RE_LATIN.finditer(t):
        w = m.group(0).lower().strip("\\/.,:-")
        if len(w) < 2 or w in INV_STOP:
            continue
        out.append("L:" + w)
        if re.fullmatch(r"v\d+(\.\d+)+", w):
            out.append("L:" + w[1:])
        if "/" in w or "\\" in w:
            out.extend("L:" + s for s in re.split(r"[\\/]", w) if len(s) >= 3 and s != w)
    for m in INV_RE_CJK.finditer(t):
        s = m.group(0)
        for i in range(len(s) - 1):
            out.append("C:" + s[i:i + 2])
    return out


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except BaseException:
        pass


# ==========================================================================
# ① 去重键（2026-10-11 修）：从 body[:24] 换成「40 字 + 压掉空白」
# ==========================================================================
# 为什么改（用户列的问题之一）：判断"是不是同一件事"只看正文前 24 个字 ——
#   实测 5785 条里只认得出 8.3% 的重复；同一句话换个开头就漏过去
#   （"命令行装插件要写…" vs "装插件命令：…" 前 24 字不同，于是同一事实的
#   几十个抄本全上场，8 条候选里能占掉 5 条）。
# ⚠ 这只是"门口拦一道"：库里 48% 的孪生是**生成端**造的，根治得去改产池子的脚本。
def _dedup_key(text) -> str:
    return " ".join(str(text or "").split())[:40]


# ---- ⑦ 重点记忆池的读写（2026-10-11）----
IMPORTANT_MAX_INJECT = 12      # 每轮最多带几条（池子还小，先全带；攒多了再改成按相关度取）


def append_important(entry) -> tuple:
    """把一条重点记忆追加进池子。

    安全做法：读 → 改 → **写临时文件再 replace**（os.replace 在 Windows 上是原子的，
    写坏也毁不掉池子）。同一条内容（前 40 字相同）不重复写。
    """
    try:
        IMPORTANT.parent.mkdir(parents=True, exist_ok=True)
        cur = {"meta": {"格式版本": 1, "条数": 0}, "entries": []}
        if IMPORTANT.exists():
            try:
                cur = json.loads(IMPORTANT.read_text(encoding="utf-8"))
            except BaseException:
                pass
        entries = cur.get("entries") or []
        key = _dedup_key(entry.get("内容"))
        for e in entries:
            if _dedup_key(e.get("内容")) == key:
                return False, "池子里已经有同一条了"
        entries.append(entry)
        meta = cur.setdefault("meta", {})
        meta.update({
            "格式版本": 1,
            "条数": len(entries),
            "最后写入": time.strftime("%Y-%m-%d %H:%M:%S"),
            "说明": "重点记忆池：每轮都会带进上下文；每条带来源指针（会话 / msg_id / 当时的索引行）",
        })
        cur["entries"] = entries
        tmp = IMPORTANT.with_name(IMPORTANT.name + ".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
        tmp.replace(IMPORTANT)
        return True, f"池子现有 {len(entries)} 条"
    except BaseException as e:
        return False, repr(e)


def load_important():
    """读重点记忆池（读失败就当空的 —— 绝不能因为它把主流程拖垮）。"""
    try:
        if IMPORTANT.exists():
            return json.loads(IMPORTANT.read_text(encoding="utf-8")).get("entries") or []
    except BaseException:
        pass
    return []


# ==========================================================================
# ⑤ 开环（2026-10-11 加）：推出去的东西到底有没有被用上
# ==========================================================================
# 用户列的问题之一：这套东西只有"推"、没有"回" —— 模型用没用、用了对不对，没人知道。
# 这里做最土的一环：prefetch 时把这一轮推的候选存下来；sync_turn 时看模型的回答里
# 有没有出现候选里的**独特片段**（路径 / 版本号 / 英文标识）—— 出现就算被用上。
# 保守之处：只用拉丁字母 + 数字的片段，不猜中文词 —— 宁可少算，不高估。
OL_STATE = PLUGIN_DATA / "last-candidates.json"
OL_LOG = PLUGIN_DATA / "openloop.log"
_OL_RE = re.compile(r"[A-Za-z]:[\\/]|[A-Za-z][A-Za-z0-9_\-\.]{2,}|\d+\.\d+")
_OL_STOP = {"the", "and", "for", "with", "this", "that", "topics", "memory", "dsh", "hermes"}


def _open_loop_keys(line: str):
    out = []
    for m in _OL_RE.finditer(line or ""):
        w = m.group(0)
        if w.lower() in _OL_STOP or w in out:
            continue
        out.append(w)
    return out[:8]


def save_open_loop(lines) -> None:
    """prefetch 末尾调用：记下这一轮推了什么（供 sync_turn 回头核）。"""
    try:
        state = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "items": [{"line": ln, "keys": _open_loop_keys(ln)} for ln in (lines or [])],
        }
        OL_STATE.parent.mkdir(parents=True, exist_ok=True)
        OL_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except BaseException:
        pass


def check_open_loop(assistant_content) -> str:
    """sync_turn 调用：数这一轮推的候选，有多少条被回答用上了。"""
    try:
        if not OL_STATE.exists():
            return ""
        state = json.loads(OL_STATE.read_text(encoding="utf-8"))
        ans = str(assistant_content or "")
        items = state.get("items") or []
        usable = [it for it in items if it.get("keys")]
        hit, detail = 0, []
        for it in usable:
            keys = it["keys"]
            used = [k for k in keys if k in ans]
            if used:
                hit += 1
                detail.append(f"{it['line'][:12]}…({len(used)}/{len(keys)})")
        rate = (100.0 * hit / len(usable)) if usable else 0.0
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(OL_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] 推 {len(items)} 条（可核 {len(usable)}）→ 回答命中 {hit} 条"
                    f" = {rate:.0f}%    {' | '.join(detail[:5])}\n")
        return f"开环：推 {len(items)} 条 → 命中 {hit} 条（{rate:.0f}%）"
    except BaseException:
        return ""


# ==========================================================================
# ⑥ 「拉」的工具（2026-10-11 加）：让模型自己来查记忆
# ==========================================================================
# 为什么要（用户列的待办之四）：这套东西原来只有「推」——每轮 prefetch 塞 8 条候选，
# 模型不能主动查。而它又是**索引机制的配套安全带**：上下文里老轮次只剩一行摘要，
# 被追问细节时模型只能拿摘要硬答（实测会「编来源」）。有了「拉」，它可以自己去取。
#
# 宿主契约（2026-10-11 用 `__run-script` 探到的**实测结论**，不是猜的）：
#   · `agent.memory_manager.MemoryProvider.get_tool_schemas(self) -> List[Dict]`
#   · `agent.memory_manager.MemoryProvider.handle_tool_call(self, tool_name: str,
#                                                            args: Dict, **kwargs) -> str`
#   · schema 两种写法都认：`{"name","description","parameters"}` 或
#     `{"type":"function","function":{...}}`（宿主里的 `normalize_tool_schema` 负责规整；
#     没有 name 的 schema 会被丢掉并记一条 warning）
#   · `MemoryManager.handle_tool_call` 把 provider 的**返回值原样**交给模型
#     （实测：返回字符串原样出去；provider 抛异常时宿主兜住，给模型的是一句
#      `{"error": "Memory tool 'x' failed: ..."}`）
#   · **门禁**：`inject_memory_provider_tools` 先看 `memory_provider_tools_exposed(agent)`；
#     会话里把 'memory' 工具集禁掉的话，provider 的工具和 system_prompt_block **一起**withheld。
#     本机实测：`memory_provider_tools_enabled(None) → True`；会话里有名为 memory 的工具
#     （我们自己的工具表里就有）→ memory_tool_present=True → 放行。
TOOL_NAME = "search_memory"
TOOL_MAX_PER_TURN = 2          # 防狂查：每轮最多查几次
TOOL_K_DEFAULT = 5             # 默认给几条
TOOL_K_MAX = 8                 # 上限
TOOL_ITEM_CHARS = 120          # 每条正文截断
# 倒排/精确串命中在工具结果里用的分数（与候选表一致：精确命中置顶，不跟向量分比大小）
TOOL_EXACT_SCORE = 2.0
# 全都低于这个分、且没有精确串命中 → 结果里加一句「可能压根没有」的实话
TOOL_BLUNT_SCORE = 0.45
# 精确串（倒排）命中最多先占几个位子（剩下的给向量候选，见 _tool_search 里的实测理由）
TOOL_EXACT_MIN_SLOTS = 2

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "查本机记忆（语义检索 + 精确串）。适合：要某条记忆的细节、"
        "上下文里只剩一行摘要或索引、要找回原话/原文件路径时用它。"
        "返回最像的几条，每条带锚点（库#i / 对话#msg_id）；"
        "拿到锚点再用 session_search(session_id, around_message_id) 取原话。"
        f"每轮最多查 {TOOL_MAX_PER_TURN} 次。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "要查什么：一句话或几个关键词，越具体越好"},
            "k": {"type": "integer",
                  "description": f"要几条，默认 {TOOL_K_DEFAULT}，最多 {TOOL_K_MAX}"},
            "pool": {"type": "string",
                     "description": "限定池子：记忆（存量记忆库）/ 标签（带类型·重要度）/ "
                                    "对话（被裁掉的对话原文索引）；不填 = 三个池都查"},
        },
        "required": ["query"],
    },
}

# ---------------------------------------------------------------------------
# ⑦ 重点记忆池（2026-10-11，用户拍板）：一个**专用**的小池子
# ---------------------------------------------------------------------------
# 目的（用户原话）："可以专门防止实在不小心漏掉那些重要的东西"。
# 两个写入入口：① 模型随时主动写（就是下面这个 save_memory 工具）
#               ② 收尾时自动挑一次（挂在备菜链路里，见 sync_turn）
# 每条都带**来源指针**（会话 + msg_id + 该条的索引行），这样"快照 ↔ 索引 ↔ 原文"
# 三层对得上：拿 msg_id 就能 session_search 取原话，也能回查 message_summaries 的要点。
IMPORTANT = HERMES_HOME / "topics-data" / "mem-important.json"

SAVE_TOOL_NAME = "save_memory"
SAVE_MAX_PER_TURN = 3          # 防滥用：每轮最多写几条
SAVE_CONTENT_CHARS = 300       # 单条上限（它是"重点"，不是原文搬运）

SAVE_TOOL_SCHEMA = {
    "name": SAVE_TOOL_NAME,
    "description": (
        "把「重要、以后绝不能再忘」的一件事写进**重点记忆池**（专用小池，每轮都会提醒我）。"
        "适合：用户明确的偏好/约束、想清楚的结论、踩过的坑、关键数字。"
        "写的时候会带上来源指针（指向当时的索引行与原文 #id），便于回溯。"
        f"只写真的重要的；每轮最多写 {SAVE_MAX_PER_TURN} 条。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的那件事，一两句说清"},
            "reason": {"type": "string", "description": "为什么值得单独记（可选）"},
            "importance": {"type": "integer", "description": "1-5，默认 3"},
            "msg_id": {"type": "integer",
                       "description": "这件事出自哪条消息的 #id（可选；给了就能精确回指原文与索引）"},
        },
        "required": ["content"],
    },
}

_TOOL_SCHEMA_CALLS = 0      # get_tool_schemas 被叫了几次（第 2 次起 = 工具真发给了模型）


def tool_error_text(msg: str) -> str:
    """工具失败时的返回：一句人话 + 结构化字段，绝不抛异常。"""
    return json.dumps({"结果": [], "说明": msg, "error": True}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 索引解析缓存（2026-10-10）
#
# 为什么加：实测一次桌面版启动里有 **3 个会话各自 initialize**，每个都把
# 25MB 的标签池重新解析一遍（其中一个用了 1014 ms）—— 等于 3 份内存 + 3 份耗时。
# 索引文件是只读的，按 (路径, 大小, mtime) 缓存一份就够，多会话共享同一份对象。
# 文件一变，key 就变，自动重新解析（不用重启进程）。
#
# 注意：缓存的对象是**共享**的，调用方只许读、不许改（现在所有用到的地方都只读）。
# ---------------------------------------------------------------------------
_INDEX_CACHE = {}
# 预热只做一次：实测一个桌面版启动里有 **3 个会话各建一个 provider 实例**，三个实例同时预热
# 会各自 `node embed-server` 一次（实测 pid 4164/19028/10056 三个）——后两个抢不到端口才退出，
# 但那一下等于把 1.8GB 的模型加载了三遍。锁 + 就绪标志把并发收敛成一次。
_WARM_LOCK = None
_SERVICE_READY = False


def _np():
    """惰性导入 numpy。为什么值得：实测 5785 条通道打分，
    纯 Python 循环 120ms → numpy 矩阵乘 6.2ms（19 倍），前 8 名分数完全一致。
    没有 numpy 就退回原来的循环（功能不变，只是慢）。"""
    global _NP
    if _NP is None:
        try:
            import numpy
            _NP = numpy
        except BaseException:
            _NP = False
    return _NP or None


_NP = None


def make_mat(docs):
    """把一批 dict 里的 vec 堆成 numpy 矩阵（无 numpy 返回 None）。"""
    np = _np()
    if np is None or not docs:
        return None
    try:
        return np.asarray([d["vec"] for d in docs], dtype=np.float32)
    except BaseException:
        return None


def topk_idx(mat, qv, k):
    """矩阵乘取前 k 名，返回 [(分数, 下标)] 降序。"""
    np = _np()
    s = mat @ np.asarray(qv, dtype=np.float32)
    k = min(k, s.shape[0])
    idx = np.argpartition(-s, k - 1)[:k]
    return sorted(((float(s[i]), int(i)) for i in idx), reverse=True)


def load_index(path: Path):
    """解析 JSON 并按文件状态缓存。返回 (对象, 是否命中缓存)。"""
    try:
        st = path.stat()
        key = (st.st_size, int(st.st_mtime))
    except BaseException:
        return json.loads(path.read_text(encoding="utf-8")), False
    hit = _INDEX_CACHE.get(str(path))
    if hit is not None and hit[0] == key:
        return hit[1], True
    obj = json.loads(path.read_text(encoding="utf-8"))
    _INDEX_CACHE[str(path)] = (key, obj)
    return obj, False


def _http_get(path: str, timeout: float = 0.6):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except BaseException:
        return None


class TopicsProvider:
    """记忆后端。名字、可用性、初始化、工具表 —— 这四个是 ABC 要求的，其余可选。"""

    def __init__(self, **kwargs):
        self._lib = None
        self._mat = None
        self._topics = []
        self._docs = []
        # 第二个池：被滚动压缩裁掉的对话原文（离线索引，见 build_conv_index_v2.py）
        self._conv_docs = []
        self._conv_mat = None
        self._conv_mat_np = None
        self._mat_np = None
        # 标签池（多通道：主题/要点/原文，每条带类型与重要度）—— 候选表模式用
        self._label_docs = []
        self._label_mat_np = None
        # 倒排（② 精确串召回）：postings 与 idf
        self._inv = None
        self._inv_idf = {}
        self._conv_by_id = {}
        self._session = None
        self._spawned = None
        self._stats = {"prefetch": 0, "命中": 0, "注入字数合计": 0, "关键词降级": 0,
                       "记忆池命中": 0, "对话池命中": 0, "工具查询": 0}
        # ⑥ 「拉」：本轮已经查了几次（prefetch / sync_turn 时清零 —— 见 handle_tool_call）
        self._tool_calls_turn = 0
        # ⑦ 重点记忆：本轮写了几条 + 池子缓存（写完置 None → 下一轮重读）
        self._save_calls_turn = 0
        self._important = None
        log("TopicsProvider 构造")

    # ---- ABC 必需 ----
    @property
    def name(self) -> str:
        return "topics"

    def is_available(self) -> bool:
        return DATA.exists()

    def get_tool_schemas(self):
        """⑥ 「拉」：把 search_memory 报给宿主。

        宿主问两次：注册时问一次（进路由表），**每次组装请求时再问一次**
        （`MemoryManager.get_all_tool_schemas` ← `inject_memory_provider_tools`，
        且那一步前面有「memory 工具集没被禁掉」的门禁）。
        所以第 2 次被调用就等于「工具真的发到模型眼前了」—— 这里记一笔，事后能对证。
        """
        global _TOOL_SCHEMA_CALLS
        _TOOL_SCHEMA_CALLS += 1
        if _TOOL_SCHEMA_CALLS == 2:
            log("get_tool_schemas 第 2 次被调用 → 工具确实发给了模型"
                "（不只是注册时问过一次；若只有 1 次=被门禁挡住了）")
        return [TOOL_SCHEMA, SAVE_TOOL_SCHEMA]

    # ---- 生命周期 ----
    def initialize(self, session_id=None, **kwargs):
        self._session = session_id
        t0 = time.time()
        try:
            raw, c1 = load_index(DATA)
            self._topics = raw["topics"]
            docs = raw["docs"]
            self._docs = docs
            # 把所有向量摊平成一个 float 数组：621k 个数 ≈ 2.5 MB，
            # 比 1213 个 Python list 省得多，点积也快。
            mat = array.array("f")
            for d in docs:
                mat.extend(d["vec"])
            self._mat = mat
            self._mat_np = make_mat(docs)      # numpy 矩阵（打分快 19 倍；没有 numpy 则 None）
            log(f"initialize(session={session_id}) 载入 {len(docs)} 条 / "
                f"{len(self._topics)} 主题，用时 {(time.time()-t0)*1000:.0f} ms"
                f"{'（记忆池命中缓存）' if c1 else '（记忆池首次解析）'}")
            # ---- 第二个池：被裁掉的对话原文（可选，没有就不启用这一池）----
            try:
                if CONV.exists():
                    craw, c2 = load_index(CONV)
                    cdocs = craw.get("docs") or []
                    cmat = array.array("f")
                    for d in cdocs:
                        cmat.extend(d["vec"])
                    self._conv_docs = cdocs
                    self._conv_mat = cmat
                    self._conv_mat_np = make_mat(cdocs)
                    log(f"  对话池：{len(cdocs)} 条被裁掉的原文（{CONV.name}）"
                        f"{'，命中缓存' if c2 else '，首次解析'}")
                else:
                    log(f"  对话池：没有 {CONV.name}，本轮只检索记忆库")
            except BaseException:
                log("  对话池载入失败（只用记忆库）：\n" + traceback.format_exc())
            # ---- 标签池：带类型/重要度的多通道条目（候选表模式用它）----
            try:
                if LABELS.exists():
                    lraw, c3 = load_index(LABELS)
                    self._label_docs = lraw.get("entries") or []
                    self._label_mat_np = make_mat(self._label_docs)
                    log(f"  标签池：{len(self._label_docs)} 个通道向量"
                        f"（主题/要点，带类型与重要度）"
                        f"{'，命中缓存' if c3 else '，首次解析'}")
                else:
                    log(f"  标签池：没有 {LABELS.name}，候选表只能给无标签的条目")
            except BaseException:
                log("  标签池载入失败：\n" + traceback.format_exc())
            # ---- ② 倒排：精确串召回（可选）----
            try:
                if INVERTED.exists():
                    iraw, c4 = load_index(INVERTED)
                    post = iraw.get("postings") or {}
                    n = iraw.get("meta", {}).get("条数") or 1
                    self._inv = post
                    self._inv_idf = {k: math.log(1 + n / (1 + len(v))) for k, v in post.items()}
                    # 池子文档按 msg_id 建索引，倒排命中直接取正文（不再算向量）
                    self._conv_by_id = {str(d.get("msg_id")): d for d in self._conv_docs}
                    log(f"  倒排：{len(post):,} 个词元（{INVERTED.name}）"
                        f"{'，命中缓存' if c4 else '，首次解析'}")
                else:
                    log(f"  倒排：没有 {INVERTED.name}，本轮精确串召回落空")
            except BaseException:
                log("  倒排载入失败（精确串召回不生效）：\n" + traceback.format_exc())
        except BaseException:
            log("initialize 失败（插件静默失效，不影响对话）：\n" + traceback.format_exc())
            self._lib = None
        # 预热：把"冷启动"从第一轮挪到初始化之后（细节见 _warm_up 的注释）
        try:
            import threading
            threading.Thread(target=self._warm_up, name="topics-warmup", daemon=True).start()
        except BaseException:
            pass

    def _warm_up(self):
        """后台预热向量服务 + 精排模型。

        ★ 2026-10-10 实测加：冷启动那一轮 prefetch 要 **4588~5092 ms**（服务刚拉起、首次
        embed 和首次 rerank 都要把模型从磁盘读进来），而宿主给 prefetch 的超时是 **8 秒**
        —— agent.log 里已经有 15 次 `prefetch timed out after 8.0s`，那几轮**一个候选都没给**。
        预热把这段开销挪到会话初始化之后（这时候模型还没开始说话），实测热态只要 765~811 ms。
        线程是 daemon，失败也只在插件日志里留一行，绝不影响对话。
        """
        try:
            t0 = time.time()
            global _SERVICE_READY, _WARM_LOCK
            import threading as _th
            if _WARM_LOCK is None:
                _WARM_LOCK = _th.Lock()
            with _WARM_LOCK:
                if _SERVICE_READY and self._health_ok():
                    log("预热：服务已就绪，跳过（同一进程里已有会话预热过）")
                    return
                warm = self._health_ok()
                if not warm:
                    self._start_service()
                for _ in range(50):                 # 最多等 10 秒
                    if self._post_vector("预热") is not None:
                        break
                    time.sleep(0.2)
                # 精排是另外一个模型，也让它先加载一次
                try:
                    self._rerank("预热", [{"text": "预热"}])
                except BaseException:
                    pass
                _SERVICE_READY = True
            log(f"预热完成（向量服务{'本就在' if warm else '新拉起'} + 精排模型），"
                f"用时 {(time.time()-t0)*1000:.0f} ms")
        except BaseException:
            log("预热失败（不影响功能）：\n" + traceback.format_exc())

    def shutdown(self, **kwargs):
        log(f"shutdown  统计={self._stats}")
        # 服务自己有空闲退出，这里不强杀（别的会话可能还在用）

    # ---- 每轮：定位主题 → 收窄取回 ----
    def prefetch(self, query=None, **kwargs) -> str:
        self._stats["prefetch"] += 1
        self._tool_calls_turn = 0      # ⑥ 「拉」：新的一轮 → 工具配额重置
        self._save_calls_turn = 0      # ⑦ 同上（写入配额）
        try:
            body = self._prefetch(query or "")
            block = self._important_block()   # ⑦ 重点记忆：**无条件**带上，绝不能漏
            if block and body:
                return block + "\n" + body
            return block or body
        except BaseException:
            log("prefetch 出错（已兜住，本轮不注入）：\n" + traceback.format_exc())
            return ""

    def _important_block(self) -> str:
        """⑦ 重点记忆块。

        用户要的是"专门防止实在不小心漏掉那些重要的东西" —— 所以这一块**不看相关性、
        不看门槛、每轮都带**。只有条数太多时才按重要度截断（那时候该改设计，不是悄悄丢）。
        """
        try:
            ents = self._important if self._important is not None else load_important()
            self._important = ents
            if not ents:
                return ""
            ents = sorted(ents, key=lambda e: -(e.get("重要度") or 3))[:IMPORTANT_MAX_INJECT]
            out = ["[重点记忆 · 每轮都在 · 绝不再漏]"]
            for e in ents:
                src = e.get("来源") or {}
                anchor = src.get("原文锚") or (f"#{src['msg_id']}" if src.get("msg_id") else "")
                tail = f"｜{e.get('时间', '')} {anchor}".rstrip()
                out.append(f"- {e.get('内容', '')}{tail}")
            if len(self._important) > len(ents):
                out.append(f"（共 {len(self._important)} 条，这里按重要度取了前 {len(ents)} 条）")
            return "\n".join(out)
        except BaseException:
            log("重点记忆块出错（已兜住，不影响本轮其余注入）：\n" + traceback.format_exc())
            return ""

    def _prefetch(self, query: str) -> str:
        if not query.strip() or self._mat is None or not self._docs:
            return ""
        # 候选表模式：分工反转 —— 向量只粗筛，模型自己刷（见文件头的说明）
        if CAND_FLAG.exists():
            return self._prefetch_candidates(query)
        t0 = time.time()
        qv = self._embed(query)
        route_ms = 0.0

        if qv is not None:
            # 与主题质心比，取最近的 ROUTE_TOPICS 个
            tr = time.time()
            scored = sorted(self._topics, key=lambda t: -self._dot(qv, t["centroid"]))
            allowed = {t["id"] for t in scored[:ROUTE_TOPICS]}
            route_ms = (time.time() - tr) * 1000
            src = "向量"
        else:
            # 降级：关键词路由（精度会掉，但不崩）
            self._stats["关键词降级"] += 1
            allowed = set()
            for t in self._topics:
                if any(k and k in query for k in t.get("keywords", [])):
                    allowed.add(t["id"])
            src = "关键词降级"
            qv = None

        if not allowed:
            log(f"prefetch 无主题命中（{src}），零注入")
            return ""

        # ---- 两个池一起找，但**门槛各用各的** ----
        # 为什么门槛要分开（2026-10-10 实测）：
        #   0.65 这个门槛当初是在**记忆库**上标定的（记忆条目是精炼的自洽短句）。
        #   对话池里是长篇解释的切片，同一件事换种问法就只有 0.54~0.61。
        #   实测对话池的真匹配集中在 0.539~0.606，而无关对照（"今天晚饭吃什么"）最高才 0.319
        #   —— 两者分离得很干净，只是不能共用同一个数。
        #   所以：记忆库 0.65（保持原样，不动已验证的基线），对话池 0.52。
        gate_of = {"记忆": GATE, "对话": GATE_CONV}
        cands = []          # (分数, 来源标签, 文本)
        for tag, docs in (("记忆", self._docs), ("对话", self._conv_docs)):
            if not docs:
                continue
            g = gate_of[tag]
            for d in docs:
                if d["topic"] not in allowed:
                    continue
                t = (d.get("text") or "").strip()
                if not t:
                    continue
                s = self._dot(qv, d["vec"]) if qv is not None else 0.0
                if qv is not None and s < g:
                    continue
                cands.append((s, tag, t))
        if not cands:
            self._stats["被门槛挡住"] = self._stats.get("被门槛挡住", 0) + 1
            log(f"prefetch 收窄后两个池都没过门槛（记忆 {GATE} / 对话 {GATE_CONV}），零注入"
                f"（{query[:24]!r}）")
            return ""
        cands.sort(key=lambda x: -x[0])
        best_score = cands[0][0]

        picked = []
        used = 0
        pool_n = {"记忆": 0, "对话": 0}
        for _, tag, t in cands:
            if len(picked) >= TOP_N:
                break
            prefix = f"【{tag}】"
            # 截断要把前缀算进 100 字里 —— 第一版先截 100 再加 4 字前缀，结果每条 104 字
            t = t[:max(0, PER_ITEM_CHARS - len(prefix))]
            # 预算要把前缀、"- "、换行都算进去
            cost = len(prefix) + len(t) + 3
            if used + cost > TOTAL_CHARS:
                room = TOTAL_CHARS - used - 3 - len(prefix)
                t = t[:max(0, room)]
                cost = len(prefix) + len(t) + 3
            if not t:
                continue
            picked.append(prefix + t)
            used += cost
            pool_n[tag] += 1
        if not picked:
            return ""

        self._stats["命中"] += 1
        self._stats["注入字数合计"] += used
        if pool_n["记忆"]:
            self._stats["记忆池命中"] += 1
        if pool_n["对话"]:
            self._stats["对话池命中"] += 1
        body = "\n".join(f"- {p}" for p in picked)
        total_ms = (time.time() - t0) * 1000
        # 打印**字符串真实长度**，不要用记账值 —— 记账是保守算法（每条约 3 字前缀+换行），
        # 最后一条没有尾换行，会多算 1 字（实测日志出现过"301 字"，实际 299）。
        log(f"prefetch 注入 {len(picked)} 条 / {len(body)} 字"
            f"（记忆 {pool_n['记忆']} + 对话 {pool_n['对话']}；{src}，最高相似度 {best_score:.3f}，"
            f"路由 {route_ms:.1f} ms，合计 {total_ms:.1f} ms，主题 {sorted(allowed)}）")
        return body

    def _prefetch_candidates(self, query: str) -> str:
        """候选表模式：向量只做粗筛，把一批候选交给模型自己刷。

        为什么门槛敢放这么低（0.42）：因为**裁决权不在标量手里**了。
        之前门槛怎么调都不对，是因为让一个标量做最终裁决；
        现在标量只负责「别漏」，「别错」交给模型 —— 它看得到上下文和世界知识。

        注意：这一模式**不按主题收窄**（那是上一版的省事做法）。
        收窄会漏（实测路由天花板只有 37.1%），而粗筛阶段的漏是补不回来的。
        """
        t0 = time.time()
        qv = self._embed(query)
        if qv is None:
            return ""

        # 三个池子**分开装**（这是修池子偏置的关键：合在一起排就是比谁基数大）
        # 对话池的正文 = 索引（v2 索引的门面就是模型生成的一行，向量也按它算）。
        # 为什么不能再截到 34 字：索引本来就只有几十字，再截就把它砍断了。
        #
        # 打分方式：有 numpy 就用矩阵乘（实测 5785 条 120ms → 6.2ms，前 8 名分数一致）；
        # 没有就退回原来的逐条点积（功能不变，只是慢）。取候选宽度与精排宽度一致——
        # 反正后面每池只喂 RERANK_PER_POOL 条给精排。
        WIDE = max(RERANK_PER_POOL, CAND_PER_POOL) * 3

        def score(docs, mat_np):
            if mat_np is not None:
                return topk_idx(mat_np, qv, WIDE)
            return sorted(((self._dot(qv, d["vec"]), i) for i, d in enumerate(docs)),
                          reverse=True)[:WIDE]

        mem_sc = score(self._docs, self._mat_np)
        lab_sc = score(self._label_docs, self._label_mat_np)
        conv_sc = score(self._conv_docs, self._conv_mat_np)

        # ④ 时间的标签（日期·第几轮·会话），三条池子共用一套构造
        def conv_label(d):
            if not d.get("_关键"):
                return None
            return ("要点", d.get("_轮"), d.get("session_id", ""), d.get("_时间") or "")

        pools = [
            ("记忆", [(s, (self._docs[i].get("text") or "").strip(), None) for s, i in mem_sc]),
            ("标签", [(s, (self._label_docs[i].get("引用") or "").strip(),
                       self._label_docs[i].get("标签") or {}) for s, i in lab_sc]),
            ("对话", [(s, (self._conv_docs[i].get("text") or "").strip(), conv_label(self._conv_docs[i]))
                     for s, i in conv_sc]),
        ]

        # ---- ② 精确串召回：倒排命中直接**置顶**对话池 ----
        # 理由：向量不擅长"精确"，把精确命中和语义候选混在一起排会互相拖累
        # （实测过：多通道取最高分导致间隔 −0.006 重叠）。所以精确命中单独占位。
        inv_hits = self._inv_search(query) if self._inv else []
        if inv_hits:
            exact = [(2.0, (d.get("text") or "").strip(), conv_label(d))
                     for _, d in inv_hits if d]
            names = [n for n, _ in pools]
            li = names.index("对话")
            pools[li] = ("对话", exact)          # 只留精确命中，本轮对话池由它说话
            log(f"  倒排命中 {len(exact)} 条：{', '.join('#' + str(d.get('msg_id')) for _, d in inv_hits[:6])}")

        pool_n = {}     # 每池过门槛的条数（记账用）
        per_pool = []   # [(池名, [(分数, 正文, 标签), ...] 按分数降序、已去重)]
        for name, rows in pools:
            hi = [r for r in rows if r[0] >= CAND_GATE and r[1]]
            pool_n[name] = len(hi)
            hi.sort(key=lambda x: -x[0])
            seen_p, uniq = set(), []
            for s, body, lb in hi:                 # 池内先去重，别让配额浪费在重复上
                key = body[:24]
                if key in seen_p:
                    continue
                seen_p.add(key)
                uniq.append((s, body, lb))
            per_pool.append((name, uniq))

        if not any(u for _, u in per_pool):
            log("候选表：粗筛后一条都没有，零注入")
            return ""

        # ---- 精排（2026-10-10 实测后接入）----
        # 为什么加：粗筛是"宁可多给"的宽门槛，前几名常被"分数高但其实不相关"的挤掉。
        # 离线真考卷（15 道手写问题 + 指定答案）实测：
        #   正确答案进前 3 —— 纯向量 60.0% → 加精排 73.3%；进前 8 —— 86.7% → 93.3%。
        # 用本地 bge-reranker-base（交叉编码器，严格离线）。它慢（约 1.3 秒/条 × 候选数），
        # 所以只精排**每池前 RERANK_PER_POOL 条**：实测这个宽度足够把对的顶上来，又不会拖慢每一轮。
        if CAND_RERANK:
            for pi, (name, uniq) in enumerate(per_pool):
                # ② 倒排命中的那批**不精排**：它们已经是"精确命中"，精排只会重排同义词、
                #    白花约 110ms/轮（实测每轮中位 370ms，三池精排是大头）。
                #    （用户要求过：调取由脚本干、精确命中优先，模型只看结果。）
                if name == "对话" and inv_hits:
                    continue
                head = uniq[:RERANK_PER_POOL]
                if len(head) < 2:
                    continue
                order = self._rerank(query, [b for _, b, _ in head])
                if order is None:
                    continue
                reranked = [head[k] for k in order]
                per_pool[pi] = (name, reranked + uniq[RERANK_PER_POOL:])

        # 第一步：每个池子按配额保底拿前几条
        taken, pool_kept = set(), {name: 0 for name, _ in per_pool}
        order = []                                  # [(分数, 正文, 标签, 池名)]
        for name, uniq in per_pool:
            for s, body, lb in uniq[:CAND_PER_POOL]:
                taken.add(_dedup_key(body))
                pool_kept[name] += 1
                order.append((s, body, lb, name))

        # 第二步：剩下没填满的名额 → **按池轮转**，不再跨池比分数。
        # 为什么改（2026-10-11，用户点的问题之二）：三个池子的分数不可比 ——
        #   池子越大，"最大值"天然越高（大数定律，不是相关性）。第一步的保底只治得了
        #   "整池缺席"，治不了"剩余名额被大池吃光"。改成每池轮流点当前最好的一条
        #   （**池内**仍按分数 —— 同一把尺子，那是可比的）。
        rest_lists = [[r for r in uniq if _dedup_key(r[1]) not in taken] for _, uniq in per_pool]
        while len(order) < CAND_N:
            progressed = False
            for pi, lst in enumerate(rest_lists):
                if not lst:
                    continue
                s, body, lb = lst.pop(0)
                key = _dedup_key(body)
                if key in taken:
                    continue
                taken.add(key)
                order.append((s, body, lb, per_pool[pi][0]))
                progressed = True
                if len(order) >= CAND_N:
                    break
            if not progressed:
                break

        # （删掉了原来的"最终按分数降序"：那是跨池比分，正是本步要治的病。
        #   现在顺序 = 保底分组 + 轮转补位，池与池之间不再比大小。）

        picked = []
        sel_pool = {name: 0 for name, _ in per_pool}    # 记账：**最终入选**的分池计数
        used = len(CAND_HEADER) + 1
        for s, body, lb, name in order:
            extra = ""
            is_conv = isinstance(lb, tuple) and lb and lb[0] == "要点"
            if isinstance(lb, dict):
                ty = "/".join(lb.get("类型") or [])[:12]
                imp = lb.get("重要度")
                if ty and imp:
                    extra = f"｜{ty}·{imp}"
                elif imp:
                    extra = f"｜{imp}"
            elif is_conv:
                # 被裁对话池：正文本来就已经是「模型生成的一行索引」。
                # 标出来既让模型知道"这是压缩版、不是原文"，也把**对话顺序**带上
                # （用户要求：索引要排好顺序，不能让模型不知道对话顺序）——
                # 日期 + 会话后 6 位 + 第几轮，跨会话也能排出先后；要原文用 session_search 取。
                sess = str(lb[2] if len(lb) > 2 else "")[-6:]
                rn = lb[1] if len(lb) > 1 else None
                tm = (lb[3] if len(lb) > 3 else "") or ""
                bits = [x for x in (tm, f"第{rn}轮" if rn else "", sess) if x]
                extra = "｜压缩记忆·" + "·".join(bits) if bits else "｜压缩记忆"
            # 被裁对话池的条目不放 34 字截断：它的正文里现在带着「｜定位：…」，
            # 截在定位串之前等于白补（实测：补了特征还是答不出"文件在哪"）。
            cap = len(body) if is_conv else CAND_ITEM_CHARS
            line = f"{len(picked)+1}) {' '.join(body.split())[:cap]}{extra}"
            cost = len(line) + 1
            if used + cost > CAND_TOTAL_CHARS:
                break
            picked.append(line)
            sel_pool[name] = sel_pool.get(name, 0) + 1
            used += cost
            if len(picked) >= CAND_N:
                break
        if not picked:
            return ""

        out = CAND_HEADER + "\n" + "\n".join(picked)
        self._stats["候选表命中"] = self._stats.get("候选表命中", 0) + 1
        self._stats["注入字数合计"] += len(out)
        # 记账修正（2026-10-11）：日志原来记的是**保底阶段**取了几个（pool_kept），
        #   跟"最终给出去几条、分别来自哪池"对不上 —— 实测出现过
        #   "实际给 8 条（记忆 3 + 标签 3 + 对话 3）"这种 3+3+3=9 的自相矛盾。
        #   现在记 sel_pool = 真正写进候选表的那几条。
        # （2026-10-10 那版修的方向是对的——三条池子都要计数——但取数取错了阶段。）
        if sel_pool.get("记忆"):
            self._stats["记忆池命中"] += 1
        if sel_pool.get("对话"):
            self._stats["对话池命中"] += 1
        if sel_pool.get("标签"):
            self._stats["标签池命中"] = self._stats.get("标签池命中", 0) + 1
        log(f"候选表：粗筛门槛 {CAND_GATE} → 候选池 " +
            " + ".join(f"{k} {v}" for k, v in pool_n.items()) +
            f" 条；实际给 {len(picked)} 条（" +
            " + ".join(f"{k} {v}" for k, v in sel_pool.items() if v) +
            f"，按池轮转补），{len(out)} 字，用时 {(time.time()-t0)*1000:.0f} ms")
        save_open_loop(picked)      # ⑤ 开环：记下这一轮推了什么，等 sync_turn 回头核
        return out

    # ---- 其余钩子：这一步都返回空，保证不干扰 ----
    def system_prompt_block(self, **kwargs) -> str:
        return ""

    def sync_turn(self, user_content=None, assistant_content=None, *, session_id="", messages=None):
        """整段对话写完的那一刻，宿主叫我们一次。

        ⚠ 签名必须跟 memory 插件基类一致（显式形参，不要 *a/**kw）：
        宿主有个 `_provider_sync_accepts_messages` 会 inspect 签名来决定怎么调，
        2026-10-11 实测：写成 (*a, **kw) 之后这个钩子**再也不被调用了**（日志里一行都没有）。

        做的事：起一趟短命进程去备菜，**立刻返回**（不阻塞宿主）。
          · 不起常驻循环、不用定时器：原来"每 30 秒扫、不管说完没"的方案已被用户否掉。
          · 只传会话 id，让它只处理刚结束的这个会话。
          · 起不来也只是不备菜，绝不让钩子把宿主拖下水。
        """
        try:
            import os as _os
            import subprocess as _sp
            log("sync_turn 被调用（session=%s 用户%s字 回答%s字）" % (
                session_id,
                len(user_content or ""), len(assistant_content or "")))
            _ol = check_open_loop(assistant_content)      # ⑤ 开环：这一轮推的用上了没
            if _ol:
                log(_ol)
            self._tool_calls_turn = 0      # ⑥ 「拉」：这一轮结束 → 工具配额重置
            script = str(RUN_FOREVER)
            cmd = [
                "powershell.exe", "-NoProfile",
                "-ExecutionPolicy", "Bypass", "-File", script,
                "-Once", "-Sid", str(session_id or ""),
            ]
            env = dict(_os.environ)
            env.setdefault("HERMES_HOME", str(HERMES_HOME))
            env["PYTHONIOENCODING"] = "utf-8"
            # ⚠ 起法实测（2026-10-11，四种组合都试过）：
            #   DETACHED_PROCESS + CREATE_NO_WINDOW + `-WindowStyle Hidden`
            #   → **进程起来了、退出码 0，但脚本一步都没跑**（没有日志、没有标记文件）。
            #      这是最坑的一种失败：宿主看不到任何异常，我们也没日志。
            #   只留 CREATE_NO_WINDOW（它本身就是无窗口启动，不需要再 Hidden）
            #   → 实测正常跑完整趟。
            flags = 0
            for name in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP"):
                flags |= getattr(_sp, name, 0)
            _sp.Popen(cmd, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                      stdin=_sp.DEVNULL, env=env, creationflags=flags, close_fds=True)
        except BaseException as e:
            try:
                log("sync_turn：起备菜进程失败（不影响对话）：%r" % (e,))
            except BaseException:
                pass
        return None

    def on_pre_compress(self, messages=None, **kwargs) -> str:
        return ""

    # ---- ⑥ 「拉」：模型自己来查记忆 ----
    # ---- ⑦ 重点记忆：模型主动写 ----
    def _tool_save(self, a: dict) -> str:
        """写一条重点记忆。补全来源指针（会话/msg_id/该条的索引行）→ 算向量 → 追加进池。

        来源指针是用户明确要求的："这些快照最好能指向对应的索引和原文" —— 所以：
          · msg_id 优先用模型给的（它知道自己在说哪条）；
          · 没给就用本会话最近一条 user/assistant 的 id（有个锚总比没有强）；
          · 拿到 msg_id 后回查 message_summaries，把**当时的索引行**一起存进去。
        """
        try:
            content = " ".join(str(a.get("content") or a.get("内容") or "").split())
            if len(content) < 4:
                return tool_error_text("要记的内容太短了 —— 一两句说清那件事。")
            content = content[:SAVE_CONTENT_CHARS]
            if self._save_calls_turn >= SAVE_MAX_PER_TURN:
                return tool_error_text(
                    f"本轮已经写过 {self._save_calls_turn} 条（上限 {SAVE_MAX_PER_TURN}）——"
                    "先用手上的信息，下一轮再写。")
            self._save_calls_turn += 1

            reason = " ".join(str(a.get("reason") or a.get("理由") or "").split())[:200]
            try:
                imp = int(a.get("importance") or a.get("重要度") or 3)
            except BaseException:
                imp = 3
            imp = max(1, min(imp, 5))

            mid = a.get("msg_id")
            try:
                mid = int(mid) if mid is not None else None
            except BaseException:
                mid = None
            if mid is None:
                mid = self._latest_msg_id()
            src = {"会话": self._session or ""}
            if mid:
                src["msg_id"] = mid
                src["原文锚"] = f"#{mid}"
                s = self._summary_of(mid)
                if s:
                    src["索引"] = s

            vec = self._embed(content)
            entry = {
                "id": "imp-" + time.strftime("%Y%m%d%H%M%S"),
                "时间": time.strftime("%Y-%m-%d %H:%M:%S"),
                "内容": content,
                "理由": reason,
                "重要度": imp,
                "来源": src,
                "vec": [round(float(x), 4) for x in vec] if vec else [],
            }
            ok, msg = append_important(entry)
            log(f"重点记忆{'已写入' if ok else '未写入'}：{content[:40]}｜来源 {src}｜{msg}")
            if not ok:
                return tool_error_text(f"没能写进池子：{msg}")
            self._important = None      # 让下一轮重新读池（新条目立刻能被带上）
            return json.dumps({
                "结果": "已写入重点记忆池",
                "内容": content,
                "重要度": imp,
                "来源": src,
                "怎么取原文": (f"session_search(session_id='{src.get('会话', '')}', "
                               f"around_message_id={mid})"),
            }, ensure_ascii=False)
        except BaseException:
            log("save_memory 出错（已兜住）：\n" + traceback.format_exc())
            return tool_error_text("写重点记忆时内部出错了，这次没写进去 —— 继续手上的事。")

    def _latest_msg_id(self):
        """本会话最近一条 user/assistant 消息的 id（当来源锚用）。"""
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{HERMES_HOME / 'state.db'}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "SELECT id FROM messages WHERE session_id=? AND role IN ('user','assistant') "
                    "ORDER BY id DESC LIMIT 1", (self._session or "",)).fetchone()
                return int(row[0]) if row else None
            finally:
                con.close()
        except BaseException:
            return None

    def _summary_of(self, mid):
        """某条消息当时的索引行（message_summaries 里的要点）。"""
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{HERMES_HOME / 'state.db'}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "SELECT summary FROM message_summaries WHERE msg_id=? AND superseded_by IS NULL "
                    "LIMIT 1", (int(mid),)).fetchone()
                return (row[0] or "").strip() if row else ""
            finally:
                con.close()
        except BaseException:
            return ""

    def _tool_search(self, query: str, k: int, pool: str = ""):
        """三个池各取前 k 条，合并去重。

        返回 (行列表, 出错原因)。行 = (分数, 池名, 正文, 锚点, 备注)。
        规矩与候选表一致：**精确串（倒排）命中置顶**，不跟向量分混着比大小 ——
        向量不擅长「精确」，混排只会互相拖累。
        """
        qv = self._embed(query)
        if qv is None:
            return [], "向量服务不可用（本轮不查）"

        def topk(docs, mat, kk):
            if not docs:
                return []
            if mat is not None:
                return topk_idx(mat, qv, kk)
            return sorted(((self._dot(qv, d["vec"]), i) for i, d in enumerate(docs)),
                          reverse=True)[:kk]

        rows = []
        if pool in ("", "对话") and self._inv:
            # 精确串最多占几个位子（不是全占）：实测「随便写点什么完全无关的东西」这种
            # 日常中文也会撞上好几个 ID 值够高的二字切片，把 5 个位子全吃掉 ——
            # 那结果里就只剩「词撞上了」，一条真语义候选都没有。留位子给向量。
            exact_cap = max(TOOL_EXACT_MIN_SLOTS, k // 2)
            for _, d in self._inv_search(query, topk=min(k, exact_cap)):
                sid = str(d.get("session_id") or "")
                rows.append((TOOL_EXACT_SCORE, "对话", (d.get("text") or "").strip(),
                             f"对话#{d.get('msg_id')}",
                             f"{sid[-6:]}·第{d.get('_轮')}轮·精确串"))
        if pool in ("", "记忆") and self._docs:
            for s, i in topk(self._docs, self._mat_np, k):
                d = self._docs[i]
                rows.append((s, "记忆", (d.get("text") or "").strip(),
                             f"库#{d.get('i')}", ""))
        if pool in ("", "标签") and self._label_docs:
            for s, i in topk(self._label_docs, self._label_mat_np, k):
                d = self._label_docs[i]
                lb = d.get("标签") or {}
                ty = "/".join(lb.get("类型") or [])[:12]
                imp = str(lb.get("重要度") or "")
                tail = "·".join(x for x in (d.get("通道") or "", ty, imp) if x)
                rows.append((s, "标签", (d.get("引用") or "").strip(),
                             f"库#{d.get('idx')}", tail))
        if pool in ("", "对话") and self._conv_docs:
            for s, i in topk(self._conv_docs, self._conv_mat_np, k):
                d = self._conv_docs[i]
                sid = str(d.get("session_id") or "")
                rows.append((s, "对话", (d.get("text") or "").strip(),
                             f"对话#{d.get('msg_id')}",
                             f"{sid[-6:]}·第{d.get('_轮')}轮"))
        # 去重：三个池常撞同一件事（同一句话换个开头就进来了——见 ① 去重键的注释）
        seen, uniq = set(), []
        for row in sorted(rows, key=lambda r: -r[0]):
            key = _dedup_key(row[2])
            if not row[2] or key in seen:
                continue
            seen.add(key)
            uniq.append(row)
        return uniq[:k], ""

    def handle_tool_call(self, tool_name=None, args=None, **kwargs):
        """模型主动来查（⑥「拉」）。

        ★ 安全第一：不管出什么事都**返回一句人话**，绝不抛异常。
          宿主兜得住异常，但那会在模型眼里变成一条冷冰冰的 `{"error": ...}`。
        ★ 防狂查：每轮限 TOOL_MAX_PER_TURN 次（计数在 prefetch/sync_turn 清零）。
          其余失败（没命中 / 向量服务不可用）一律静默降级成「没查到」。
        """
        t0 = time.time()
        try:
            if tool_name == SAVE_TOOL_NAME:
                return self._tool_save(args if isinstance(args, dict) else {})
            if tool_name != TOOL_NAME:
                return tool_error_text(f"不认识这个工具：{tool_name}")
            a = args if isinstance(args, dict) else {}
            q = str(a.get("query") or a.get("查询") or "").strip()
            if not q:
                return tool_error_text("query 是空的 —— 要查什么？给一句话或几个关键词。")
            if self._tool_calls_turn >= TOOL_MAX_PER_TURN:
                return tool_error_text(
                    f"本轮已经查过 {self._tool_calls_turn} 次（上限 {TOOL_MAX_PER_TURN}）——"
                    "先用手上已有的信息答，下一轮再查。")
            self._tool_calls_turn += 1
            try:
                k = int(a.get("k") or TOOL_K_DEFAULT)
            except BaseException:
                k = TOOL_K_DEFAULT
            k = max(1, min(k, TOOL_K_MAX))
            pool = str(a.get("pool") or a.get("池") or "").strip()
            if pool not in ("记忆", "标签", "对话"):
                pool = ""

            rows, err = self._tool_search(q, k, pool)
            self._stats["工具查询"] = self._stats.get("工具查询", 0) + 1
            ms = (time.time() - t0) * 1000
            if err or not rows:
                log(f"工具 {TOOL_NAME}({q[:30]!r}, k={k}, pool={pool or '全部'}) → "
                    f"没查到（{err or '没有过门槛的条目'}），用时 {ms:.0f} ms")
                return tool_error_text(
                    f"没查到和「{q}」相关的记忆。换个说法、换个关键词，或去掉 pool 限定再试。")

            items = []
            for s, name, body, anchor, note in rows:
                it = {"池": name, "相似度": round(float(s), 3),
                      "文本": " ".join(body.split())[:TOOL_ITEM_CHARS], "锚": anchor}
                if note:
                    it["备注"] = note
                items.append(it)
            tally = {}
            for s, name, _b, _a, _n in rows:
                tally[name] = tally.get(name, 0) + 1
            log(f"工具 {TOOL_NAME}({q[:30]!r}, k={k}, pool={pool or '全部'}) → "
                f"{len(items)} 条（" + " + ".join(f"{n} {c}" for n, c in tally.items()) +
                f"），最高分 {rows[0][0]:.3f}，用时 {ms:.0f} ms")
            payload = {
                "查询": q, "给了": len(items), "命中": items,
                "怎么取原文": "对话池 → session_search(session_id=…, around_message_id=锚里的数字)；"
                              "库 → topics-data/library.json 里 i=锚里的数字（那条 100 字原文）",
            }
            # 诚实提示：全都不像的时候明说「可能没有」——别让模型把 0.38 的噪声当答案。
            best_vec = max((s for s, *_ in rows if s < TOOL_EXACT_SCORE), default=0.0)
            n_exact = sum(1 for s, *_ in rows if s >= TOOL_EXACT_SCORE)
            if best_vec < TOOL_BLUNT_SCORE:
                if n_exact:
                    payload["提示"] = (f"向量分都不高（最高 {best_vec:.2f}）；上面 {n_exact} 条是精确串命中，"
                                      "可能只是词撞上了 —— 用之前先核一眼。")
                else:
                    payload["提示"] = (f"分都不高（最高 {best_vec:.2f}，精确串也没命中）——"
                                      "可能压根没有这条记忆，别硬用这些。")
            return json.dumps(payload, ensure_ascii=False)
        except BaseException:
            log("工具调用出错（已兜住，返回一句人话）：\n" + traceback.format_exc())
            return tool_error_text("查记忆时内部出错了，这次没给结果 —— 先用手上的信息，别停。")

    def on_session_end(self, *a, **kw):
        """会话结束钩子。

        ★ 2026-10-10 补：宿主在每次会话结束时会调 provider.on_session_end()，
        而这个类没实现它 —— agent.log 里留下 46 条
        `Memory provider 'topics' on_session_end failed: 'TopicsProvider' object has no
        attribute 'on_session_end'` 加一整个 traceback。功能上被宿主兜住了（不影响检索），
        但它是**每天都会刷的假报错**，会把真问题淹掉；而且会话结束时该做的收尾（清缓存、
        把这一轮的命中统计写盘）本来就没有地方做。
        这里只做收尾，不碰会话内容：清掉按库文件时间戳走的几处缓存，下一次会话重新读。"""
        try:
            for k in ("_INDEX_CACHE",):
                if k in globals() and isinstance(globals()[k], dict):
                    globals()[k].clear()
            self._topics = getattr(self, "_topics", [])
            log("on_session_end：缓存已清（正常收尾）")
        except BaseException:
            # 绝不让收尾钩子把宿主拖下水
            return None
        return None

    # ---- 向量服务 ----
    def _rerank(self, query: str, docs: list):
        """让本地交叉编码器给「问题 + 候选」重新排序，返回下标顺序；失败返回 None。

        为什么要它：向量是"各算各的向量再比余弦"，交叉编码器是"问题与候选一起读"，
        后者能分清"整体像"和"真的回答了这个问题"。离线真考卷实测进前 3 从 60%→73.3%。
        服务不在就先拉起（跟 _embed 同一套懒启动）。
        """
        payload = json.dumps({"query": query, "docs": docs}).encode()
        for attempt in (0, 1):
            try:
                req = urllib.request.Request(
                    BASE + "/rerank", data=payload,
                    headers={"content-type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=60) as r:
                    d = json.loads(r.read().decode())
                if d.get("ok") and isinstance(d.get("order"), list):
                    return d["order"]
            except BaseException:
                if attempt == 0 and self._start_service():
                    # 等待时间分两种情况（2026-10-10 实测改）：
                    #   服务**本来不在**（CLI 一次性跑、开机第一问）：现拉进程 + 磁盘加载
                    #     交叉编码器，实测要 5.5 秒才回；原来只等 8 秒，稍慢一点就白等
                    #     → 这一轮直接没有精排（静默降级，没人会发现）。
                    #   服务**已在**（桌面常驻）：那就是个别请求失败，等 2 秒足够。
                    warm = self._health_ok()
                    budget = 2.0 if warm else 30.0
                    step = 0.2
                    for _ in range(int(budget / step)):
                        time.sleep(step)
                        try:
                            req = urllib.request.Request(
                                BASE + "/rerank", data=payload,
                                headers={"content-type": "application/json"}, method="POST")
                            with urllib.request.urlopen(req, timeout=60) as r:
                                d = json.loads(r.read().decode())
                            if d.get("ok") and isinstance(d.get("order"), list):
                                return d["order"]
                        except BaseException:
                            continue
                    log(f"精排等待超时（服务此前{'在' if warm else '不在'}，等了 {budget:.0f}s）→ 本轮不精排")
                    break
        return None

    def _health_ok(self) -> bool:
        """服务是不是已经在跑（用来决定"等它多久"）。"""
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=1.5) as r:
                return bool(json.loads(r.read().decode()).get("ok"))
        except BaseException:
            return False

    def _embed(self, text: str):
        """问本地服务要向量；服务不在就懒拉起；彻底失败返回 None（走关键词降级）。"""
        r = self._post_vector(text)
        if r is not None:
            return r
        if self._start_service():
            for _ in range(30):          # 最多等 6 秒
                time.sleep(0.2)
                r = self._post_vector(text)
                if r is not None:
                    return r
        log("向量服务不可用，本轮走关键词降级")
        return None

    def _post_vector(self, text: str):
        try:
            req = urllib.request.Request(
                BASE + "/vector", data=json.dumps({"text": text}).encode(),
                headers={"content-type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            return d.get("vector") if d.get("ok") else None
        except BaseException:
            return None

    def _start_service(self) -> bool:
        if not SERVICE.exists():
            return False
        try:
            import subprocess
            SERVICE_LOG.parent.mkdir(parents=True, exist_ok=True)
            f = open(SERVICE_LOG, "a", encoding="utf-8")
            self._spawned = subprocess.Popen(
                ["node", str(SERVICE), str(PORT)],
                stdout=f, stderr=subprocess.STDOUT, cwd=str(SERVICE.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            log(f"拉起向量服务 pid={self._spawned.pid}")
            return True
        except BaseException:
            log("拉起向量服务失败：\n" + traceback.format_exc())
            return False

    def _dot(self, v, w) -> float:
        return sum(a * b for a, b in zip(v, w))

    def _inv_search(self, query, topk=6):
        """② 倒排查询：返回 [(分数, 对话池文档)]，只保留够精确的命中。

        门槛 INJ_MIN_SCORE 的意义：防"一个常用词命中一大堆"。
        实测（build_inverted.py 的基准）：精确串提问前 3 命中 6/8。
        """
        try:
            hits = {}
            toks = set(inv_tokens(query or ""))
            for tk in toks:
                w = self._inv_idf.get(tk)
                if not w:
                    continue
                for did in self._inv.get(tk, ()):
                    hits[did] = hits.get(did, 0.0) + w
            out = []
            for did, sc in sorted(hits.items(), key=lambda x: -x[1]):
                if sc < INV_MIN_SCORE:
                    continue
                d = self._conv_by_id.get(str(did))
                if d:
                    out.append((sc, d))
                if len(out) >= topk:
                    break
            self._stats["倒排命中"] = self._stats.get("倒排命中", 0) + (1 if out else 0)
            return out
        except BaseException:
            log("倒排查询失败：\n" + traceback.format_exc())
            return []


def register(ctx) -> None:
    log("=" * 70)
    log("register(ctx) 被调用")
    try:
        reg = ctx.register_memory_provider(TopicsProvider())
        log(f"  ✓ register_memory_provider 完成：{reg!r}"[:300])
    except BaseException:
        log("  ✗ register_memory_provider 失败：\n" + traceback.format_exc())


log("=" * 70)
log("topics 插件模块被 import")
