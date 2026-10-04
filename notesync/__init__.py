"""sticky-mi-sync —— 微软便笺 ⇄ 小米笔记 本地同步器

纯标准库实现（真实连接器按需引入第三方库）：
  textutil  文本规范化（幂等映射的基石）
  store     SQLite 状态库（映射表 / 基线哈希 / 凭据 / 日志）
  graph     微软便笺数据源（设备码 OAuth + Graph /me/notes）
  xiaomi    小米笔记数据源（i.mi.com Cookie + /note/full/page）
  engine    三方比较同步引擎（四方决策 + 冲突 + 删除传播）
"""

__version__ = "0.1.0"
