"""包裹领域对象。"""
from dataclasses import dataclass, field


@dataclass
class Parcel:
    tracking: str
    recipient: str
    address: str
    status: str = "created"
    history: list[str] = field(default_factory=lambda: ["created"])
