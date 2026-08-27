<h1 align="center" style="font-size: 1.75em;">OpenOPC：打造你的个人 AI 原生公司 — 自建、自营、自成长</h1>

<p align="center">
  <a href="README.md">English</a> | <b>简体中文</b>
</p>

🏗️ **自建（Self-Built）** — 全自动招募各岗位的 AI 员工，搭建组织架构。

⚙️ **自营（Self-Run）** — 全自动分派任务、驱动交接，持续朝你的目标推进。

🌱 **自成长（Self-Grown）** — 从每个任务中学习，沉淀组织记忆，交付越来越聪明。

<p align="center">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="Office UI" src="https://img.shields.io/badge/Office%20UI-React%20%2B%20Phaser-14b8a6?style=flat-square">
  <img alt="CLI and UI" src="https://img.shields.io/badge/interface-CLI%20%2B%20Office%20UI-64748b?style=flat-square">
  <img alt="License MIT" src="https://img.shields.io/badge/license-MIT-111827?style=flat-square">
  <a href="https://github.com/HKUDS/.github/blob/main/profile/README.md"><img src="https://img.shields.io/badge/Feishu-Group-E9DBFC?style=flat-square&logo=feishu&logoColor=white" alt="Feishu" /></a>
  <a href="https://github.com/HKUDS/.github/blob/main/profile/README.md"><img src="https://img.shields.io/badge/WeChat-Group-C5EAB4?style=flat-square&logo=wechat&logoColor=white" alt="WeChat" /></a>
</p>

![OpenOPC hero banner](docs/assets/chat.png)

## 新闻

- **2026 年 8 月 26 日 — 接入 Jiuwen 与 JiuwenSwarm：** Task 与 Company 模式均可将 Jiuwen 用作单 Agent，或将 JiuwenSwarm 用作一个不透明 Team。[查看配置与角色规则](#jiuwen-integration)。
- **2026 年 7 月 14 日 — Company 运行更可靠：** Company 会话可更顺畅地恢复和继续，同时保留 Agent 身份、角色上下文、委派与审核进度。
- **2026 年 7 月 13 日 — Office UI 更流畅：** 实时更新和聊天滚动性能提升，长时间运行的项目更顺畅。
- **2026 年 7 月 8 日 — 审批更智能：** 会话授权可持续生效，低风险操作自动流转，待处理决策可随时继续。

## 目录

- [何时使用 OpenOPC](#何时使用-openopc)
- [演示](#演示)
- [OpenOPC 如何工作](#openopc-如何工作)
- [快速开始](#快速开始)
- [Office UI 指南](#office-ui-指南)
- [CLI 指南](#cli-指南)
- [配置](#配置)
- [Jiuwen / JiuwenSwarm](#jiuwen-integration)
- [生态与分享](#生态与分享)
- [路线图](#路线图)
- [致谢](#致谢)

## 何时使用 OpenOPC

**OpenOPC** 覆盖九大核心垂直领域 — 从 AI 开发、软件工程到金融、销售、媒体、电商与教育。无论哪个行业，OpenOPC 都会组建合适的团队并端到端交付。

<table>
  <tr>
    <td width="33%" valign="top">
      <br><strong>🤖 AI 技术与研究</strong>
      <br><sub>模型训练与评估、Agent 开发、LLM 应用与 AI 基础设施</sub>
    </td>
    <td width="33%" valign="top">
      <br><strong>💻 软件开发</strong>
      <br><sub>Android 应用、SaaS MVP、网站、小程序与游戏开发</sub>
    </td>
    <td width="33%" valign="top">
      <br><strong>📈 金融投资</strong>
      <br><sub>投资备忘录、市场图谱、尽职调查与投决会材料</sub>
    </td>
  </tr>
  <tr>
    <td valign="top">
      <strong>🚀 销售增长</strong>
      <br><sub>外呼销售、交易策略、方案书与渠道拓展</sub>
    </td>
    <td valign="top">
      <strong>🎬 内容与媒体</strong>
      <br><sub>视频制作、短视频内容、脚本、分镜与多平台剪辑</sub>
    </td>
    <td valign="top">
      <strong>🤝 行业助理</strong>
      <br><sub>客服、房产、法律咨询、HR 入职、零售等场景的 Copilot</sub>
    </td>
  </tr>
  <tr>
    <td valign="top">
      <strong>🧾 会计与财务</strong>
      <br><sub>记账、财务报告、税务合规、预算与风险审查</sub>
    </td>
    <td valign="top">
      <strong>🛍️ 品牌与电商</strong>
      <br><sub>品牌规划、选品、店铺运营、用户增长与留存</sub>
    </td>
    <td valign="top">
      <strong>🎓 教育与培训</strong>
      <br><sub>课程设计、知识库、学员管理与内容生产</sub>
    </td>
  </tr>
</table>

## 演示

<table>
  <tr>
    <td width="33%" align="center" valign="top">
      <a href="https://youtu.be/XqQeTt6XvPQ">
        <img src="https://img.youtube.com/vi/XqQeTt6XvPQ/maxresdefault.jpg" alt="OpenOPC 视频制作演示" width="100%">
      </a>
      <br><br>
      <strong>🎬 视频制作</strong>
    </td>
    <td width="33%" align="center" valign="top">
      <a href="https://drive.google.com/drive/folders/1T1Nl6CCE-cmbGy6sKrYML7_UnP8XID88?usp=drive_link">
        <img src="docs/assets/vc-research-package.svg" alt="OpenOPC VC 投资研究演示" width="100%">
      </a>
      <br><br>
      <strong>📈 投资研究</strong>
    </td>
    <td width="33%" align="center" valign="top">
      <a href="https://youtu.be/SVc9BvE5ohY">
        <img src="https://img.youtube.com/vi/SVc9BvE5ohY/maxresdefault.jpg" alt="OpenOPC 游戏原型演示" width="100%">
      </a>
      <br><br>
      <strong>🎮 游戏原型</strong>
    </td>
  </tr>
</table>

## OpenOPC 如何工作

OpenOPC 围绕复杂的真实任务组建一家 AI 公司 — 通过三个紧密耦合的机制：**自建**负责组织配员，**自营**负责执行工作，**自成长**负责从结果中学习。

<p align="center">
  <img src="docs/assets/video.png" alt="一家 OpenOPC 公司：角色、汇报关系，以及每个角色配备的员工" width="100%">
</p>

**1. 自建 — 为组织配员**

在任何工作开始之前，必须先把合适的人放到合适的位置。给定一个目标，OpenOPC 会：

- 🌿 起草组织架构图 — 从任务需求推导出所需的角色与汇报结构。
- 🎯 填补每个角色 — 由招聘 Agent 在「复用现有员工（带着以往项目塑造的经验）」与「从人才池中招募新人」之间做出选择。

💡 有经验的员工携带积累的上下文；当角色需要时，新员工则提供一张白纸。

**⚙️ 2. 自营 — 执行工作**

团队组建完成后，自营机制协调成员产出最终交付物。核心挑战不在于单纯执行，而在于不确定性下的高效协作，具体体现为两个问题。

🔀 动态协作编排。真实工作无法完全提前规划。OpenOPC 通过工作项状态机来解决，每个工作项所处的阶段决定：

- 📋 它在看板的哪一列 — 处于工作流的哪个位置。
- 👑 它的负责人 — 该阶段由哪个角色负责。
- ✅ 它的可执行性 — 是否已经具备推进条件。

管理者负责拆解工作项、分派并评审结果 — 接受、返工或上报 — 覆盖五种模式：执行（execute）、委派（delegate）、评审（review）、集成（integrate）与返工（rework）。拆解定义了一个依赖 DAG，因此：

- ⚡ 相互独立的工作项并行推进。
- ⏳ 有依赖的工作项等待前置项完成。

🔗 依赖解除与驳回都作为结构化的阶段转换传播，消除了临时的人为协调。

🛡️ 处理运行中途出现的阻塞。并非所有障碍都能提前预见。OpenOPC 在两个层面解决：

- 💬 团队内部 — 一条阻塞消息会暂停发送者，并激活最适合解决该问题的角色。
- 📡 团队之外 — 当阻塞超出团队权限时，运行时会上报给人类所有者，在真正需要时引入人类判断。

🖥️ 看板与办公室视图实时呈现这一编排过程。

**🌱 3. 自成长 — 从运行中学习**

执行产生原始经验；自成长把它转化为持久的改进，遵循两条原则。

🏅 把结果归因到正确的角色。把功劳记给整个公司学不到任何东西。因此 OpenOPC：

- 🔍 将用户反馈解析为针对每位员工的评估。
- 🎯 只更新负责了相关工作项的角色 — 功与过都落到应得之处。

📖 把执行轨迹提炼为知识。执行轨迹噪声太大，无法直接学习。因此 OpenOPC：
- 💡 把每个角色的任务提炼为高信号的经验教训，存入其私有经验档案。
- 📚 把反复出现的经验提升为共享的作业手册（playbook），新员工从入职起即可继承 — 让组织知识随时间复利增长。

<details>
<summary><strong>这些机制如何对应到 UI</strong></summary>

- `Org -> Team` 编辑公司架构与角色。
- `Org -> Employees` 为空缺角色招募人才。
- `Team Roster -> Deploy` 把已录用的员工变成办公室中可见的 Agent。
- Workspace 输入框可选择 Task 模式的执行 Agent。
- 角色检查器可为 Company 模式的角色设置运行时策略与偏好的外部 Agent。
- 执行期间，Workspace 的 `Agents` 页签与 Execution Progress 面板会显示哪个角色处于活动状态、它负责哪个工作项、以及由哪个执行 Agent 完成具体工作。
</details>

## 快速开始

推荐使用 `uv` 来安装 OpenOPC。它可以安装/管理 Python、创建项目虚拟环境，并在该环境中运行命令，而不会把 OpenOPC 的依赖混入全局 Python。

OpenOPC 要求 Python `>=3.10`；下面的示例使用 Python `3.12`。

对于直接的一次性工作，OpenOPC 还提供 Task 模式 — 一个类 LobeChat 的单 Agent 工作台，可使用 OpenOPC Native、Codex、Claude Code、Cursor 或 OpenCode。

<details open>
<summary><strong>推荐：uv 环境搭建</strong></summary>

**macOS**

```bash
# 使用 Homebrew 安装 uv，或使用官方独立安装脚本。
brew install uv
# curl -LsSf https://astral.sh/uv/install.sh | sh

cd /path/to/OpenOPC
uv python install 3.12
uv venv --python 3.12
source .venv/bin/activate
```

**Linux**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"

cd /path/to/OpenOPC
uv python install 3.12
uv venv --python 3.12
source .venv/bin/activate
```

**Windows PowerShell**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

cd C:\path\to\OpenOPC
uv python install 3.12
uv venv --python 3.12
.\.venv\Scripts\Activate.ps1
```

**Windows 命令提示符**

```bat
winget install --id=astral-sh.uv -e
:: 或在 cmd 中运行独立安装脚本：
:: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

cd C:\path\to\OpenOPC
uv python install 3.12
uv venv --python 3.12
.venv\Scripts\activate.bat
```
</details>

```bash
# 将 OpenOPC 安装到 uv 管理的环境中
uv pip install -e .

# 可选但推荐：安装浏览器工具所需的 Chromium
uv run python -m playwright install chromium

# 初始化本地配置、记忆、技能、项目与工作区目录
uv run opc init

# 在 .opc/config/llm_config.yaml 中填入 API key，
# 或配置 llm.api_key_env 指定的环境变量。

# 启动浏览器 UI
uv run opc ui
```

默认打开 `http://localhost:8765`。

```bash
# 交互式 CLI
uv run opc chat -p demo

# 一次性 Task 模式
uv run opc chat -p demo --mode task --agent codex "Refactor this module and run focused tests"

# 使用内置 Corporate 架构的 Company 模式
uv run opc chat -p demo --mode company --company-profile corporate "Plan, implement, review, and document this feature"

# 非交互脚本 / CI 风格用法
uv run opc exec -p demo --mode task --agent native --json "Summarize the current repo status"
```

<details>
<summary><strong>安装说明</strong></summary>

- Python：`>=3.10`。当前必需依赖并非全部提供兼容 Python 3.9 的版本。
- 本地开发与发布测试推荐使用 `uv`。如果你偏好经典 pip，请创建并激活一个 Python `>=3.10` 的虚拟环境，然后运行 `python -m pip install -e .`。
- 如果虚拟环境激活被阻止，可以不激活，直接用 `uv run ...` 运行命令。
- 关于其他包管理器与托管 Python 的细节，参见官方 [`uv` 安装文档](https://docs.astral.sh/uv/getting-started/installation/) 与 [Python 管理文档](https://docs.astral.sh/uv/guides/install-python/)。
- Node.js：需要构建 Office UI 前端时要求 `>=18`。
- `opc ui` 会自动安装缺失的 `aiohttp` / `aiosqlite`，并在需要时自动构建前端。
- 如果你尚未安装外部 Agent CLI，运行 `opc init --no-external-agent-preflight` 可跳过首次运行的外部 Agent 检查。
- 浏览器工具基于原生 Playwright。在让 Agent 浏览网页之前，先用 `python -m playwright install chromium` 安装 Chromium。
</details>

<details>
<summary><b>开发环境搭建（从源码构建）</b></summary>

```bash
python -m pip install -e .
python -m pytest

cd opc/plugins/office_ui/frontend_src
npm install
npm run typecheck
npm run build
```

前端构建产物从 `opc/plugins/office_ui/frontend_dist/` 提供服务。
</details>

## Office UI 指南

<details>
<summary><b>展开 Office UI 指南 — 视觉导览、工作台、Company 模式、看板、办公室、组织</b></summary>

启动方式：

```bash
opc ui
opc ui --port 9000 --project demo
opc ui --rebuild
```

### 视觉导览

横向滚动浏览 Office UI 演示。每张截图都附有简短的说明文字。

<div style="overflow-x:auto; padding:8px 0 18px;">
  <div style="display:flex; gap:18px; min-width:5520px;">
    <figure style="flex:0 0 900px; width:900px; margin:0;">
      <img src="docs/assets/fig1.png" alt="Workspace 的项目、聊天、模式、组织与 Agent 控件" width="900">
      <figcaption><strong>工作台与初始设置。</strong>选择或创建项目，点击 <code>New Chat</code>，然后选择 <code>Company</code> 或 <code>Task</code> 以及对应的组织或 Agent。在 Company 模式下，可以指定角色员工与执行 Agent，也可以让 OpenOPC 自动招募。</figcaption>
    </figure>
    <figure style="flex:0 0 900px; width:900px; margin:0;">
      <img src="docs/assets/fig2.png" alt="Execution Progress 面板显示角色状态与执行记录" width="900">
      <figcaption><strong>执行进度。</strong>跟踪每个角色的状态，点击角色或工作项即可查看详细的执行记录、工具活动、交接、评审与运行时元数据。</figcaption>
    </figure>
    <figure style="flex:0 0 900px; width:900px; margin:0;">
      <img src="docs/assets/fig3.png" alt="看板展示 Agent 的工作项与状态" width="900">
      <figcaption><strong>看板。</strong>监督每个 Agent 的具体任务与工作项，观察它们在规划、执行、评审、阻塞与完成之间流转。</figcaption>
    </figure>
    <figure style="flex:0 0 900px; width:900px; margin:0;">
      <img src="docs/assets/fig4.png" alt="组织编辑器，可调整现有组织或新建组织" width="900">
      <figcaption><strong>组织管理。</strong>调整现有组织、修改角色与汇报关系、查看运行时策略，或创建一个新组织。</figcaption>
    </figure>
    <figure style="flex:0 0 900px; width:900px; margin:0;">
      <img src="docs/assets/fig5.png" alt="人才市场，可浏览并招募员工" width="900">
      <figcaption><strong>人才市场。</strong>浏览人才模板，查看候选人详情，在公司需要更多能力时把员工招募到空缺角色上。</figcaption>
    </figure>
    <figure style="flex:0 0 900px; width:900px; margin:0;">
      <img src="docs/assets/fig6.png" alt="动画办公室视图，展示每个角色正在做什么" width="900">
      <figcaption><strong>办公室视图。</strong>以动画办公室的形式观察整个组织，每个角色/Agent 都会显示状态、当前任务、正在使用的工具、座位与运行时活动。</figcaption>
    </figure>
  </div>
</div>

Office UI 有三个主要页面：

| 页面 | 在这里做什么 |
|---|---|
| **Workspace** | 主要工作界面：会话列表、看板、聊天、任务详情、角色进度、通讯与团队驾驶舱。 |
| **Office** | 可视化办公室地图：Agent 以角色形象出现，可以选中、移动、分配座位与查看详情。 |
| **Org** | 公司架构：切换 Corporate/已保存的组织、创建新组织、编辑角色、招募人才、应用架构预设、导入/导出配置。 |

### 工作台（Workspace）

Workspace 页面是默认界面。

| 区域 | 关注点 |
|---|---|
| 左侧边栏 | 项目会话、活动、未读计数与新建聊天。 |
| 中间看板 | 看板卡片。Task 模式下，一张卡片通常对应一个任务型聊天会话。Company 模式下，看板跟随所选的运行时会话，展示已委派的工作项。 |
| 右侧面板 | 上下文面板，包含 `Chat`、`Agents`、`Info`、`Comms`、`Team` 等页签。工作运行期间可以折叠、调整大小或最大化。 |
| 输入框 | 发送消息、附加文件、选择模式、选择公司架构；在 Task 模式下选择执行 Agent。 |

### 从 UI 开始工作

1. 在顶部项目选择器中创建或选择一个项目。
2. 在 Workspace 中点击 `New Chat`。
3. 在输入框中选择 `Task` 或 `Company`。
4. Task 模式下选择 Agent：`OpenOPC Native`、`Codex`、`Claude Code`、`Cursor` 或 `OpenCode`。
5. Company 模式下选择 `Corporate` 或一个已保存的组织架构。
6. 发送任务简报。

第一条消息发出后，该聊天的模式与任务 Agent 即被锁定。若需换用其他模式，可通过锁定模式的弹出提示在新聊天中继续。

### UI 中的 Company 模式

Company 模式把一份简报变成一个运行时会话加一组由角色负责的工作项。

| 页签 | 展示内容 |
|---|---|
| `Chat` | 父级对话、最终回复、运行时进度卡片、检查点回复、停止/继续/完成控件，以及跳转到工作项执行的链接。 |
| `Agents` | 角色汇总：活动/等待/待定/完成的角色、当前工具、角色工作项、筛选、搜索，以及详细执行进度的链接。 |
| `Info` | 状态、负责人、角色身份、员工分配、所选执行 Agent、时间信息与开发者详情。 |
| `Comms` | 角色收件箱、未读/已读/已发消息、会议、决策与最近的通讯故障。 |
| `Team` | 运行时驾驶舱：团队、座位、审批、未读通讯、恢复状态与当前运行的停止控件。 |

要查看某个角色的详细工作流，打开一个 Company 模式会话，在 `Chat` 进度卡片或 `Agents` 页签中点击角色/工作项。Execution Progress 面板会展示每个工作项及其状态、活动分区、工具进度、交接、评审对象与执行轮次元数据。

### 看板（Kanban）

- Task 模式：看板是项目级面板。可以在 `Todo` 中快速创建任务、启动任务，并从右侧面板查看每个任务。
- Company 模式：当前面板跟随所选运行时会话。卡片代表公司工作项，按照后端运行时状态在规划/执行/评审/完成之间流转。
- 当运行时掌管状态时，跨状态列的手动拖拽会被有意限制。同列内重新排序在适用时是支持的。

### 办公室（Office）

当你想以可视化方式查看运行中的团队时，使用 Office 页面。

- 点击 Agent 角色形象或列表行，查看状态、当前工具、当前任务、角色、办公室与座位。
- 使用办公室/座位控件移动 Agent。
- 子 Agent 可以显示或隐藏。
- 由员工或模板创建的 Agent 会出现在办公室中，并持久化在 `.opc/ui_state.db`。

### 组织（Org）

Org 页面是公司结构变得可运行的地方。

| 子页签 | 用途 |
|---|---|
| `Team` | 查看/编辑角色图谱、表格、角色检查器、花名册、已保存组织选择器、导出打包流程，并把已录用员工部署到办公室。 |
| `Runtime` | 调整运行时团队、座位、最终决策者、委派策略与运行时策略。Corporate 为只读；已保存的组织可编辑。 |
| `Architecture` | 浏览内置架构预设、预览/应用包、管理已安装的包、导入/导出 YAML。 |
| `Employees` | 搜索人才模板、查看详情、招募到空缺角色、为公司配员。 |

创建新公司：打开 `Org`，点击 `New organization`，输入名称，添加至少两名带职责与汇报关系的成员，检查并创建。OpenOPC 会自动保存，并把输入框切换为 `Company / <你的组织>`。

招募：先导入人才模板，然后打开 `Org -> Employees`，搜索模板，点击 `Hire`，选择一个空缺角色；若希望员工出现在 Office 页面，再从 `Team Roster` 部署。

```bash
opc talent import /path/to/agency-agents
```

<details>
<summary><strong>项目文件的位置</strong></summary>

OpenOPC 把运行时/配置状态与交付物工作区文件分开存放。

| 路径 | 含义 |
|---|---|
| `.opc/config/` | 由 `opc init` 从 `config/` 复制而来的本地配置。 |
| `.opc/memory/` | 全局与项目级 Markdown 记忆。 |
| `.opc/projects/<project>/` | 项目运行时元数据与任务存储。 |
| `.opc/ui_state.db` | Office UI 的聊天、频道与可视化 Agent 状态。 |
| `../OpenOPC_workplace/<project>/` | 默认项目工作区。Agent 应把持久的项目文件写到这里。 |
| `../OpenOPC_workplace/<project>/.opc-comms/` | Company 模式内部通讯信箱、会议与工具结果暂存区。 |

若希望配置与运行时状态放在仓库之外，设置 `OPC_HOME=/path/to/opc-home`。
</details>

</details>

## CLI 指南

<details>
<summary><b>展开 CLI 指南 — 常用命令与交互式斜杠命令</b></summary>

OpenOPC 同时提供高层的自然语言命令与更底层的 UI/服务命令。

概念上 OpenOPC 有两种执行模式：`task` 与 `company`。部分底层 CLI/服务命令仍将 `org` 作为「Company 模式 + 已保存组织架构」的兼容选择器；在 UI 中这表现为 Company 加一个架构选择。

### 常用命令

```bash
# 聊天
opc chat
opc chat -p demo --mode task --agent native "Inspect the failing tests"
opc chat -p demo --mode company --company-profile corporate "Ship this change with review"

# 可脚本化执行
opc exec -p demo --mode task --agent codex --stream-json "Run the migration check"
opc exec -p demo --mode company --company-profile corporate "Draft the research report"

# 项目生命周期
opc project list
opc project create demo
opc project switch demo

# 会话
opc session list -p demo
opc session create "New feature" -p demo --mode company
opc session send <task_id> "Continue with implementation" -p demo
opc session stop <task_id> -p demo
opc session continue <task_id> "Proceed after review" -p demo

# 运行时检查
opc runtime status -p demo
opc runtime logs <task_id> -p demo
opc work-item list -p demo
opc work-item show <work_item_id> -p demo
opc comms state <task_id> -p demo

# 招募
opc talent import /path/to/agency-agents
opc talent hire <template_id> <role_id> -p demo
```

### 交互式斜杠命令

运行 `opc chat`，然后使用斜杠命令：

```text
/status
/mode task
/mode company corporate
/agent codex
/project switch demo
/session list
/runtime --full
/logs <task_id> --full
/comms <task_id> --full
/org
/talent list
/market list
```

完整命令表见 [`docs/cli-chat-slash.md`](docs/cli-chat-slash.md)。

<details>
<summary><strong>CLI 命令分组</strong></summary>

| 分组 | 示例 |
|---|---|
| `opc project` | `list`、`show`、`create`、`switch`、`rename`、`delete --yes` |
| `opc session` | `list`、`create`、`show`、`config`、`send`、`rename`、`delete --yes`、`stop`、`continue`、`resume`、`complete` |
| `opc mode` | `show`、`set task`、`set company --profile corporate`、以及用于已保存组织公司运行的 `set org --org <id>` |
| `opc kanban` | `view`、`task create`、`task update`、`task move`、`task assign`、`task status`、`task delete --yes` |
| `opc agent` | `list`、`create`、`create-from-template`、`import-employee`、`detail`、`move`、`delete --yes` |
| `opc org` | `info`、`export`、`import`、`saved list/save/load/delete`、`role add/update/bulk-add/delete`、`policy update`、`strategy update`、`reset --yes` |
| `opc talent` | `list`、`employees`、`import`、`hire`、`scan`、`import-selected`、`employee-detail`、`import-agent` |
| `opc market` | `presets`、`browse`、`preview`、`apply-preset`、`export`、`install`、`list`、`uninstall --yes` |
| `opc runtime` | `status`、`checkpoints`、`logs`、`run` |
| `opc channels` | `status`、`login`、`start`、`stop` |

大多数服务类命令都支持 `--project/-p` 与 `--json`。

对于已保存的组织架构，部分 CLI/服务命令目前将 `org` 作为兼容选择器使用，尽管概念上的运行时仍是 Company 模式：

```bash
opc exec -p demo --mode org --org hku_research_lab "Draft the research report"
opc session create "Research sprint" -p demo --mode org --org hku_research_lab
```
</details>

</details>

## 配置

在仓库根目录运行一次 `opc init`。它会创建 `.opc/`、从 `config/` 复制模板配置、创建记忆/技能/日志目录，并可选地创建第一个项目。

项目本地的 `.opc/config` 可以选择本地可执行文件、远程端点和凭据来源，因此 OpenOPC 在加载已有项目配置前会请求工作区信任；非交互命令会安全失败。检查配置后可运行 `opc trust add /path/to/workspace`。信任同时绑定安全相关配置源及其规范化生效权限的指纹，因此 system、LLM、Agent 或 channel 配置变化后必须重新授权；旧版仅绑定路径的信任记录也需要更新一次。可用 `opc trust list` 查看、用 `opc trust remove /path/to/workspace` 撤销。由 `opc init` 新建的工作区会在创建时自动信任。

<a id="jiuwen-integration"></a>
<details>
<summary><strong>Jiuwen / JiuwenSwarm — 配置与角色规则</strong></summary>

Task 和 Company 模式都提供两个选项：

- **Jiuwen** 是普通单 Agent，和 Codex、Claude Code 一样，只处理选中的任务或角色。
- **JiuwenSwarm Team** 是一个不透明的外部 Team。OpenOPC 只与整个 Team 协作，不管理 Jiuwen 内部的 Agent。

### 配置

Jiuwen 读取 `~/.jiuwenswarm/config/.env`，不会自动读取 OpenOPC 的 `.opc/config/llm_config.yaml`。可以复用同一套提供方凭据，但模型名必须支持 Jiuwen 的 Agent 请求。例如火山方舟 Agent Plan 使用：

```dotenv
API_BASE="https://ark.cn-beijing.volces.com/api/plan/v3"
API_KEY="<你的 API key>"
MODEL_NAME="ark-code-latest"
MODEL_PROVIDER="OpenAI"
```

安装适配器、启动 Gateway，并检查两个选项：

```bash
pip install -e '.[jiuwen]'
chmod 600 ~/.jiuwenswarm/config/.env
jiuwenswarm-start --restart default
opc agents preflight jiuwen
opc agents preflight jiuwenswarm
opc exec --mode task --agent jiuwen "完成这个任务"
opc exec --mode task --agent jiuwenswarm "由一个 Team 完成这个任务"
```

默认 Gateway 地址是 `ws://127.0.0.1:19001/tui`。只有 Jiuwen 运行在其他地址时，才需要修改 `.opc/config/agent_config.yaml` 中的 `gateway_url`。

### Company 模式的角色选择

可以在 Staffing / Recruitment Review 中选择执行 Agent，也可以进入 **Company → Org → 角色 → Runtime** 设置。

- CTO 选择 **Jiuwen**：只有 CTO 使用 Jiuwen；CTO 的下级仍是独立的 OpenOPC 角色，继续单独招聘和选择 Agent。
- CTO 选择 **JiuwenSwarm Team**：CTO 及其所有下级合并为一个 Team。组织图仍显示这些角色，但下级选项会锁定，OpenOPC 不再为该子树招聘或分配员工。CEO 只向 CTO Team 边界派单，并接收一份 Team 结果。
- CEO 选择 **JiuwenSwarm Team**：CEO 和整个公司子树合并为一个 Team；下级不能再单独选择 Agent，也不会创建嵌套 Team。
- COO、CTO、CMO 等同级管理者分别选择 Team：它们会成为多个独立 Team，由共同的上级协调和审核。
- 不选择 Team：保持普通 Company 模式，照常招聘并按角色执行。

上级会收到根据覆盖角色、Skill、工具和交付物动态生成的能力摘要；Jiuwen 自行安排 Team 内部如何分工。

</details>

<details>
<summary><b>展开配置 — 配置文件、LLM 密钥、外部 Agent、频道、浏览器/MCP、故障排查</b></summary>

| 文件 | 用途 |
|---|---|
| `.opc/config/llm_config.yaml` | 默认模型、兼容 LiteLLM/OpenRouter 的 API base、API key、环境变量间接引用、路由、回退、temperature、token 限制。 |
| `.opc/config/system_config.yaml` | 运行时行为、浏览器工具、原生运行时、压缩、验证、权限、沙箱与安全设置。 |
| `.opc/config/agent_config.yaml` | 外部 Agent 命令路径、优先顺序、模型参数、会话模式、超时、审批模式与原生子 Agent 配置。 |
| `.opc/config/channel_config.yaml` | 外部消息提供方与凭据。入站发送者列表默认拒绝。 |
| `.opc/config/company_corporate_config.yaml` | 内置 Corporate 公司架构模板。 |
| `.opc/config/company_orgs/org_<id>_config.yaml` | Company 模式使用的自定义公司架构。 |
| `.opc/config/org_index.yaml` | 当前生效的已保存公司架构选择器。 |

### LLM 密钥与本地模型支持

运行 `opc init` 后，编辑仓库本地 OPC home 中的 `.opc/config/llm_config.yaml`（或 `$OPC_HOME/config/llm_config.yaml`）。OpenOPC 同时支持云端 LLM（OpenAI、Anthropic、OpenRouter）与无 API Key 的**本地模型**（Ollama、vLLM、LM Studio、LocalAI、Llama.cpp、TGI）：

```yaml
llm:
  default_model: "ollama/llama3.3"         # 或 "vllm/meta-llama-3.1-8b-instruct"、"openai/gpt-4o"
  api_base: "http://localhost:11434"        # 本地模型前缀将自动解析默认 Base URL
  api_key: ""                               # 本地节点免 API Key 运行！

  max_tokens: 32768         # 每次请求的最大输出 token
  is_local: true            # 可选的显式本地运行标志
```

你也可以直接在浏览器 Office UI 顶栏点击 **LLM & 本地模型设置弹窗（`🤖` 按钮）** 进行可视配置。

详细配置指南请参见 [`docs/LOCAL_MODELS.md`](docs/LOCAL_MODELS.md)。

然后用 `opc status` 验证。

如果不想把云端密钥存在文件里，可以将 `api_key` 留空，并把 `api_key_env` 设置为持有密钥的环境变量名（例如 `api_key_env: "OPENROUTER_API_KEY"`）。

### 审批与 Agent 权限

`.opc/config/system_config.yaml` 的 `autonomy` 部分控制 Agent 无需询问即可执行多少操作。关键旋钮是 `max_auto_approve_risk` — 可被自动批准的最高风险等级：

```yaml
autonomy:
  max_auto_approve_risk: medium   # low | medium | high | critical
  allow_native_tool_auto_approval: true
  tool_first_use_approval: true   # 每个工具首次使用时总是询问
```

每次原生工具调用在运行前都会做风险分级：已知的破坏性命令（`rm -rf`、`drop table`、force-push 等）与敏感关键词（凭据、部署等）为 `high`/`critical`，总是上报给人类；白名单中的安全前缀（`ls`、`git status` 等）为 `low`；其余为 `medium`，在自动批准前会经过 LLM 审查。

- `medium`（默认）：平衡 — 普通命令无提示运行；危险命令上报。
- `low`：严格 — 不在安全白名单中的任何操作都需要审批。推荐用于共享或生产机器。
- `high`/`critical`：宽松 — 仅用于可随时丢弃的沙箱。

每个工具首次使用时总会提示（除非该工具在 `tool_approval_exemptions` 中），你的「始终允许」选择会累积到项目级白名单。

### 外部 Agent

Task 模式可以显式选择执行 Agent：

```bash
opc chat -p demo --mode task --agent codex "Implement the change"
```

可用值有 `native`、`codex`、`claude_code`、`cursor`、`opencode`、`jiuwen` 与 `jiuwenswarm`。参见 [Jiuwen / JiuwenSwarm 配置与角色规则](#jiuwen-integration)。

### 飞书接入

```bash
pip install -e .[channels-feishu]
opc init
opc channels login feishu
```

编辑 `.opc/config/channel_config.yaml`：

```yaml
channels:
  feishu:
    enabled: true
    app_id: "cli_xxx"
    app_secret: "..."
    encrypt_key: ""
    verification_token: ""
    react_emoji: THUMBSUP
    allow_from:
      - "ou_xxx"
```

然后：

```bash
opc channels status
opc channels start -p demo
# 或运行常驻引擎 + 频道运行时：
opc run -p demo
```

飞书使用 `lark-oapi` WebSocket 客户端。`app_id` 与 `app_secret` 为必填；`encrypt_key` 与 `verification_token` 为可选，除非你的租户/应用配置要求。请保持 `allow_from` 显式配置；空列表会拒绝所有入站消息。

<details>
<summary><strong>其他频道提供方</strong></summary>

| 提供方 | 安装 extra | 运行方式 | 必填字段 |
|---|---|---|---|
| Telegram | `channels-telegram` | polling | `token` |
| Slack | `channels-slack` | socket | `bot_token`、`app_token` |
| Discord | `channels-discord` | socket | `token` |
| 钉钉 | `channels-dingtalk` | socket | `client_id`、`client_secret` |
| 邮件 | `channels-email` | polling | IMAP/SMTP 字段、`consent_granted` |
| Matrix | `channels-matrix` | sync/polling | `homeserver`、`access_token`、`user_id` |
| QQ | `channels-qq` | socket | `app_id`、`secret` |
| WhatsApp | `channels-whatsapp` | bridge | `bridge_url` |
| Mochat | `channels-mochat` | bridge | `base_url`、`claw_token`、`agent_user_id` |

常用命令：

```bash
opc channels login slack
opc channels status
opc channels start -p demo
opc channels stop
opc run -p demo
```

参见 [`docs/channels.md`](docs/channels.md) 与 [`docs/channel-bridges.md`](docs/channel-bridges.md)。
</details>

<details>
<summary><strong>浏览器工具与 MCP 服务器</strong></summary>

浏览器工具：

```bash
python -m playwright install chromium
```

在 `.opc/config/system_config.yaml` 中配置启动行为：

```yaml
system:
  browser:
    mode: embedded   # embedded | chrome | auto
    headless: true
    chrome_channel: chrome
    user_data_dir: ""
```

原生浏览器工具包括 `browser_navigate`、`browser_snapshot`、`browser_click`、`browser_type`、`browser_wait_for`、`browser_scroll`、`browser_select_option`、`browser_evaluate`、`browser_take_screenshot` 与 `browser_close`。

MCP 服务器可添加到 `system_config.yaml` 的 `mcp_servers` 下。本地服务器使用 stdio 命令；远程服务器使用 HTTP/SSE 风格的 URL。发现的工具会以服务器前缀注册，避免命名冲突。
</details>

### 故障排查

<details>
<summary><strong>Office UI 无法打开或界面陈旧</strong></summary>

```bash
opc ui --rebuild
```

如果浏览器仍显示陈旧的 UI 状态，强制刷新页面。如果之前的进程在运行中途崩溃，先重启 `opc ui` 以释放内存中的锁。
</details>

<details>
<summary><strong>任务看起来卡住了</strong></summary>

先重启服务器并强制刷新浏览器。如果持久化的任务状态仍然异常，使用重置工具：

```bash
python scripts/reset_stuck_task.py --project <project> --session <session_id> --apply
python scripts/reset_stuck_task.py --all --apply
```
</details>

<details>
<summary><strong>外部 Agent 不可用</strong></summary>

运行：

```bash
opc status
```

检查 `.opc/config/agent_config.yaml` 中的命令名，例如 `codex`、`claude`、`cursor-agent`、`opencode` 与 `jiuwenswarm`。Gateway 模式还需确认 `jiuwenswarm-start` 服务已启动，并运行 `opc agents preflight jiuwen`。
</details>

<details>
<summary><strong>频道提供方收不到消息</strong></summary>

检查：

- 已安装对应的 extra，例如 `pip install -e .[channels-feishu]`。
- 该提供方为 `enabled: true`。
- 必填凭据已填写。
- `allow_from` 包含你期望的发送者 ID。
- `opc channels status` 显示该提供方已配置且可用。
</details>

</details>

## 生态与分享

OpenOPC 构建的一切都归你所有，可以保留、复用与分享 — 组织、员工、人才模板、技能与频道都只是文件。你可以导入一个流行的人才库、跨项目复用一个团队，或者把整个公司打包成可分享的 `.opcpkg`。

```bash
# 从人才库（例如 agency-agents）招募到某个角色
opc talent import /path/to/agency-agents
opc talent hire <template_id> <role_id> -p demo

# 复用或分享整个组织
opc org export --json > my-org.yaml
opc market export --id hku_lab --name "HKU Lab" --output-dir packages
opc market install packages/hku_lab.opcpkg
```

## 路线图

OpenOPC 正在快速迭代。以下领域反映当前的开发重点 — 每一项都源自早期使用中发现的真实缺口。

| 领域 | 计划方向 |
|---|---|
| **角色级技能** | 角色配置已支持 `skill_refs`，Org UI 目前也展示技能元数据。下一步是让用户直接在 Org 页面选择哪些技能挂载到哪些角色 — 汇入更广泛的自演化技能生态。 |
| **秘书设置** | 秘书将成长为更强的配置与记忆管家：负责 OPC 系统记忆、分析与对比项目，并为 OpenOPC YAML 配置提供引导式设置。 |
| **Company 模式频道** | 外部频道将从简单的聊天入口演进为更丰富的 Company 模式工作流 — 支持角色感知的通知、结构化审批与跨平台协作。 |
| **CLI 对齐** | CLI 目前可用，但 Office UI 仍是更完整的界面。后续工作聚焦于从终端进行组织编辑、Company 模式检查、故障恢复与长时运行时控制。 |
| **TUI** | CLI 对齐成熟后将考虑完整的终端 UI。在此期间 Office UI 仍是主要界面。 |
| **市场与预设** | 更多架构预设、可招募的人才包、导入/导出工作流，以及用于分享与发现社区组件的包市场。 |
| **运行时打磨** | 持续改进恢复、检查点、执行进度可见性与可视化文档 — 让长时间的公司运行更可观察、更有韧性。 |

## 致谢

OpenOPC 的 Agent 设计、技能结构与人才模板生态受益于多个开源项目，在此致谢：

- [openai/codex](https://github.com/openai/codex/) 启发了实用的编码 Agent 工作流与执行模式。
- [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban) 启发了以看板为中心的 Agent 工作管理与任务可见性。
- [msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents) 提供了人才模板的基础。本仓库包含的所有人才模板均导入自 `agency-agents`。
- [HKUDS/nanobot](https://github.com/HKUDS/nanobot) 启发了面向技能的 Agent 设计与 `SKILL.md` 风格的组织方式。
- [pixel-agents-hq/pixel-agents](https://github.com/pixel-agents-hq/pixel-agents) 启发了以像素动画办公室可视化 Agent 活动的方式。

---

<p align="center">
  <em> ❤️ 感谢访问 ✨ OpenOPC！</em><br><br>
  <img src="https://visitor-badge.laobi.icu/badge?page_id=HKUDS.OpenOPC&style=for-the-badge&color=00d4ff"
  alt="Views">
</p>
