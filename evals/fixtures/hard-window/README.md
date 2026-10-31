# Hard Window

滚动窗口统计工具。当前实现采用朴素算法，且滚窗边界、偶数窗口中位数、最近秩分位数与并列排序的定义都不正确。

## 公开 API

- `window.rolling_median(values, k) -> list`
- `window.p95(values) -> float | None`
- `window.top_k_frequent(values, k) -> list[tuple[int, int]]`

## 运行测试

```
python -m unittest discover -s tests
```
