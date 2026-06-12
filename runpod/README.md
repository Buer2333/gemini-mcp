# RunPod Wan2.2 部署套件 — 为什么在 gemini-mcp 下？

> 2026-06-12 补注（用户对此位置感到困惑后写）

**身世**：本套件（grab_pod / bootstrap_nv / wan22_generate / daily_health / auto_stop）2026-05 起在本仓有机生长——当时 gemini-mcp 是 AI 生成工具的既有仓库且已在 Git Auto-Sync 清单，放这里免费获得版控+自动同步。语义上它与 Gemini MCP server **无关**，是历史便利驱动的错位。

**为什么不搬（2026-06-12 决策）**：按重构 B/C 门槛（独立部署/跨仓共享），当前只有 A 类（语义可读性）诉求，且活任务（T-20260520-001 chest-factory、T-20260611-003 autonomy pilot）正在引用现路径。

**搬家触发条件**（满足任一再动）：
1. remote-fleet（DT 双机）与 RunPod 工具收敛成统一"生成算力基建"模块（C 驱动）
2. 本套件需要独立部署到其他机器（B 驱动）
3. 引用本目录的活任务全部 closed 且下轮巡检确认

**真源注册**：`~/.claude/skills/runpod-ops/references/scripts-map.md`（脚本用法）+ routing.md 高频指针。
