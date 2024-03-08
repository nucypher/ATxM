from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from eth_typing import ChecksumAddress
from eth_utils import encode_hex
from hexbytes import HexBytes
from web3.types import TxParams, TxReceipt

from atxm.exceptions import Fault

TxHash = HexBytes


@dataclass
class AsyncTx(ABC):
    id: int
    params: TxParams
    final: bool = field(default=None, init=False)
    fault: Optional[Fault] = field(default=None, init=False)
    on_broadcast: Optional[Callable[["PendingTx"], None]] = field(
        default=None, init=False
    )
    on_broadcast_failure: Optional[Callable[["FutureTx", Exception], None]] = field(
        default=None, init=False
    )
    on_finalized: Optional[Callable[["FinalizedTx"], None]] = field(
        default=None, init=False
    )
    on_fault: Optional[Callable[["FaultedTx"], None]] = field(default=None, init=False)

    def __repr__(self):
        return f"<{self.__class__.__name__} id={self.id} final={self.final}>"

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return self.id == other.id

    def __ne__(self, other):
        return not self.__eq__(other)

    @abstractmethod
    def to_dict(self):
        raise NotImplementedError

    @classmethod
    def from_dict(cls, data: Dict):
        raise NotImplementedError


@dataclass
class FutureTx(AsyncTx):
    requeues: int = field(default=0, init=False)
    final: bool = field(default=False, init=False)
    info: Optional[Dict] = None

    def __hash__(self):
        return hash(self.id)

    @property
    def _from(self) -> ChecksumAddress:
        return self.params["from"]

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "params": _serialize_tx_params(self.params),
            "info": self.info,
        }

    @classmethod
    def from_dict(cls, data: Dict):
        return cls(
            id=int(data["id"]),
            params=TxParams(data["params"]),
            info=dict(data["info"]),
        )


@dataclass
class PendingTx(AsyncTx):
    retries: int = field(default=0, init=False)
    final: bool = field(default=False, init=False)
    txhash: TxHash
    created: int

    def __hash__(self) -> int:
        return hash(self.txhash)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "txhash": self.txhash.hex(),
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, data: Dict):
        return cls(
            id=int(data["id"]),
            txhash=HexBytes(data["txhash"]),
            created=int(data["created"]),
        )


@dataclass
class FinalizedTx(AsyncTx):
    final: bool = field(default=True, init=False)
    receipt: TxReceipt

    def __hash__(self) -> int:
        return hash(self.receipt["transactionHash"])

    def to_dict(self) -> Dict:
        return {"id": self.id, "receipt": _serialize_tx_receipt(self.receipt)}

    @classmethod
    def from_dict(cls, data: Dict):
        receipt = _deserialize_tx_receipt(data["receipt"])
        return cls(id=int(data["id"]), receipt=receipt)

    @property
    def txhash(self) -> TxHash:
        return self.receipt["transactionHash"]


@dataclass
class FaultedTx(AsyncTx):
    final: bool = field(default=False, init=False)
    fault: Fault
    error: Optional[str] = None

    def __hash__(self) -> int:
        return hash(self.id)

    def to_dict(self) -> Dict:
        return {"id": self.id, "error": str(self.error), "fault": self.fault.value}

    @classmethod
    def from_dict(cls, data: Dict):
        return cls(
            id=int(data["id"]), error=str(data["error"]), fault=Fault(data["fault"])
        )


def _serialize_tx_params(params: TxParams) -> Dict:
    """Serializes transaction parameters to a dictionary for disk storage."""
    data = params.get("data")
    if isinstance(data, bytes):
        data = encode_hex(data)
    result = {
        "nonce": params["nonce"],
        "chainId": params["chainId"],
        "gas": params["gas"],
        "type": params.get("type"),
        "to": params["to"],
        "value": params["value"],
        "data": data,
        "maxPriorityFeePerGas": params.get("maxPriorityFeePerGas"),
        "maxFeePerGas": params.get("maxFeePerGas"),
        "gasPrice": params.get("gasPrice"),
    }
    # filtrate Nones
    result = {k: v for k, v in result.items() if v is not None}
    return result


def _serialize_tx_receipt(receipt: TxReceipt) -> Dict:
    return {
        "transactionHash": encode_hex(receipt["transactionHash"]),
        "transactionIndex": receipt["transactionIndex"],
        "blockHash": encode_hex(receipt["blockHash"]),
        "blockNumber": receipt["blockNumber"],
        "from": receipt["from"],
        "to": receipt["to"],
        "cumulativeGasUsed": receipt["cumulativeGasUsed"],
        "gasUsed": receipt["gasUsed"],
        "status": receipt["status"],
    }


def _deserialize_tx_receipt(receipt: Dict) -> TxReceipt:
    return TxReceipt(
        {
            "transactionHash": HexBytes(receipt["transactionHash"]),
            "transactionIndex": receipt["transactionIndex"],
            "blockHash": HexBytes(receipt["blockHash"]),
            "blockNumber": receipt["blockNumber"],
            "from": receipt["from"],
            "to": receipt["to"],
            "cumulativeGasUsed": receipt["cumulativeGasUsed"],
            "gasUsed": receipt["gasUsed"],
            "status": receipt["status"],
        }
    )
