# 构建组合目录

实际组合稍后根据已经验证的本地配置接入；目前没有可运行的组合。

每个组合独立保存为 `<name>.json`，可参考 `../examples/preset.json.example`。
文件名必须与配置中的 `name` 一致。只有 `enabled: true` 的组合会进入构建 matrix。
完整字段、文件打包规则与本地配置导入方式见 `../README.md`。
