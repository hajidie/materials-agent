# Materials storage references

`materials_storage.ObjectStorageRef` 是仅依赖 Python 标准库的不可变值对象，格式为
`object-storage-ref-v1`。它记录 object_id、store_id、bucket、storage_key、可选 version_id、
SHA-256、size_bytes 和 media_type；`verify(bytes)` 检查大小和内容摘要。

引用不是授权，也不是下载 URL。调用者的 Storage Adapter 负责限定 store/bucket/namespace、验证对象归属、
处理写入结果未知和清理。不要把 bucket/key 放进平台公开 ResourceRef。该包没有客户端、业务实体或数据库表，
不迁移现有图片 Asset；MLArtifact 使用它，未来 RAG 的 Document Artifact 可复用相同合同。

在独立 ML 环境安装 `pip install -e ./packages/materials_storage`，运行
`python -m pytest packages/materials_storage/tests -q`。完整 ML 验收入口见 `services/materials_ml/README.md`。
