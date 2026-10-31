"""进程内包裹仓库。"""


class ParcelStore:
    def __init__(self):
        self._items = {}

    def save(self, parcel):
        self._items[parcel.tracking] = parcel

    def get(self, tracking):
        return self._items[tracking]
