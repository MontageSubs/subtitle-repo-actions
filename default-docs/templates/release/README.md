# 版本：{release_name}

本目录对应一份独立时间轴，来源 / 版本信息如下：

| 项目 | 值 |
| :--- | :--- |
| 来源类型 | {source_type_label} |
| 附加标签 | {label_display} |
| 目录标识 | `{release_name}` |

命名规则与来源类型说明参见 [版本命名指南](../../docs/RELEASE_GUIDE.md)。

## 下方会有哪些子目录

字幕的翻译、制作与生成全部发生在 [`work/`](./work/) 目录下，其中又分三个子目录：

- [`work/source/`](./work/source/) —— 官方原始底本，只读，不改动
- [`work/authored/`](./work/authored/) —— 翻译、校对、时间轴、特效持续打磨的地方
- [`work/generated/`](./work/generated/) —— 自动化产出的最终字幕，随时可被重跑覆盖

进入 [`work/`](./work/) 查看完整说明。

## 打包元数据

[`release.yml`](./release.yml) 记录打包用的机器可读元数据，如需调整展示名称请直接编辑该文件。

---

<div align="center">

**蒙太奇字幕社区 (MontageSubs)**  
"用爱发电 ❤️ Powered by Love"

</div>
