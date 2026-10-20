# DeepSeek 后端接入与首次验收

日期：2026-10-13。用户选择 DeepSeek，允许发送问题和检索片段，讨论首轮 20 元预算。
用户现已提供首次真实返回：输入 3484、输出 240 tokens，调用完成；第一条数学
要点却因提取时丢失共轭横线而错误。因此接入成功不等于数学问答验收成功。
后续增加了 [公式风险检查与生成拦截](formula-quality-guide.md)，没有代充值或核对真实账单。
2026-10-15 再增加 [已核对公式结论选择](reviewed-formula-guide.md)，独立于原始回答，
目前仅一条来源绑定的助手视觉核对注释；新增流程的真实模型选择仍待验收。
另已增加 [默认免费的六题评测脚本](answer-evaluation-guide.md)，真实整轮必须明确开启收费开关。

## 1. 这一层的作用

新增 `POST /documents/{document_id}/answer`，自动执行：

1. 从指定已解析文档检索，验证原文件、解析数据和字符来源。
2. 按字符预算生成带 E1/E2 等编号的证据包。
3. 先检查证据页公式提取风险；风险/无法检查时本地拒答。无标记时才把问题和
   收录的证据发到固定的 DeepSeek 官方地址，生成结构化 JSON 草稿。
4. 用已有的回答检查模块验证格式、编号归属和连续字面引文。
5. 模型调用结束后再检查当前来源/证据包；版本改变就丢弃回答。

原来的搜索、context 和 check-answer 不调用模型，行为保持兼容。主工作台 `/`
仍是检索页面；本轮从 `/docs` 验收回答接口，不增加隐式收费的搜索按钮。

## 2. 改动与目的

| 文件 | 作用 |
|---|---|
| src/deepseek_client.py | 不可变密钥配置、官方 HTTP 请求、调用次数/并发保护、限长读取和脱敏错误 |
| src/answer_generation.py | 问题＋证据的提示词、模型草稿生成、引用检查、无证据时跳过模型 |
| src/api_models.py | 明确外发许可、回答响应、token 用量的数据契约 |
| src/api.py | 新接口与每个应用独立的模型配置/调用计数，调用后重新核对来源 |
| src/answer_check.py | 将证据包校验函数公开给生成模块复用，原有字面核验规则不变 |
| tests/test_deepseek_client.py | 配置、请求、限额、异常/不重试与响应解析测试 |
| tests/test_answer_generation.py | 生成编排、拒答分支、错误引用与语义局限测试 |
| tests/test_answer_api.py | 上传→解析→模拟生成→引用来源的完整接口流程测试 |
| tests/test_web.py | 接口总数从 10 更新为 11，工作台交互不变 |

复用已有 httpx，不安装 OpenAI SDK，不改变 requirements/锁文件。API 格式兼容
不代表发给 OpenAI：网络地址固定为 `https://api.deepseek.com/chat/completions`。
客户端不能通过接口传入密钥、地址、服务器路径或自造证据。

## 3. 密钥配置：只在自己的 PowerShell 操作

不要把真实密钥发到聊天、截图、Git、网页、API 请求体或普通脚本中。
`.env.example` 只是参考，项目仍不自动读取 `.env`。
当前采用进程环境变量，不保存真实密钥文件。

先在原来运行服务的终端按 Ctrl+C 停止服务，然后在项目根目录 `D:\MathScope项目`
执行以下代码。`Read-Host -AsSecureString` 会隐藏粘贴内容，密钥不作为命令文字写入历史。
下面没有真实密钥，不需要替换代码里的占位符。

```powershell
$deepseekSecret = Read-Host "请粘贴 DeepSeek API 密钥（不要截图）" -AsSecureString
$deepseekPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($deepseekSecret)
try {
    $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($deepseekPointer)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($deepseekPointer)
    $deepseekSecret.Dispose()
    Remove-Variable deepseekSecret, deepseekPointer
}

$env:MATHSCOPE_DEEPSEEK_ENABLED = "1"
$env:MATHSCOPE_DEEPSEEK_MAX_CALLS = "3"
.\.venv\Scripts\python.exe -m uvicorn src.api:app --host 127.0.0.1 --port 8000
```

首次付费验收不用 `--reload`，也不加多 workers，避免自动重启/多进程重置或分别
计算次数。如果 8000 被其他进程占用，先确认并停止自己已有的服务，不要随意杀进程。

这里安全输入不等于密钥永远加密：运行中的 Python 必须拿到明文凭证，环境变量仍在
该终端和子进程内存中。不要打印 `$env:DEEPSEEK_API_KEY` 或全部环境变量。
配置在创建应用时读取，修改后需重启。用 Ctrl+C 停止后可清理该终端的变量：

```powershell
Remove-Item Env:\DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:\MATHSCOPE_DEEPSEEK_ENABLED -ErrorAction SilentlyContinue
Remove-Item Env:\MATHSCOPE_DEEPSEEK_MAX_CALLS -ErrorAction SilentlyContinue
```

这只清理当前终端，不删除文档，也不会撤销平台密钥；泄露时须在 DeepSeek 平台撤销。

## 4. 第一次只发一个问题

打开 `http://127.0.0.1:8000/docs`：

1. 先运行 `GET /documents`，复制状态为 parsed 的文档编号。
2. 展开新增 `POST /documents/{document_id}/answer`，点击 Try it out。
3. 在 document_id 中填写编号，在请求体中填以下 JSON，点击一次 Execute 后等待。

```json
{
  "query": "复数三角不等式在什么条件下取等号？",
  "method": "normalized",
  "k": 10,
  "max_context_chars": 6000,
  "allow_external": true
}
```

此问题适用于原来那份 lecture，不适用于任意 PDF。`allow_external` 必须明确为
JSON 布尔值 true，不能省略、填写字符串或数字。它表示同意发送并可能收费，
不是身份验证或账户限额。不要不断点击 Execute，重复问题也会再次调用模型。

2026-10-13 公式风险保护启用后，这道题预期本地拒答、model_called=false；这不是
恢复公式后的正确答案。先按公式风险指南调用不收费的 /quality 确认新代码生效。

发送内容只有问题和本次收录的证据（含文档显示名称、物理页和字符区间）；不上传
整份 PDF，不发送本机路径、document_id、指纹、其他文档或历史对话。

在真实 Server response 中检查：

- `status=answered`：草稿生成完成且字面引用通过；查看 `check.claims` 的要点和 citations。
- `status=refused`：给出 `check.refusal_reason`；可能是模型认为证据不足，也可能是本地无可用证据。
- `status=rejected`：草稿未通过核验；`check.issues` 给出原因，`check.claims` 为空，不交付部分草稿。
- `generation.model_called=true`：已获得模型响应；`usage` 是供应商返回的实际 token 用量。
- `generation.calls_used/max_calls`：当前服务实例尝试次数；不是账户账单。
- `check.semantics_verified=false`：始终为 false，不能把 answered 或 passed 当成数学证明已验证。

确认模型要点、逐字引文与来源 PDF 对得上。检索到内容但公式提取缺失时，refused
也可能是合理结果。模型拒答不能代替人工判断，不能通过更换状态字段证明正确。
请先验收这一题，再决定是否消耗剩余两次；不要为了得到 answered 反复请求。

## 5. 费用与保护边界

当前固定选择 `deepseek-flash`，非思考模式、非流式、最多输出 2048 tokens；证据最多
6000 字符、问题最多 500 字符，整个模型消息不超过 10000 字符。字符不等于 tokens，
字符预算不含所有提示词，不能用字数直接计算账单。

截至 2026-10-13，[官方价格页](https://api-docs.deepseek.com/quick_start/pricing/)
以美元展示：Flash 高峰输入缓存未命中每百万 tokens 为 $0.30、输出为 $1.20；
非高峰为 $0.15/$0.60。仅作为示例，假设一次实际输入 6000 tokens、输出 1000 tokens，
按高峰且全部输入未命中缓存约为 $0.003，不是对当前某次请求的测量或人民币价格。
20 元通常足以开展首轮少量问题测试；仍须以自己平台的人民币账单/余额和最新价格为准。

默认关闭。启用后默认每个实例最多 10 次，环境变量可设 1～100；本指南首轮设 3。
同一实例同时只允许一个上游请求；失败、超时和截断也计入尝试次数，因为可能已收费。
没有自动重试、不跟随重定向、不继承系统代理。HTTP 连接/读写有超时，读包时另检查
累计 30 秒；这是网络阶段与读包保护，不是严格实时的总请求时间保证。
响应最大读取 128 KiB，拒绝截断、空白、非 JSON、重复 JSON 字段、非有限数字和异常用量。

次数限额是内存中的应用实例限额：重启、热重载会重置，多实例各算各的，其他应用
也可能使用同一个账户消费。**没有持久化人民币账本，不能保证整个账户消费不超过 20 元。**
别一次充值大额；在官方平台核对余额、用量及可用限额措施。平台计费与收费失败由
平台规则决定；收到错误后先查看账单，不能把错误状态理解成免费。

## 6. 错误如何看

| HTTP 状态 | 常见含义 |
|---|---|
| 200 | 流程完成；继续看 answered/refused/rejected，不能只看 HTTP 成功 |
| 404/409 | 文档缺失、未解析、来源不一致或调用期间变化；调用后才发现变化可能已收费 |
| 422 | 问题/参数/外发许可不合法；不发起模型调用 |
| 429 | 当前实例有请求进行中或次数用尽；本次不追加调用 |
| 502 | 网络/上游请求失败，或 JSON/用量不合法；不交付原始内容、不自动重试 |
| 503 | 未启用、未配置密钥，或上游权限/余额/限流异常；看脱敏 detail |
| 504 | 上游超时；可能已收费，不自动重试 |

不开启或缺少密钥不阻止原有文档与检索服务启动。有证据时 answer 返回 503；没有
完整证据则直接本地返回拒答，`model_called=false`，不发送任何内容。

## 7. 验收记录与未完成事项

接入增量新增 47 项模拟测试；后续公式风险增量新增 28 项，已核对片段增量新增
30 项，回答评测及预览绑定再新增 22 项，累计 371 项 Python 和 21 项 JavaScript 测试通过；
依赖版本检查和 pip check 通过。所有新增上游 HTTP 测试都使用 MockTransport 和
虚构密钥，不发送真实讲义、不产生费用。旧 bad-PDF 测试的 EOF 提示与原有 Starlette
弃用警告不是本次测试失败。

没有安装新依赖、修改用户 PDF/解析数据、创建 Git 提交或更改 backend-v0.1 标签。
真实密钥由用户配置；用户首次真实响应已确认模型调用链正常，但暴露了错误的
数学要点。实际费用尚未核对，公式恢复、数学回答/拒答质量、语义引用支持检查
和工作台回答展示尚未完成。本步新增测试与只读质量验收不调用付费模型。
提示词要求忽略文档指令并不构成完整提示注入防护；字面引文正确也不保证相关性、
推导正确或拒答恰当。仍仅用于本机单用户原型，不应直接开放到公网。

接口行为依据 [官方 Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/)
和 [JSON 输出说明](https://api-docs.deepseek.com/guides/json_mode/)；不是保证未来参数永不变。
