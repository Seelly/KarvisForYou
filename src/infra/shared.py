# -*- coding: utf-8 -*-
"""
KarvisForAll 共享资源。

全局线程池等需要被多个模块复用的资源集中在此，
避免 skills 模块直接依赖 brain 模块的内部实现。
"""
from concurrent.futures import ThreadPoolExecutor

# 全局复用线程池，减少线程创建开销
# brain.py、skills/*、proactive.py 等模块统一使用此线程池
executor = ThreadPoolExecutor(max_workers=6)
