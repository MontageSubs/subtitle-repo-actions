# Init Manifest

初始化阶段建立仓库目录结构、写入默认文档所依据的配置表。`main.py` 只解析下方的 Markdown 表格本体（表头行 + 分隔行 + 连续的数据行），表格前后的说明文字不会被解析，可自由编辑。

## 字段说明

- **action**：`copy` 从 source 复制内容到 destination；`touch` 在 destination 创建一个空文件（用于建立空目录，Git 不支持提交空目录）
- **source**：相对 subtitle-repo-actions 仓库根目录的路径；`action` 为 `touch` 时填 `-`
- **destination**：相对目标仓库根目录的完整路径（含文件名），支持在复制时改名，如 `docs/CONTRIBUTING.md → docs/Guide.md`
- **commit_message**：本行改动所属的 commit message；多行共用同一 commit_message 会被合并进同一次提交
- **description**：给人看的说明，机器不解析此列

| action | source | destination | commit_message | description |
| :--- | :--- | :--- | :--- | :--- |
| copy | default-docs/docs/CONTRIBUTING.md | docs/CONTRIBUTING.md | add: docs | 贡献指南 |
| copy | default-docs/docs/COPYRIGHT.md | docs/COPYRIGHT.md | add: docs | 著作权声明 |
| copy | default-docs/docs/GITHUB_USAGE_GUIDE.md | docs/GITHUB_USAGE_GUIDE.md | add: docs | GitHub 使用指南 |
| copy | default-docs/docs/TRANSLATION_GUIDE.md | docs/TRANSLATION_GUIDE.md | add: docs | 翻译与风格指南 |
| copy | default-docs/licenses/cc/by-nc-sa/4.0/LICENSE | LICENSE | CC BY-NC-SA 4.0 | 许可协议（英文） |
| copy | default-docs/licenses/cc/by-nc-sa/4.0/LICENSE.zh-Hans | LICENSE.zh-Hans | CC BY-NC-SA 4.0 | 许可协议（中文） |
| copy | default-docs/docs/RELEASE_GUIDE.md | docs/RELEASE_GUIDE.md | add: docs | Release 命名指南 |
| copy | default-docs/subtitles/README.md | subtitles/README.md | add: subtitles skeleton | 字幕版本总览 |
