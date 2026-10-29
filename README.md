# 步衡 WalkScope

15分钟生活圈公共服务设施体检与规划研判平台。竞赛团队：野蛮人鲈鱼豪；贡献者：刘彦和、于明睿。参赛方向：开源基础软件与解决方案 / 03 行业应用平台与数据平台。

本目录是 2026-10-29 的本地私有评审源码快照。应用使用 Python FastAPI 后端与 Vue 3 / TypeScript / Vite 前端，保持原工程目录，不引入参考项目的代码、模块结构或许可证。

## 当前样例与边界

默认同济样例 `tongji-all7-small-updated-20261027` 包含 7 组、45 类设施，设施配置参考分为 55。`frontend/public/cases/tongji.json` 与对应原始报告字节一致；SHA-256 为 `9d944fb334035df407c1a4fb741a4e4a2859ba983e14cf20391ce2aa821c17cf`。保存数据表示历史采集结果，不表示当前设施完整普查，也不能替代规划实地调查。

无密钥时可以打开保存的样例、读取报告、加载完整六章报告初稿并导出可编辑 Word；报告事实问答有无模型回退路径。自由模型改写和模型规划解读需要本人配置有效的 `DEEPSEEK_API_KEY`；界面保留需要密钥的提示，不能将无密钥模式称为真实模型验证。报告章节移除、恢复等结构操作按现有规则处理。百度道路底图需要浏览器类型的 `VITE_BAIDU_MAP_AK`；未配置或加载失败时使用无道路底图的空间示意。新的地址检索、设施与路径采集另需服务端 `BAIDU_MAP_AK`，并会产生外部服务请求和额度消耗。

## 安装

复验环境：Python 3.12.14、Node 24.22.1。以下命令从本目录执行。安装需要软件源网络访问；本次材料整理复用了已有依赖环境，没有重新执行联网安装，也未验证全新机器安装。

```sh
python3.12 tools/unpack_offline_samples.py
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
cd frontend
npm ci
cd ..
cp .env.example .env
cp frontend/.env.example frontend/.env.local
```

`.env` 和 `.env.local` 的密钥默认留空即可查看保存结果。不要将密钥写入源码、报告、截图或前端 `VITE_` 变量；只有浏览器地图 AK 可以使用其专用 `VITE_BAIDU_MAP_AK`。

## 启动

在两个终端分别激活同一 Python 环境，从本目录执行：

```sh
python tools/dev.py backend
```

```sh
python tools/dev.py frontend
```

浏览器打开 `http://127.0.0.1:5173`，后端地址为 `http://127.0.0.1:8768`。后端保持单进程；重启会丢失运行或暂停任务的执行栈，已经保存的报告仍可读取。`python tools/dev.py check` 只检查配置是否存在，不访问外部服务。

## 验证

```sh
python -m pytest
python tools/verify_handoff.py
cd frontend
npm test
npm run build
```

本次在已有依赖环境中复验：后端 825 通过、3 项显式真实调用测试跳过；前端 236 通过，生产构建通过。离线冒烟通过，未发起供应商请求。普通测试使用本地固定数据与模拟服务。三项百度真实调用测试默认跳过，需要显式开启；评审复验不要开启。`scripts/*live.py`、部分 `tools/` 执行器和历史 `verify/` 工具可调用外部服务，不属于无密钥离线验收命令。`tools/verify_handoff.py` 是本次快照的离线冒烟入口：核对当前 55 分样例、历史终态不重启任务、目录配置、六章基线及 Word 导出。

`.github/workflows/ci.yml` 定义安装、后端测试、前端测试与构建、离线冒烟检查。该文件仅是 CI 定义，本次未触发或证明远程 GitHub Actions 运行成功。

## 目录职责

| 目录 | 内容 |
| --- | --- |
| `app/` | API、体检流程、设施目录、空间分析、报告问答与 Word 草稿 |
| `frontend/` | Vue 页面、地图与报告交互、保存样例、前端测试 |
| `contracts/` | 版本化接口示例与约束 |
| `tests/` | 后端回归、保存数据重放与空间算法测试 |
| `verify/` | 独立核验脚本；含历史专项工具，具体输入以脚本为准 |
| `tools/` | 本地启动、离线冒烟、保存结果处理与有界采集工具 |
| `scripts/` | 接口合同校验与显式真实调用验收脚本 |
| `data/reports/` | 26 份不可改写的样例及回归报告 |
| `data/report_drafts/baselines/` | 完整六章初稿及不可变 v1 基线；不含个人编辑会话 |
| `data/samples/`、`执行资料/` | 测试所需样本与历史路线固定输入 |
| `docs/报告索引.json` | 最初 19 份不可变报告的回归索引 |

打包保留原始测试断言，仅将一处位于项目外的报告索引路径改为本目录内的 `docs/报告索引.json`。历史工具按原职责保留；部分工具依赖未随包提供的历史缓存或证据目录，不能视作全部可独立运行的当前入口。默认离线复验以上述命令为准。

源码沿用 [MIT License](LICENSE)，原版权归属 `Copyright (c) 2026 Yerim-mirey`。第三方地图、POI、路径及报告中派生数据的权利不因 MIT 而改变；详情见 [NOTICE](NOTICE)。本材料仅供本地私有评审准备，公开再分发前仍需核查第三方数据授权。

## 参赛介绍文档

[作品介绍](docs/步衡WalkScope_作品介绍.pdf)、[技术设计](docs/步衡WalkScope_技术设计.pdf)、[验收说明](docs/步衡WalkScope_验收说明.pdf)。文档基于实际功能与本机验证，视频按团队决定最后制作。当前仓库为私有准备仓库，正式评审前需要确认访问权限。

## 保存样例的仓库打包方式

GitHub 中52个较大历史输入与保存案例合并为 data/offline-samples.zip（约10.2 MB），避免重复提交约200 MB JSON。克隆后先运行 python tools/unpack_offline_samples.py，脚本逐项核验摘要并还原原路径；已有文件不一致时拒绝覆盖。本地竞赛包同时保留展开样例便于评审，事实字节不变。CI会先解包再测试。
