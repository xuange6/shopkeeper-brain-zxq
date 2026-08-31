# Contributing

感谢你改进 Shopkeeper Brain。

## 开发流程

1. 从 `main` 创建功能分支。
2. 不要提交 `.env`、模型、虚拟环境、用户文档或 MinerU 解析产物。
3. 保持 API 向后兼容；新增响应字段优先采用可选字段。
4. 外部服务调用需要有超时、可读错误和降级路径。
5. 提交前运行：

```powershell
python -m unittest discover -s tests -v
python -m compileall -q -x "\\.venv|import_temp_Dir|__pycache__" knowledge tests
```

## Pull Request

PR 描述请包含：

- 问题与目标；
- 关键设计选择；
- 对导入/查询工作流的影响；
- 测试方式；
- 涉及 UI 时附桌面端和移动端截图。

不要在示例、日志、Issue 或截图中暴露真实 API Key、数据库密码和用户文档。
