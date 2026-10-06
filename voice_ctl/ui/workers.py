"""界面后台线程的共享约定。

一把锁：`HEAVY_IMPORT_LOCK`——凡是会 `import torch` / `laya` 的后台活（语义层可用性检查、
下载前的预检、完整体检）都先拿它。

为什么必须有它，而且 UI 线程**绝不能**去碰这些导入（实测踩出来的死锁）：

    后台线程 import torch（几秒、期间 Python 的垃圾回收随时会跑）
      → GC 回收到某个 tkinter 对象（PhotoImage / StringVar / Font，它们的 `__del__` 要调 Tcl）
      → 非主线程调 Tcl，会被转发给主线程并**阻塞等待**主线程处理
      → 而主线程若正好卡在同一个 import 锁上（比如 UI 线程里也调了 `preflight_fetch`）
      → 两边互等，永久卡死。

所以规矩是：主线程只跑事件循环，从不等后台线程；重量级导入只在持有这把锁的后台线程里发生。
这样 Tcl 转发总有人及时处理，而且同一时刻只有一个线程在导入 torch（多线程同时导入 torch 本身
也有死锁隐患）。
"""

from __future__ import annotations

import threading

HEAVY_IMPORT_LOCK = threading.Lock()
