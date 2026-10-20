# 构建组合目录

已接入 `x60pro-personal.json`：个人 X60 Pro，使用上传的完整配置及 Caddy 文件备份。
客户配置和 EW-6000GX Pro 配置仍待后续接入。

每个组合独立保存为 `<name>.json`，可参考 `../examples/preset.json.example`。
文件名必须与配置中的 `name` 一致。只有 `enabled: true` 的组合会进入构建 matrix。
完整字段、文件打包规则与本地配置导入方式见 `../README.md`。
