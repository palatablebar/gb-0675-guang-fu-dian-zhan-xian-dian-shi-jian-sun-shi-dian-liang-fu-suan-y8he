"""事件身份工具。

事件键只由设备与限电并集的 UTC 起止时刻构成（后者用 epoch 秒，全部为
URL 非保留字符），因此同一天然事件在脏批/清批/旧编码批之间保持一致，
事件编号变化不会把同一事件拆成两条，也可安全用作查询参数。
"""

from app.coerce import instant


def event_key(device, start, end):
    return f'{device}~{int(instant(start).timestamp())}~{int(instant(end).timestamp())}'
