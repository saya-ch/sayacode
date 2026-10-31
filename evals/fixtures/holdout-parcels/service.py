"""包裹业务入口。"""
from models import Parcel


class ParcelService:
    def __init__(self, store):
        self.store = store

    def register(self, tracking, recipient, address):
        try:
            self.store.get(tracking)
        except KeyError:
            pass
        else:
            raise ValueError("duplicate tracking")
        parcel = Parcel(tracking, recipient, address)
        self.store.save(parcel)
        return parcel

    def dispatch(self, tracking):
        parcel = self.store.get(tracking)
        if parcel.status != "created":
            raise ValueError("cannot dispatch")
        parcel.status = "in_transit"
        parcel.history.append("in_transit")
        self.store.save(parcel)
        return parcel
