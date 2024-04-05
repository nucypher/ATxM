from enum import Enum

from web3.types import PendingTx


class Fault(Enum):
    """
    Fault codes for transaction processing.
    These are alternate states that a transaction can enter
    other than "finalized".
    """

    # Strategy has been running for too long
    TIMEOUT = "timeout"

    # Something went wrong
    ERROR = "error"


class InsufficientFunds(Exception):
    """raised when a transaction exceeds the spending cap"""


class RPCException(Exception):
    def __init__(self, error_code: int, error_message: str):
        self.error_code = error_code
        self.error_message = error_message
        super().__init__(f"RPC Error [{error_code}]: {error_message}")


class TransactionFaulted(Exception):
    """Raised when a transaction has been faulted."""

    def __init__(self, tx: PendingTx, fault: Fault, message: str):
        self.tx = tx
        self.fault = fault
        self.message = message
        super().__init__(message)
