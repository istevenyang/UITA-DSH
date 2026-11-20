"""rosetta_diff — 对比"真机探查结果"（*_rosetta_map_probe.json）与现有
`apps/<app>/rosetta_map.json`，输出新增/消失/变更/selector 冲突的结构化 diff，
供人工审核后经 `knowledge_writer` 写入知识库（probe-log.md / known-issues.md）。

对齐宪法 R21/R22：
- rosetta_map v3.0 schema：`app -> 页面 -> 元素 -> {mac,win,usage,...}`
- 探查草稿本身不入库，须经本工具生成 diff 报告，人工确认后才落地正式文档。
"""
