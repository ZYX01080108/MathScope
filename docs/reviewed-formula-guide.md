# 已核对公式片段：先受控恢复一条回答

日期：2026-10-15。当前只登记 FR001：原 lecture 物理第 9 页的三角不等式取等号条件。
不是通用公式 OCR，不是整页无损恢复，也没有解除原始回答接口的公式风险拦截。
后续已有 [六题回答/拒答开发评测](answer-evaluation-guide.md)，默认免费预检；
answer-reviewed 可选传 expected_reviewed_context_id 绑定预览版本，旧手动请求保持兼容。

## 1. 本步解决什么

原始提取文本把 `z*conjugate(w)` 丢成 `zw`。此前的保护可以拒答，却不能交付正确
结论。本步将视觉核对结果作为独立、可追溯的注释登记，不改原文和原有字符位置。

src/reviewed_formulas.json 当前只有一条记录，包含原 PDF 的 SHA256、物理第 9 页、
原始字符区间 [67,88)、原始有损引文、核对后的结论和 LaTeX、日期及审阅方式。
结论为：复数三角不等式取等号，当且仅当 z·conj(w) 为非负实数，conj(w) 表示共轭。

审阅方式明确为 assistant_visual_review：由助手查看原 PDF 页图核对，不是人工审定、
自动 OCR 或形式化证明。scope=single_statement：只涉及这一条结论，不涉及同页其余公式。
记录是服务器维护的受控注释文件，没有公开编辑/上传 API，也不会自动学习模型的回答。

## 2. 为什么不直接覆盖提取文字

原始 pages/chunks 和字符 offsets 是现有引用链的基础。直接加入一个共轭符号会
改变字符位置，还可能误改普通的 zw 乘法。本步保留两层：

- raw_quote/raw_start_char/raw_end_char：原始提取句子及它在 PDF 页提取文本中的位置。
- reviewed_statement/formula_latex：视觉核对后的注释，不冒充 PDF 的逐字提取结果。

输出 source_kind=reviewed_formula_annotation，quote 引自注释；不再输出误导性的
quote_start_char/quote_end_char，把注释长度假装成 PDF 原文长度。
raw_context_id 和 reviewed_context_id 分别标识原始证据包与已核对注释包；它们是
版本指纹，不是认证令牌。每条注释还有 review_revision，用于发现审阅记录变化。

## 3. 新模式如何工作

1. 仍先按原来的文档编号、query/method/K/预算检索，不直接回退到固定讲义。
2. 只有源 SHA256 相同，且原文区间完整落在本次收录证据中，注释才有资格被选用。
3. 比对已保存的原文切片，再读取校验后的 PDF 字节，用当前 pypdf 重新提取对应页，
   确认原文区间仍然匹配。原 PDF/提取结果改变时需要重新核对，不能套用旧记录。
4. 把“问题＋入选的已核对结论”发给模型；不发送有损引文、同页未审阅段落、整份 PDF、
   本机路径、文件编号/指纹或历史对话。
5. 模型只能逐字选择完整 reviewed_statement，不得自由改写、拼接新推导或补证明；
   无法仅靠注释回答时应拒答。后端检查结论与引文是否都逐字匹配登记的同一条记录。
6. 模型返回后再次构造原始和核对证据包，来源或注释版本变化就丢弃结果。

即使模型引用了真正存在的注释，只要把结论改回 `zw ≥ 0`、增加推导或改写句子，
也会得到 rejected，且不交付任何 claims。这是受限的“结论选择”模式，不是通用生成。
模型是否选对了与问题相关的结论、拒答是否合理，以及审阅记录本身是否正确，仍需
评测；semantics_verified 保持 false，不能声称自动证明了数学正确性。

原 /answer 的页级风险门完全保留。这不是一个把 blocks_generation 改成 false 的
绕过开关：新模式只提供已审阅的短结论，未审阅的风险原文不会进入新模式的模型输入。

## 4. 两个新增接口

| 接口 | 用途 | 是否可能收费 |
|---|---|---|
| POST /documents/{document_id}/reviewed-context | 预览本次检索可用的核对注释及原文映射 | 不调用模型 |
| POST /documents/{document_id}/answer-reviewed | 从这些结论中受限选择回答 | 有注释且启用模型时可能收费 |

当前共 14 个业务接口。旧接口字段和主工作台检索行为保持不变，新接口只在 /docs
单独验收。preview 请求不填 allow_external；回答请求必须明确 allow_external=true。
回答模式与原始 /answer 共用同一个 DeepSeek 客户端、次数额度、并发保护、超时及
不重试策略，不为新模式另开额度。失败、拒绝草稿、超时也可能已计费。

核对注册文件最多 64 KiB、最多 8 条，字段、重复 ID/位置、长度、日期等不一致时
拒绝使用，不悄悄回退到风险原文。客户端不能传入自己的记录、注册文件路径或密钥。
相同字节内容的 PDF 重新上传可以复用同一源注释；仅仅文件名相同不行。

## 5. 用户验收：先免费预览

先在原服务终端 Ctrl+C 停止，再启动新代码：

```powershell
.\.venv\Scripts\python.exe -m uvicorn src.api:app --host 127.0.0.1 --port 8000
```

同一终端原有密钥环境变量可以继续使用。新终端需要重新按 DeepSeek 指南配置，
但免费预览不需要密钥。付费服务不加 --reload/多 workers。重启会重置本地调用计数，
不会撤销已产生的账单，也不是账户 20 元消费硬限制。

打开 /docs 并刷新，先确认看到 reviewed-context。对原讲义，编号填写：

```text
f73e1265fc5242829509031e208d7256
```

展开 POST /documents/{document_id}/reviewed-context，使用以下请求并点一次 Execute：

```json
{
  "query": "复数三角不等式在什么条件下取等号？",
  "method": "normalized",
  "k": 10,
  "max_context_chars": 6000
}
```

在真正的 Server response 中应看到 status=ready、included_count=1、FR001、物理
页 9，以及含 conj(w) 的 reviewed_statement。raw_quote 仍显示有损的 zw，这表示
两层被明确区分，不是修改失效。review_method 为 assistant_visual_review，
raw_text_modified 为 false。不要从页面下方 Example Value 复制虚构编号。

## 6. 可选：一轮真实模型选择验收

免费预览符合预期后，可展开 POST /documents/{document_id}/answer-reviewed，使用
同一个真实编号和上面的请求体，另加 `"allow_external": true`。这一步可能收费，
只点击一次并等待，不要因 rejected/超时反复请求。

这道题预期可以只选择 FR001 的结论。返回时核对：

- status 是 answered/refused/rejected 中哪一种，不能只看 HTTP 200。
- check.claims 中结论包含 conj(w)，并与预览的 reviewed_statement 完全一致。
- citation 的 source_kind=reviewed_formula_annotation，review_id=FR001，物理页=9。
- quote 是注释原句；raw_quote 和 raw_* 区间才对应原始提取结果，不把两者混为一谈。
- generation.model_called/usage/calls_used，及仍为 false 的 check.semantics_verified。

如果没有匹配的注释或预算不足，新模式本地拒答、model_called=false，不回退到原文。
若要求完整证明，目前只有结论，应该拒答而不是补写未审阅证明；但拒答合理性仍须验收。
本步没有运行真实 DeepSeek，因此不能预先保证模型一定选择而非拒答或返回格式错误。

## 7. 验证记录与未完成部分

新增 30 项测试，累计 349 项 Python 与 21 项 JavaScript 测试通过；依赖版本检查
和 pip check 通过。夹具使用本地合成 PDF 的真实提取流程与 MockTransport，不需要
私有讲义或真实密钥。覆盖原文/注释分离、来源匹配、位置/版本/日期/额度、错误
结论及新推导的拒绝、注释在模型调用期间改变、来源再次校验等。

真实 lecture 的独立只读验收中，预览取得 FR001；模拟模型选择正确原句得到 answered，
把结论改回错误的 zw 则 rejected。旧 /answer 仍 refused。所有上游响应都是模拟，
没有新增真实模型调用或费用；模拟 usage 不代表用户的真实消费。原 PDF、上传记录、
三个解析文件和注释注册文件的指纹在验收前后不变。

没有安装依赖、覆盖解析数据、发送页图、运行外部 OCR、创建 Git 提交或更改标签。
本步没有自动消除第 9 页全部风险，只恢复这一条已核对结论的受限调用路径。后续
仍需真实模型选择验收、审阅记录质量评估、更多公式/证明的可靠恢复及问答评测。
