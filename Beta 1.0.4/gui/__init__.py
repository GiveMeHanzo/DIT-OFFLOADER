"""GUI 层：基于 PySide6 的三栏界面。

后端通过 QObject + Qt Signal 与 UI 解耦，所有重活（拷贝/校验）在 QThread 中进行。
"""
