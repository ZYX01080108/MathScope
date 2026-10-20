# 后端环境基线与恢复说明

记录日期：2026-10-11。参考环境为 Windows x64、CPython 3.11.9。
当前项目仍是本机单用户学习原型；这份记录不是公网部署说明，也不是 Git 发布标签。

## 1. 为什么需要两份依赖清单

requirements.txt 保留项目直接使用的 8 个包，便于理解和维护。
requirements.lock.txt 保存当前环境中 22 个包的确切版本，包含间接依赖。
例如，项目直接使用 FastAPI，但 FastAPI 还依赖 Starlette；只固定直接依赖，
重新安装时未必获得这次通过测试的同一组间接依赖。

恢复已验收环境优先使用 requirements.lock.txt，不需要同时执行两份安装命令。
以后确实需要更新依赖时，在独立环境修改、检查和运行测试，然后有意更新两份清单；
不要把遇到错误时直接升级到最新版本作为恢复步骤。

这是一份 pip 可读取的版本快照，并非工具生成的带平台解析与安装包哈希的严格锁文件。
它没有保存 wheel 文件，没有固定索引来源，没有固定 pip/setuptools，也不阻止额外
包安装。不同操作系统、Python 版本和未来的包源可用性，都可能影响恢复结果。
参考环境的 pip 为 26.2.1、setuptools 为 65.5.0，仅记录，未强制固定构建工具。

## 2. 只读检查当前环境

在项目根目录执行，不需要激活虚拟环境：

```powershell
.\.venv\Scripts\python.exe scripts/check_environment.py
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

三项检查回答不同问题：

- check_environment.py：Python 是否为 3.11，所有记录包的版本是否匹配，直接依赖
  是否都在快照里且版本一致。缺包、版本不一致或清单格式错误会返回非零退出码。
- pip check：已安装包声明的依赖关系是否满足；不保证程序行为正确。
- unittest：在这组环境中，项目已经编写的行为与边界测试是否通过；不代表没有未知问题。

检查脚本使用标准库 importlib.metadata 获取安装版本，不导入第三方业务模块。
它不会安装、卸载或升级依赖，不修改环境变量，不读取讲义，也不修复错误。
它不检查额外安装的包、安装包内容哈希、全部文件完整性或所有行为兼容性。
Python 3.11 的补丁号允许不同；本次实际验收的补丁版本为 3.11.9。

## 3. 在独立环境恢复

先准备完整项目代码和 Python 3.11；Windows 的 py 启动器应能找到该解释器。
在项目根目录运行下列命令。示例环境目录必须是尚不存在的专用目录，避免覆盖已有环境。

```powershell
if (Test-Path -LiteralPath 'D:\MathScope-env-baseline') {
    throw '目录已经存在，请选择另一个全新的环境目录。'
}
py -3.11 -m venv D:\MathScope-env-baseline
if ($LASTEXITCODE -ne 0) { throw '虚拟环境创建失败。' }

D:\MathScope-env-baseline\Scripts\python.exe -m pip install -r requirements.lock.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请先处理安装错误。' }
D:\MathScope-env-baseline\Scripts\python.exe scripts/check_environment.py
if ($LASTEXITCODE -ne 0) { throw '版本快照检查失败。' }
D:\MathScope-env-baseline\Scripts\python.exe -m pip check
if ($LASTEXITCODE -ne 0) { throw '依赖一致性检查失败。' }
D:\MathScope-env-baseline\Scripts\python.exe -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw '项目测试失败。' }
```

安装需要包源或事先准备的离线安装包。不要删除原 .venv、原始 PDF 或解析目录来验收。
独立环境可以保留，原环境不受影响；文档里的这些示例命令并未替你执行。
实际自动验收使用了另一份唯一命名的临时环境，结束后仅清理这次临时环境。

通过后可在空数据目录启动服务，沿用 README 中 MATHSCOPE_DATA_DIR 的设置方式。
不用准备 lecture 基线即可使用 /docs、上传、按编号解析与按编号检索。
默认旧 /search 的实验基线缺失时会返回 503，不代表整个服务启动失败。

## 4. 代码归档不等于数据备份

Git 当前忽略 .venv、data/raw、data/parsed、reports 和 .env。
因此，即使未来提交代码并打标签，也不会自动保存真实 PDF、上传元数据和解析结果。
当前文档也不能替代一份已经提交的代码快照。

要保留已上传文档，应在服务没有上传或解析写入时，分别备份实际配置数据根目录下的
raw/uploads 和 parsed/uploads，保留原文件名、文档编号及同编号的全部文件。
只复制 PDF 而不保留相邻上传 JSON，不是当前程序能直接识别的完整文档库。
恢复时应先验证备份，使用独立数据目录测试，不覆盖尚有价值的现有文件库。

固定开发评测还需要 lecture 原始 PDF、页面 JSONL、分块 JSONL 和相匹配的
docs/eval_manifest.json；文件指纹不一致时评测应拒绝运行。
生成的新 reports 不在 Git 中，重要实验结果需另行保存。
本次没有复制或搬迁数据，也没有自动创建数据备份。

## 5. 本次验收与版本边界

本次新增环境快照、只读检查器及 13 项检查测试，未改变检索算法、7 个业务接口、
响应模型、配置默认值或上传解析安全上限。2026-10-11 的验收结果：

- 原项目环境：22 个记录包版本一致，pip check 通过，147 项测试全部通过。
- 独立临时环境：使用 CPython 3.11.9 创建新虚拟环境，从 requirements.lock.txt
  安装依赖，版本检查和 pip check 通过，147 项测试全部通过。
- 独立临时环境已在验收结束后清理，原 .venv 没有被更新或替换。

Starlette 对旧 HTTPX 测试客户端的弃用提示仍存在，目前不是测试失败；迁移工具链
应单独验证，不能在冻结基线时悄悄升级。

本次没有 git add、commit、tag 或 push。下一步若决定保存代码版本，应先检查
待提交文件、排除数据和私密配置、运行验收，再由用户授权提交并命名标签。
这份快照解决“记录与恢复包版本”，没有完成代码发布、数据备份、跨平台验证、
多用户安全或公网部署。
