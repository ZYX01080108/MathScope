# MathScope 本地后端基线 backend-v0.1

归档日期：2026-10-11（Asia/Shanghai）。版本标识：backend-v0.1。
这是一份本机单用户 PDF 检索后端的 Git 基线，不是公网发布。

## 1. 版本包含哪些能力

- PDF 上传、随机文档编号、大小与文件头检查、元数据和 SHA256 记录。
- 按文档编号解析，保留物理页号、空文本页、分块原文与字符区间。
- 按编号进行 BM25 检索，支持原始/术语统一方案、返回数量限制与索引缓存。
- 文档列表与详情，返回磁盘处理状态，不依赖数据库。
- 可选 lecture 基线；缺失时旧 /search 为 503，不阻止文档服务启动。
- 7 个业务接口、请求校验、响应模型和自动生成的 /docs 页面。
- 应用工厂与独立配置、22 个包的依赖版本快照、只读环境检查。
- 147 项自动化测试、十题开发集的目标证据评测与数据指纹清单。

开发题不是留出测试集；两道证据不足题记录人工预期，不自动判定拒答。
PDF 数学公式可能失真。本版本未完成手写产品前端、生成式回答、自动拒答、
多文档联合检索、用户鉴权、异步解析、数据库事务或公网部署保护。

## 2. 提交、标签与备份分别是什么

提交保存当时已暂存文件的内容与历史记录。附注标签 backend-v0.1 给一个提交起
稳定的名字，并记录版本说明；继续开发产生新提交时，这个标签不自动跟随移动。
提交编号由 Git 根据内容和元数据生成，不能写进自身提交文件，所以用以下命令查询：

```powershell
git log -1 --oneline backend-v0.1
git rev-parse 'backend-v0.1^{commit}'
git show --no-patch backend-v0.1
```

这些命令只查看版本，不切换分支或修改工作区。标签是本地标签，没有推送。
Git 提交与当前项目位于同一磁盘，不是异地备份；磁盘故障仍可能同时丢失二者。

## 3. 安全取出这份代码

首先在现有项目根目录检查版本：

```powershell
git status --short
git show --stat --oneline 'backend-v0.1^{commit}'
```

希望查看或测试旧版本时，优先取到独立目录，不对现有项目使用 reset --hard、
checkout -- 或 restore，也不要把导出包解压覆盖当前工作区。

下面的示例输出路径必须全部尚不存在。示例不会自动执行：

```powershell
if ((Test-Path -LiteralPath 'D:\MathScope-backend-v0.1.zip') -or
    (Test-Path -LiteralPath 'D:\MathScope-backend-v0.1-code')) {
    throw '输出路径已存在，请选择其他路径。'
}
git archive --format=zip --output=D:/MathScope-backend-v0.1.zip backend-v0.1
if ($LASTEXITCODE -ne 0) { throw '版本导出失败。' }
Expand-Archive -LiteralPath 'D:\MathScope-backend-v0.1.zip' -DestinationPath 'D:\MathScope-backend-v0.1-code'
```

导出包含标签指向的已提交源码，但不含 .git 历史、虚拟环境、讲义或解析数据。
导出的 ZIP 只是本地代码副本；离线恢复环境还需要可安装的依赖，完整 Git 历史
备份和异地数据备份仍需另行安排。

在导出目录中，按 environment-baseline.md 创建独立 Python 3.11 环境、安装
requirements.lock.txt，并执行版本检查、pip check 和全部测试。
空库 HTTP 服务可启动；固定 lecture 开发评测需另行准备指纹匹配的数据文件。

## 4. 本次记录与未纳入的文件

本次提交合并之前逐步完成、但还未提交的后端能力，不只是本次写的发布说明。
新增和修改范围以 `git show --stat 'backend-v0.1^{commit}'` 的实际输出为准。
原有提交历史保留，没有 amend、rebase 或强制重置。

未纳入：data/raw、data/parsed、reports、.venv、真实 .env。
纳入 .env.example 只是因为它包含无敏感值的配置示例，不会自动加载。
归档前做了常见凭据特征检查，但该检查不是完整安全审计。

真实文档的备份必须包含 PDF、相邻上传 JSON 与同编号解析目录，且在没有写入时
保存一致副本。只保存 Git 标签或 PDF，不能恢复完整的已上传文档库。
本次未创建这样的数据备份，未上传代码或数据到任何远程服务。
