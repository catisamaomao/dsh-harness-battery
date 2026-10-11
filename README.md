# 鲸相 · Harness Battery

DeepSeek Harness 的行为画像插件。侧栏打开“鲸相”，选择题包、查看题目、启动独立测试，再阅读左侧证据面板与右侧鲸鱼娘立绘组成的报告。

**首版 v0.1.0 为探索性预览。** 它记录当前受控配置下的公开回答、工具动作和实际产物，帮助观察你的 Harness 如何工作；尚不是经过心理测量标定的性格量表，也不证明这些特征由用户记忆造成。

## 安装

从 [GitHub Releases](https://github.com/catisamaomao/dsh-harness-battery/releases) 下载 `dsh-harness-battery-0.1.0.tgz` 和 `SHA256SUMS.txt`。在下载目录核对：

```bash
shasum -a 256 -c SHA256SUMS.txt
```

在 Harness 的插件管理页安装本地 `.tgz`。若使用命令行，先完全退出 Harness，再运行：

```bash
dsh plugin --profile desktop add ./dsh-harness-battery-0.1.0.tgz
```

如果 `dsh` 尚未加入 PATH，macOS 安装包提供以下入口：

```bash
"/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh" \
  plugin --profile desktop add ./dsh-harness-battery-0.1.0.tgz
```

重新打开 Harness，在插件管理页确认“鲸相”已启用，再点击侧栏“鲸相”。插件已经包含运行代码，无安装构建脚本，也无需 npm 账号或 npm 全局安装。

在本机 `0.2.0-rc.2` 验收时，宿主的 pnpm 曾显示安装完成后仍未退出，导致自动启用没有执行。遇到这种情况，应先确认该安装进程已停止，再在插件管理页检查已安装列表并手动启用；仅看到下载或 `Done` 日志还不代表插件可用。

## 使用

1. 点击环境检查。真实测试需要 macOS、DeepSeek Harness 桌面版、Python 3.10+ 和可用的 DeepSeek 登录状态。Python 3.14 之前还需要 `zstd` 命令用于读取会话证据。
2. 选择 **核心版 9 题**或**扩展版 23 题**，可以先预览题面。扩展版包含原九题和新增十四题，七个维度各新增两题。
3. 确认请求预算后点击开始。默认每题一次、每题最多 30 次模型请求；核心版最多 270 次、扩展版最多 690 次，实际数量通常较少，会产生 DeepSeek 用量。
4. 可取消正在运行的测试。完成、取消及失败批次都留在本机历史中，报告会区分已完成、受阻、截断和缺失证据。
5. 打开报告查看七维观察及原文证据。报告 HTML 可独立离线打开，JSON 导出包含所选报告的证据，请按需要自行保管。

**默认不读取你的记忆。** 勾选使用 Memorix 记忆副本后，插件在本地制作私有副本；随后点击开始才允许本次测试将检索到的记忆连同题目、工具结果发送到官方 DeepSeek API。测试中的修改留在每题独立副本，答复、报告和画像标签不写回原记忆或当前对话。

插件只提供用户操作页面，不注册模型工具，不添加系统提示，不自动发起后台测评。

## 题库与报告

| 维度 | 观察内容 |
|---|---|
| D1 自主权与范围 | 明确授权、建议与修改边界 |
| D2 确认姿态 | 实际检查、计算与资料核对 |
| D3 沟通形态 | 自然回答中的表达与组织方式 |
| D4 证据与不确定性 | 来源冲突、证据不足及结论边界 |
| D5 异议行为 | 对错误和有根据前提的回应 |
| D6 工作组织 | 替代路径、部分交付与具体求助 |
| D7 能力与失败报告 | 公开汇报与实际结果是否一致 |

[新增题面全文](runtime/batteries/extended-v2/QUESTIONS.md) · [题库覆盖说明](runtime/batteries/extended-v2/README.md)

题库按版本冻结，运行计划绑定 SHA256。产物通过仅说明它符合该题的交付约定；受阻题通过不意味着整体目标已完成。D5/D7 的语义仍需复核，分数和等级保持空值。右侧三张不同姿态的立绘为自选示意封面，六轴形象演示与真实七维报告分开，不用演示数字冒充测试结果。

隔离执行模式没有复刻日常 desktop 的全部工具、插件与历史上下文。提供记忆副本也不等于模型实际检索过记忆；报告分别记录服务可用性和公开检索证据。详情见 [数据与运行边界](docs/data-boundaries.md)。

## 系统支持

- **真实模型执行：macOS。** 使用系统 Seatbelt 隔离整个 DSH 进程树，隔离不可用时拒绝启动，不退回无隔离执行。
- **其他平台：浏览题库。** 已导出的 HTML 报告可独立打开；插件内历史迁移尚未提供。尚未提供 Windows/Linux 的安全执行后端。
- 首版面向 DeepSeek Harness `0.2.0-rc.2`，清单兼容范围为 `>=0.2.0-rc.2 <0.3.0`；后续宿主版本仍需实际验证。
- 默认安装路径为 `/Applications/DeepSeek Harness.app`。自定义安装路径可通过宿主进程环境变量 `DSH_HARNESS_APP` 指定。

## 开发与验证

在源码目录执行以下命令。不依赖 npm 第三方包，需要 Node.js 22+ 与 Python 3.10+：

```bash
npm test
npm run check
npm pack --ignore-scripts
python3 -B scripts/audit-release.py --tarball dsh-harness-battery-0.1.0.tgz
```

`npm test` 执行离线插件、运行器与发布扫描检查，**不会调用模型或读取真实记忆**。题包的两个冻结摘要也会复核。测试真实调用需要通过页面明确开始。

发布源码由研究项目整理而来，没有携带研究仓库 Git 历史、真实运行结果、用户会话、凭据或记忆数据库。题包保留原版本的元数据和历史来源标识，用于版本追溯；这些标识不代表旧数据已随插件发布。冻结题包中的研究命令和历史链接是原始版本记录，插件的使用入口以本 README 为准。

MIT 源码许可；插画来源和许可边界见 [ARTWORK.md](ARTWORK.md)。本项目非 DeepSeek 官方插件。
