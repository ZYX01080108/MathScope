# MathScope：可追溯的数学讲义 RAG 原型

基于 Python、jieba、BM25、FastAPI 和 DeepSeek API 的本地 PDF 检索与受限问答项目。
重点不是只生成答案，而是让检索片段、模型引用和原始 PDF 之间保留可检查的来源关系。

## 项目亮点

- PDF 页级解析与按页字符分块，保留物理页号、字符区间和来源 SHA-256。
- 中文 BM25 稀疏检索，对比原始分词与术语统一方案，提供固定开发集与分层评测脚本。
- 在字符预算内打包证据，通过请求级证据编号、版本绑定和字面引文校验约束模型草稿。
- 针对 PDF 共轭符号丢失的真实反例增加公式风险拦截；核对模式只选择已登记结论，不自由补充证明。
- 提供上传、解析、检索、免费证据预览与显式许可后的模型调用网页，密钥只保留在服务器环境中。

这是 BM25 稀疏检索 RAG，不包含向量数据库、Embedding、混合检索或重排模型。
引用检查不等于语义蕴含或数学正确性验证，`semantics_verified` 保持为 `false`。

## 快速体验：无需密钥，也无需自带固定讲义

要求 Python 3.11；在项目根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
python -m uvicorn src.api:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/`，上传自己有权处理的 PDF，再依次解析、检索和预览证据。
`http://127.0.0.1:8000/docs` 是交互式 API 文档。
默认不调用模型；配置模板 `.env.example` 不会自动加载，启用方式见
[DeepSeek 接入指南](docs/deepseek-guide.md)。不要将密钥粘贴到网页或提交到 Git。

## 已有实验与能力边界

2026-10-18 的本地复现中，在固定十题开发集的 8 道计分题上，术语统一使
Top-10 平均目标证据覆盖率由 75% 提升至 87.5%。这是小规模开发集上的
已标目标证据指标，不是通用召回率或问答准确率；Top-5 没有改善。
原始 PDF 不随仓库发布，完整复现固定讲义实验需要自行准备指纹匹配的数据；
普通上传、检索和离线单元测试不依赖这份讲义。

当前为本地、单用户、单文档查询 MVP，不应直接开放公网。
网页受限回答仅登记一条来源绑定的核对结论，不能回答任意数学问题或自动生成完整证明。
项目在 AI 辅助编码与指导下完成，测试通过也不代表通用数学答案已经验收。

## 公开副本说明

本仓库整理自 2026-10-20 的完整本地代码，不携带原项目的 Git 历史、密钥、
上传文件、解析数据和运行报告。下文及历史文档提到的 `backend-v0.1` 标签、
“尚未提交”等描述是原开发目录当时的记录，不是本公开仓库的 Git 状态。
公开发布代码不等于已经部署在线服务。

下面保留原有的实现说明、运行方法和阶段记录。

## 后端里程碑

当前已完成本地、单用户、可追溯的 PDF 检索与受限问答 MVP，已有网页操作入口及 DeepSeek 回答接口。
代码与模拟流程已验收，用户已完成一次真实调用，但发现公式丢失导致的错误回答。
当前新增公式提取风险拦截，以及单条视觉核对注释的受限回答模式；尚未完成通用
公式恢复或数学答案质量验收，也未公网部署。
[后端 v0.1 里程碑与后续路线](docs/backend-v0.1-milestone.md) 记录客户端/HTTP/API/后端关系、
初始 112 项测试与十题开发评测、后续增量、当前限制和验收标准。
本地基线标签为 `backend-v0.1`，包含当前后端代码、测试、配置模板与依赖快照。
[版本范围与安全取出方法](docs/release-backend-v0.1.md) 说明如何查看该版本。
这是本地 Git 归档，不是公网发布或远程备份；讲义与解析数据不包含在代码版本中。

## 当前目标：
已打通上传、按编号解析、单文档检索与目录查询，保留原文和物理页号的可追溯性。
可恢复的后端基线已归档，后续增量包括本地操作页面、证据包和字面引用核验。
当前进入真实模型接入验收；语义引用支持、回答/拒答评测和公网部署仍未完成。
已增加六题回答/拒答开发评测脚本；免费预检已完成，真实模型整轮评测仍待显式开启。

## 核心原则：
- 先建立可测量的检索基线，再接入大模型。
- 先保证证据可追溯，再追求回答流畅。
- 证据不足时，系统应拒答。

## 环境与运行

在项目根目录执行，要求 Python 3.11：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
python scripts/check_environment.py
python -m pip check
python -m unittest discover -s tests -v
```

`requirements.txt` 保留 8 个直接依赖，`requirements.lock.txt` 记录本次验收环境的
22 个包版本（包含间接依赖）。恢复已验收环境优先使用后者；它是 Windows x64、
Python 3.11 的版本快照，不是带安装包哈希的严格锁文件，也不是跨平台保证。
不要为了验证恢复而覆盖已有虚拟环境或删除数据。创建独立环境的方法、检查脚本的
边界、代码与数据备份的区别见 [环境基线与恢复说明](docs/environment-baseline.md)。

以下 CLI 和固定开发集评测需要自行准备讲义数据；它们与空库 HTTP 启动独立。
原始文献放在 `data/raw/lecture.pdf`。已有的解析与分块结果分别为
`data/parsed/lecture.page.jsonl` 和 `data/parsed/lecture.chunks.jsonl`。

```powershell
python -m src.bm25_cli
python -m src.compare_bm25
```

比较程序使用十道固定开发题、两种 BM25 方案、K=5/10/20，自动将结果保存到
`reports/evaluation-<运行编号>.json`，不覆盖旧报告。也可指定一个尚不存在的路径：

```powershell
python -m src.compare_bm25 --output reports/my-first-evaluation.json
```

题目和答案要点见 `docs/eval_cases.json`，数据版本和文件指纹见
`docs/eval_manifest.json`，标注口径和改动解释见 `docs/evaluation-guide.md`。
更换 PDF 或分块后，必须重新核对标注与清单；文件指纹不一致时程序会报错。
当前只报告目标证据覆盖率、目标 MRR 和目标 nDCG，不宣称相关性已完全标注。

## 本地操作页面（backend-v0.1 之后的增量）

启动下方 HTTP 服务后，访问 `http://127.0.0.1:8000/` 即可上传、选择文档、解析、
检索、免费预览核对结论和明确许可后的 DeepSeek 回答，无需手动复制编号或证据版本。
`/docs` 仍保留为接口测试页。前端是由现有 FastAPI 提供的
HTML/CSS/JavaScript，没有前端构建或 npm 依赖；运行页面不需要 Node。

Python 373 项测试和另外 38 项 JavaScript 交互测试通过，后者需独立执行：

```powershell
node --check src/web/app.js
node --test tests/web/workspace.test.cjs
```

交互测试使用实际页面脚本与 DOM/fetch 替身，不是真实浏览器端到端或布局验收。
2026-10-17 已在真实浏览器验证选择现有讲义、免费预览和安全禁用状态；真实付费
回答未在本次自动运行。最新操作、边界和后续路线见 [本地 MVP 交付说明](docs/local-mvp-guide.md)。
页面及模型接入增量尚未提交，`backend-v0.1` 仍指向此前的纯后端基线。
页面搜索和预览不隐式触发模型；只有证据与本机配置就绪、明确许可并点击回答按钮才请求模型。
当前受限模式只覆盖已登记的核对结论，不能回答任意数学问题；本次没有改检索算法。

当前文档区可打开原 PDF，每条证据可打开对应的 PDF 物理页。链接绑定检索时的
SHA256，来源版本不一致时返回 409，不自动换文件。页面使用浏览器自带查看器，
不保证所有浏览器自动跳页，没有片段高亮；详细边界见工作台说明第 6 节。

## 本地 HTTP 接口

### 回答/拒答开发评测（默认不收费）

服务运行后，在另一个终端、项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m src.evaluate_answers --document-id f73e1265fc5242829509031e208d7256
```

默认只免费预览六题的可用核对证据、验证来源并保存新报告，不调用模型、不填写
回答分数。显式 `--live --allow-paid --max-model-calls 3` 才执行可能收费的整轮；
采用本机现有服务并共用调用额度。六题基于一条核对结论，不是通用数学准确率。
脚本、评分口径、停止保护与报告说明见 [回答与拒答评测指南](docs/answer-evaluation-guide.md)。

### 服务启动与接口

在项目根目录、虚拟环境激活后运行：

```powershell
python -m uvicorn src.api:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/docs`，可以测试：

- `GET /health`：检查服务是否运行。
- `GET /generation-status`：只读查看模型标识、本机配置状态和尝试次数，不调用模型或验证余额。
- `GET /documents?offset=0&limit=20`：只读列出文档编号、名称和处理状态。
  limit 为 1～100，按编号字典顺序分页（不是时间或相关性排序）。
- `GET /documents/{document_id}`：只读查看单个文档，未知编号返回 404。
- `GET /documents/{document_id}/file?sha256=<预期指纹>`：只读返回校验后的原始 PDF，
  不要求先解析；指纹可省略，证据链接会携带它。未知/缺失文件为 404，来源冲突为 409。
  当前共有 15 个业务操作（含免费 generation-status）；context 响应包含 context_id，answer 响应包含 quality。
- `POST /search`：查询当前固定的 lecture 分块，支持 original/normalized 和 K=1～20。
  这是可选的实验基线；缺失、为空、损坏或没有可检索词时返回 503，其他文档接口仍可用。
- `POST /documents`：用文件选择框上传单个 PDF，最多 10 MiB。成功返回 201，
  包含文档编号、显示名称、字节数、SHA-256 和 uploaded 状态。
- `POST /documents/{document_id}/parse`：填写上传返回的编号和 JSON
  `{"chunk_size": 500}`，核对原始文件指纹后提取页面、按页分块。成功返回 200，
  包含 parsed 状态、物理页数、非空文本页数、空文本页号、块数与解析器版本。
- `POST /documents/{document_id}/search`：只搜索这个编号的已解析文档，正文与
  /search 相同，例如 `{"query": "复数三角不等式", "method": "normalized", "k": 5}`。
  返回文档编号、原文件指纹、索引块数、cache_hit 和带文档编号的来源片段。
  未上传返回 404，未完成解析或数据不一致返回 409，不回退到 lecture 数据。
- `POST /documents/{document_id}/context`：复用搜索并按预算打包 E1/E2 等编号证据，
  增加 `max_context_chars`（默认 6000），保留整块原文、页号、字符区间和来源指纹。
  不调用模型、不自动回答或拒答，`ready` 仅表示有块被收录。
  本次先通过 `/docs` 独立验收，主工作台搜索按钮不变；详见
  [证据打包使用说明](docs/evidence-context-guide.md)。
- `POST /documents/{document_id}/check-answer`：重建并绑定 context_id，检查草稿的
  回答/拒答格式、编号归属与字面引文，返回服务器推导的来源位置。不调用模型，
  `passed` 不代表语义或事实正确；`semantics_verified` 固定为 false。
  操作与错误实验见 [回答引用核验说明](docs/answer-check-guide.md)。
- `POST /documents/{document_id}/answer`：自动检索、证据打包、DeepSeek 生成 JSON 草稿、
  字面引用核验并再次检查来源版本。先检查 PDF 公式提取风险；有风险/无法检查则
  本地拒答、不调用模型、不改写原文。无标记才进入模型步骤；未发现标记不是公式可靠保证。
  模型默认关闭，必须在服务器配置密钥并明确传入
  `allow_external: true`；有可能收费。默认 normalized、K=10、最多 6000 证据字符。
  `answered/refused/rejected` 与 HTTP 成功分开，`check.semantics_verified` 仍为 false。
  密钥配置、费用边界与第一次操作见 [DeepSeek 接入验收指南](docs/deepseek-guide.md)。
- `POST /documents/{document_id}/quality`：只读检查当前证据页的细短水平绘图痕迹，
  不恢复公式、不调用模型，无需密钥或外发许可。字段、误报/漏检限制与免费验收
  见 [公式提取风险说明](docs/formula-quality-guide.md)。
- `POST /documents/{document_id}/reviewed-context`：免费预览当前检索片段可用的视觉
  核对公式注释，绑定文件指纹、物理页、原文位置与核对版本；不改写原始提取文本。
- `POST /documents/{document_id}/answer-reviewed`：仅允许逐字选择已核对结论，不新增
  证明、改写或放行整页风险原文；可能收费，与原回答共用模型额度。目前只登记
  原 lecture 第 9 页的一条结论，审阅方式为助手视觉核对，不是人工审定或自动 OCR。
  分层来源与验收见 [已核对公式片段指南](docs/reviewed-formula-guide.md)。
  请求可选填写 `expected_reviewed_context_id`，在模型调用前绑定免费预览版本；
  评测脚本会自动填写，版本改变返回 409，不自动重试。

上传文件使用随机编号保存到 `data/raw/uploads/<document_id>.pdf`，相邻 JSON
保存元数据；同名上传不会覆盖旧文件。原始数据目录已经被 Git 忽略。
uploaded 仅表示已保存，不代表解析成功。解析结果保存到
`data/parsed/uploads/<document_id>/`，包括 `pages.jsonl`、`chunks.jsonl` 和
最后写入的 `parse_report.json`。原始 PDF 和上传元数据保持不变；上传元数据
是上传时的记录，当前解析阶段由单独的报告记录。空白页仍保留物理页号。
同名文件通过 document_id 区分；重复解析返回 409，当前版本不覆盖旧结果。
上传、解析和按编号搜索都不会改变 /search 的 lecture 基线索引。
按编号搜索先核对上传记录、原文件指纹、解析报告、页号和全部字符切片。
首次使用某个“文档编号＋检索方案”时建立 BM25；数据未变化的后续请求复用索引。
每次请求仍读取文件并计算指纹，缓存只避免重复分词、校验分块和建立 BM25，
不是零磁盘开销。最多缓存 4 套索引，最近最少使用的会被移出；使用锁避免
同时重复建立索引。缓存只在当前进程内，重启后重新建立，不删除磁盘上的文档。
当前单个页面或分块文件最多加载 64 MiB、最多索引 10 万块；缓存条数限制
不是严格的内存字节限制。暂不跨文档联合搜索；这些搜索接口自身不生成答案。

文档列表/详情从磁盘推导状态，不改上传记录、不重新解析、不建立索引：
uploaded 表示原文件已保存且指纹通过检查、没有解析目录；parsed 表示原文件
指纹和报告一致且三个解析文件存在；incomplete 表示解析目录存在但文件不齐；
invalid 表示上传记录、原文件、路径或解析报告异常。异常文档单独显示，不让
整页列表失败；无法确认的名称/页数等字段为 null。状态检查不逐条读取页面和
分块，不等于全文来源校验通过，搜索仍执行全部字符切片校验。parsed 也不
意味着当前内存缓存已存在、每页都有可检索文本、公式正确或能够回答问题。
单个异常文档的信息查询返回 200 和异常状态，这是查询成功，不是处理成功。
目录查询会扫描目录、读取所选文档记录并核对原始 PDF 指纹；目前适用于本机
少量文档，不是大型数据库目录或解析任务进度系统。

当前只检查 .pdf 扩展名和 %PDF- 文件头，不是完整的 PDF 有效性检查或安全扫描。
大小限制发生在 multipart 解析之后（UploadFile 可能已经使用临时磁盘），
不是整个 HTTP 请求的网络接收上限。这里只用于本机单用户学习服务，不应直接
暴露到公网；公网部署还需要入口请求限制、鉴权和完整解析时的资源保护。

### 空文档库启动（2026-10-09 增量）

无需准备 lecture 或预先创建数据目录，即可使用上面的 Uvicorn 命令启动服务。
/health 返回 200，/docs 可打开，GET /documents 在没有任何文档时返回空列表。
随后按上传 -> 按编号解析 -> 按编号搜索的流程处理自己的文档。
默认 lecture 分块只是可选实验数据，在启动时加载一次；缺失、为空、超限、
无法读取或格式损坏时会记录未就绪原因，旧 POST /search 返回 503 和提示，
不会退回空结果或最新上传的文档。损坏记录有日志告警，不阻止正常文档服务。
准备或修复默认基线后需要重启服务才能加载；当前未增加自动监听数据文件或配置接口。
已有有效默认基线的行为不变。/health 表示服务存活，不表示默认实验基线已就绪。
不要为了验收删除真实文件；test_api 中使用临时空目录/坏文件检查此行为。
该增量更新了两项旧启动测试并新增 7 项检查；当前全部 119 项测试通过。
另已用临时空目录和随机本机端口启动真实 Uvicorn，验证 health/docs 为 200、
文档列表为空、旧 /search 为 503，结束后关闭临时服务，不占用现有 8000 端口。

### 独立应用配置（2026-10-09 增量）

HTTP 服务配置集中在 `src/settings.py`，不增加额外依赖。`create_app(settings)`
创建一个独立的 FastAPI 应用；每个应用持有自己的不可变配置和内存索引，不再
依赖可修改的全局数据路径。现有 `src.api:app` 启动命令与默认数据布局保持不变。

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| MATHSCOPE_DATA_DIR | 项目根目录下的 data | 上传文件和解析结果的数据根目录 |
| MATHSCOPE_BASELINE_CHUNKS | 数据根目录下的 parsed/lecture.chunks.jsonl | 可选旧 /search 基线；可另指向一份 JSONL |
| MATHSCOPE_INDEX_CACHE_SIZE | 4 | 文档编号＋方案的索引缓存套数，整数 1～64；不是查询 K 或内存字节上限 |

配置在创建应用时读取。显式传入 Settings 对象时使用该完整对象；否则读取
当前进程环境，未设置的变量使用默认值。环境变量是文本，缓存数量会转换并
校验为整数。空路径、磁盘根目录、类型不匹配的现有文件/目录与不合法缓存值
会明确报错，不会悄悄采用另一份库。不存在的数据目录允许配置，直到首次写入
才创建；读取配置或列表不会自动创建目录。

相对路径始终相对项目根目录，不相对启动终端的工作目录。填写实际路径，不
自动展开 ~、%变量% 或 $变量；Windows 的 D:folder、\\folder 这类不完整路径
会被拒绝。配置不是 API 请求参数，客户端不能通过查询参数指定服务器任意保存路径。

例如在另一个 PowerShell 终端中，从项目根目录启动独立演示库：

```powershell
$env:MATHSCOPE_DATA_DIR = "D:\MathScope-config-demo"
$env:MATHSCOPE_BASELINE_CHUNKS = "D:\MathScope-config-demo\parsed\lecture.chunks.jsonl"
$env:MATHSCOPE_INDEX_CACHE_SIZE = "2"
.\.venv\Scripts\python.exe -m uvicorn src.api:app --host 127.0.0.1 --port 8001
```

首次使用这个空目录时，打开 `http://127.0.0.1:8001/documents` 会看到空列表。
随后上传保存到 `D:\MathScope-config-demo\raw\uploads`，解析结果保存到
`D:\MathScope-config-demo\parsed\uploads`。原项目 data 下的文件没有搬迁或删除；
8000 上的原服务不受这个终端的设置影响。演示目录若以前用过，列表可能非空。

用 Ctrl+C 停止演示服务后，可在同一终端恢复默认配置：

```powershell
Remove-Item Env:\MATHSCOPE_DATA_DIR -ErrorAction SilentlyContinue
Remove-Item Env:\MATHSCOPE_BASELINE_CHUNKS -ErrorAction SilentlyContinue
Remove-Item Env:\MATHSCOPE_INDEX_CACHE_SIZE -ErrorAction SilentlyContinue
```

这里只移除当前终端的环境变量，不删除任何数据。$env 设置影响该终端和后续
启动的子进程，不自动修改已经运行的服务；变更配置后需要重新启动服务。
`.env.example` 仅提供参考，本项目目前不自动读取 `.env`，复制文件不会自动生效。
这三项配置只影响 HTTP 服务，原有 CLI 与固定开发集评测仍按各自项目默认路径运行。
没有改变上传/解析等业务安全上限，也没有增加多用户鉴权或数据库事务隔离。
不同数据目录是不同文件库；若主动给多个实例配置同一个目录，它们会共享该文件库。

原有接口测试已迁移为独立应用配置；新增 15 项配置/实例隔离测试，当前全部
134 项测试通过。测试不需要修改进程全局路径，也不接触真实讲义数据。
另已用临时数据目录和随机本机端口完成真实 HTTP 验收：健康检查 200、首次
列表为空、上传 201 且文件落在配置目录、列表更新为 1、缓存容量为 2，缺失
可选基线时旧 /search 为 503。测试服务已停止；原有文档检索仍正常，真实上传
及解析文件的 SHA256 前后未变。

解析当前在请求内同步完成，暂不支持加密文件或 OCR。第一版限制 300 页、
提取结果 200 万字符；字符上限在文本提取后检查，不能作为解析时 CPU/内存的
硬隔离。PDF 公式和排版仍可能在文本提取中丢失或错序，parsed 不代表 OCR
完成、数学内容正确或检索已就绪。本机学习阶段之后再考虑任务队列与隔离进程。
