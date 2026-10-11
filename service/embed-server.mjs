// 在线向量服务 —— 让 Python 插件能问「这句话的向量是多少」。
//
// 为什么要有这个进程：
//   模型在 Node 那边（jev-local/judge.mjs 连着 @huggingface/transformers，
//   模型放在本地目录（TOPICS_MODEL_DIR），严格离线）。插件是 Python 写的。
//   跨语言调用两条路，实测选「常驻 + 空闲自动退出」：
//     常驻：热调用 3 ms ／ 每次现开：冷启动 204 ms（每轮都付）
//   所以平时不占资源，第一次用到才拉起，之后复用，闲置超时自己退出。
//   这不是我编的做法 —— Hermes 自带的 hindsight 后端 local_embedded 就是
//   「本地端口 + /health + idle_timeout」这套，插件起本地服务有既有先例。
//
// 接口（四个，别的都不做）：
//   GET  /health              → {"ok":true}              插件用它判断要不要拉起进程
//   GET  /quit                → {"ok":true} 然后自己退出   给测试用
//   POST /vector {"text"}     → {"vector":[...]}         拿一句话的向量
//   POST /rerank {query,docs} → {"order":[...],"scores"} 给候选重排（交叉编码器）
//
// 安全：只绑 127.0.0.1，不监听外网；不读不写任何用户数据。
//
// 用法：
//   node embed-server.mjs [port]
//   环境变量 EMBED_IDLE_MS 控制空闲退出毫秒数（默认 600000 = 10 分钟）
import http from "node:http";

// ---- 依赖（发布版：全部走环境变量，不写死任何人的机器）----------------------------
// 1) transformers.js：装了 npm 包就直接用包名；否则用 TOPICS_TRANSFORMERS 指向入口文件
const TRANSFORMERS = process.env.TOPICS_TRANSFORMERS || "@huggingface/transformers";
// 2) 模型缓存目录（严格离线时指向你下载好的模型）
//    ⚠ 注意：写 "./models" 时，它是**相对「启动这个进程时的工作目录」**，不是相对本文件所在目录。
//      插件拉起本服务时会把工作目录设成 embed-server.mjs 所在目录，所以从插件走是没问题的；
//      但你手动 `node service/embed-server.mjs` 从仓库根目录跑，它就会去 <仓库根>/models 找。
//      找不到就会直接起不来（因为下面把"允许联网下载"关掉了）。
const MODEL_DIR = process.env.TOPICS_MODEL_DIR || "./models";
// 3) 模型名（可换；默认取 512 维的中文小模型）
const EMBED_MODEL = process.env.TOPICS_EMBED_MODEL || "Xenova/bge-small-zh-v1.5";

const { pipeline, env } = await import(TRANSFORMERS);

// 精排模型：本地的交叉编码器（BAAI/bge-reranker-base 的 onnx 量化版）。
// 为什么用它：向量粗筛只能比"整体像不像"，交叉编码器是"问题+候选一起读"，
// 能把真正回答了问题的候选顶上来。严格离线，记忆内容不出机器。
env.cacheDir = MODEL_DIR;
env.allowRemoteModels = false;
env.allowLocalModels = true;
const RERANK_MODEL = "Xenova/bge-reranker-base";
let _reranker = null;
let rerankCalls = 0;
async function loadReranker() {
  if (!_reranker) _reranker = await pipeline("text-classification", RERANK_MODEL, { dtype: "q8" });
  return _reranker;
}
/**
 * 取**原始 logit** 当相关性分数。
 * 为什么不能用 pipeline 的 score：bge-reranker 是单标签分类头，
 * pipeline 会做 softmax，单类概率恒等于 1 —— 实测三个候选全给 1.0，
 * 排序完全失效。必须绕过 softmax 直接读 logits。
 */
async function rerankScore(query, doc) {
  const clf = await loadReranker();
  const tok = clf.tokenizer;
  const inputs = await tok(query, { text_pair: doc, padding: true, truncation: true });
  const model = clf.model;
  if (model && typeof model === "function") {
    const out = await model(inputs);
    const logits = out && (out.logits || out[symbolKey(out)]);
    const data = logits && logits.data;
    if (data && data.length) return Number(data[0]);
  }
  // 兜底：退回 pipeline，至少不崩（但分数会是常数）
  const r = await clf(query, doc, { topk: 1 });
  const first = Array.isArray(r) ? r[0] : r;
  return first && typeof first.score === "number" ? first.score : 0;
}
function symbolKey(o) {
  try { return Object.keys(o)[0]; } catch { return ""; }
}

const PORT = Number(process.argv[2] || process.env.EMBED_PORT || 8765);
const IDLE_MS = Number(process.env.EMBED_IDLE_MS || 600000);
const HOST = "127.0.0.1";

let lastHit = Date.now();
let calls = 0;

// 先把模型加载进内存再开始监听 —— 这样 /health 一旦可用，
// 后面每次调用都是热的（3 ms），不会把冷启动分摊到某一轮对话上。
// ---- 向量函数 ------------------------------------------------------------------------
// 默认：用本地 transformers.js 现算（自包含 —— 装好依赖就能跑）。
// 可选：设了 TOPICS_VECTOR_MODULE 就用那个模块导出的 vector()（作者自用版带了"按前 400 字"
//       的缓存；实测短句向量干净、长文糊，所以那边只喂前 400 字）。
// ⚠⚠ 要照抄这段去建自己的池子，先读这里 ⚠⚠
//   作者自己那份池子（1213 条记忆 / 5120 条标签）里的 512 维向量**不是**用下面这段算的：
//       · 作者用的是 judge.mjs 里的 vector()：pooling = "cls"，而且只喂前 400 字（带缓存）
//       · 下面这段默认是：pooling = "mean"，喂全文
//   同一句话，这两套算出来是**两组不同的数** —— 混着用不会报错，只会让检索悄悄变差。
//   所以：要么全程用同一套（建池子用哪套，查询就必须用哪套）；
//         要么设 TOPICS_VECTOR_MODULE 指向你自己那份 vector()（作者就是这么接的）。
//   bge 系列官方推荐的是 cls 池化，所以作者那套更"正统"；这里留 mean 是为了让默认值不依赖外部文件。
let vector;
if (process.env.TOPICS_VECTOR_MODULE) {
  ({ vector } = await import(process.env.TOPICS_VECTOR_MODULE));
} else {
  const _embedder = await pipeline("feature-extraction", EMBED_MODEL, { dtype: "q8" });
  // ⚠ 这里的 "mean" 与作者存量池子的 "cls" 不一致 —— 见上面那段说明
  vector = async (text) => (await _embedder(text, { pooling: "mean", normalize: true })).data;
}

const t0 = Date.now();
await vector("预热");
const coldMs = Date.now() - t0;

const server = http.createServer(async (req, res) => {
  lastHit = Date.now();

  if (req.method === "GET" && req.url.startsWith("/health")) {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true, cold_ms: coldMs, calls, pid: process.pid }));
    return;
  }

  if (req.method === "GET" && req.url.startsWith("/quit")) {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true, bye: true }));
    setTimeout(() => process.exit(0), 50);
    return;
  }

  if (req.method === "POST" && req.url.startsWith("/vector")) {
    let body = "";
    req.on("data", (c) => {
      body += c;
      if (body.length > 1_000_000) req.destroy();   // 防超大请求
    });
    req.on("end", async () => {
      try {
        const { text } = JSON.parse(body || "{}");
        if (typeof text !== "string" || !text.trim()) {
          res.writeHead(400, { "content-type": "application/json" });
          res.end(JSON.stringify({ ok: false, error: "text 不能为空" }));
          return;
        }
        const s = Date.now();
        const v = await vector(text);
        calls++;
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ ok: true, vector: Array.from(v), ms: Date.now() - s }));
      } catch (e) {
        res.writeHead(500, { "content-type": "application/json" });
        res.end(JSON.stringify({ ok: false, error: String(e) }));
      }
    });
    return;
  }

  if (req.method === "POST" && req.url.startsWith("/rerank")) {
    // 精排：给「问题 + 一批候选」打分，返回按分数排序的下标。
    // 用途：向量粗筛出候选后，用交叉编码器把真正相关的顶上来
    // （实测动机：宽门槛捞回一批之后，前 3 名常被"分数高但其实不相关"的挤掉）。
    let body = "";
    req.on("data", (c) => {
      body += c;
      if (body.length > 4_000_000) req.destroy();
    });
    req.on("end", async () => {
      try {
        const { query, docs } = JSON.parse(body || "{}");
        if (typeof query !== "string" || !Array.isArray(docs) || !docs.length) {
          res.writeHead(400, { "content-type": "application/json" });
          res.end(JSON.stringify({ ok: false, error: "需要 query 和 docs[]" }));
          return;
        }
        const s = Date.now();
        // 一次一条打分（交叉编码器必须成对输入；批量太长反而更慢）
        const scores = [];
        for (const d of docs) {
          scores.push(await rerankScore(query, String(d || "")));
        }
        const order = scores.map((v, i) => [i, v]).sort((a, b) => b[1] - a[1]).map((p) => p[0]);
        rerankCalls++;
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ ok: true, order, scores, ms: Date.now() - s }));
      } catch (e) {
        res.writeHead(500, { "content-type": "application/json" });
        res.end(JSON.stringify({ ok: false, error: String(e) }));
      }
    });
    return;
  }

  res.writeHead(404, { "content-type": "application/json" });
  res.end(JSON.stringify({ ok: false, error: "只有 /health 、/vector 和 /rerank" }));
});

server.listen(PORT, HOST, () => {
  console.log(`READY ${HOST}:${PORT} cold_ms=${coldMs} pid=${process.pid}`);
});

// 空闲自动退出：平时不占资源
const timer = setInterval(() => {
  if (Date.now() - lastHit > IDLE_MS) {
    console.log(`空闲 ${IDLE_MS} ms，退出`);
    process.exit(0);
  }
}, Math.min(30000, Math.max(1000, IDLE_MS / 4)));
timer.unref?.();
