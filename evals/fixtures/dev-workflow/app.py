"""记录导出器。"""
import csv
import io


def render(records):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["name", "score"])
    writer.writeheader()
    writer.writerows(records)
    return stream.getvalue()
