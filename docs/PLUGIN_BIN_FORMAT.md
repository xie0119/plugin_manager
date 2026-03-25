# PLUGBIN 插件包格式说明

## 1. 设计目标

- **稳定**：固定长度头 + 明确长度字段，便于流式校验与随机访问扩展。
- **可扩展**：`flags` 预留压缩/签名；manifest `extra` 保留未知 JSON 字段。
- **安全**：路径必须为相对 POSIX 路径，禁止 `..`、绝对路径；载荷解析后再次校验解包路径。
- **完整性**：对 `manifest || payload` 计算 CRC32，与头中字段比对。

## 2. 文件布局

| 区域 | 长度 | 说明 |
|------|------|------|
| Header | 64 字节 | 魔数、版本、长度、CRC、保留 |
| Manifest | `manifest_len` | UTF-8 JSON |
| Payload | `payload_len` | 二进制文件表 |

总大小不超过 `MAX_BIN_FILE_BYTES`（默认 64 MiB）。

## 3. Header（64 字节，小端）

| 偏移 | 类型 | 字段 |
|------|------|------|
| 0–7 | `bytes` | Magic：`PLUGBIN\0` |
| 8–11 | `uint32` | `format_version`：当前为 `1` |
| 12–15 | `uint32` | `flags`：预留（压缩/签名等） |
| 16–19 | `uint32` | `manifest_len` |
| 20–23 | `uint32` | `payload_len` |
| 24–27 | `uint32` | `crc32`：`zlib.crc32(manifest_bytes + payload_bytes) & 0xffffffff` |
| 28–63 | `bytes` | 保留，填 0 |

## 4. Manifest（JSON）

必填字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `plugin_id` | string | 与 `plugins/<id>.bin` 文件名 stem 一致 |
| `plugin_name` | string | 展示名 |
| `version` | string | 语义化版本或构建号 |
| `entry` | string | 入口相对路径（如 `plugin.py` 或 `foo.py`） |
| `package_type` | string | `single_file` \| `directory` |
| `source_layout` | string | `flat_py` \| `directory_tree` |
| `created_at` | string | ISO8601（UTC，建议带 `Z`） |
| `api_version` | int | manifest 架构版本，当前为 `1` |

可选：`optional_dependencies`、`description`。其余键落入 `PluginBinManifest.extra`，便于前向兼容。

## 5. Payload（文件表）

逻辑结构：

1. `uint32`：`file_count`
2. 对每个文件：
   - `uint32`：`path_len`
   - `path_len` 字节：路径 UTF-8（POSIX，`/` 分隔，无 `..`）
   - `uint32`：`content_len`
   - `content_len` 字节：原始文件内容

单文件插件：`file_count == 1`，且 `package_type == single_file`。

目录插件：至少包含 `entry` 指向的文件（通常为 `plugin.py`），可含 `config.json`、`resources/**` 等。

## 6. 单文件与目录的兼容

- **单文件**：manifest 声明 `single_file` + `flat_py`，载荷仅一个键（文件名与磁盘上一致）。
- **目录**：`directory` + `directory_tree`，载荷为整棵目录的相对路径集合。

加载器将目录型包解包到**临时目录**，把该目录加入 `sys.path`，再对入口 `compile`+`exec`。资源通过注入的 `plugin_dir`（即解包根）访问，例如 `plugin_dir / "resources" / "a.png"`。

**相对导入**：当前入口模块名为 `plugin_<id>`，**不是** Python package 内的子模块，因此 **`from . import x` 不支持**；同目录辅助模块请使用 **`import sibling`**（依赖 `sys.path`）。

## 7. 扩展性

| 能力 | 方式 |
|------|------|
| 压缩 | `flags` 置位 + 载荷算法约定（待实现） |
| 签名 | `flags` + header 或 manifest 中 `signature` + 验签块 |
| 加密 | 独立层：解密后再走同一解析流程 |
| 版本迁移 | 提高 `format_version` 或 `api_version`，加载侧分支处理 |

## 8. 工具与加载

- **打包**：`python pack_plugins.py --plugins-dir plugins --out-dir dist/plugins_bin`
- **运行**：将 `.bin` 放入应用 `plugins/`，与 `.py` 目录插件共存；**同名时源码目录/单文件优先于 `.bin`**。

## 9. 限制（当前实现）

- 损坏的 bin、CRC 不符、非法 manifest、缺失 entry、源码非合法 UTF-8（入口）均会**显式失败**。
- 单文件包**恰好**一个文件；路径穿越载荷在打包与解包阶段均拒绝。
