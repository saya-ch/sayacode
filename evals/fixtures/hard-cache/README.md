# Hard Cache

带容量上限与 TTL 的键值缓存。当前实现 TTL 不生效、读取不更新最近使用顺序、过期也没有统计。

## 公开 API

- `cache.TTLCache(capacity, ttl, clock)`
- `get(key) -> float | None`
- `put(key, value) -> None`
- `stats() -> {"hits", "misses", "evictions", "expirations"}`

`clock` 是返回当前时间的可调用对象，测试会用一个可控时钟替换它。

## 运行测试

```
python -m unittest discover -s tests
```
