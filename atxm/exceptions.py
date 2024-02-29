from enum import Enum

from web3.types import RPCError


class Fault(Enum):
    """
    Fault codes for transaction processing.
    These are alternate states that a transaction can enter
    other than "finalized".
    """

    # Strategy has been running for too long
    TIMEOUT = "timeout"

    # Transaction has been capped and subsequently timed out
    PAUSE = "pause"

    # Transaction reverted
    REVERT = "revert"

    # Something went wrong
    ERROR = "error"

    # ...
    INSUFFICIENT_FUNDS = "insufficient_funds"


class InsufficientFunds(RPCError):
    """raised when a transaction exceeds the spending cap"""


class Wait(Exception):
    """
    Raised when a strategy exceeds a limitation.
    Used to mark a pending transaction as "wait, don't retry".
    """


class TransactionFault(Exception):
    """raised when a transaction has been faulted"""

    def __init__(self, tx, fault: Fault, message: str):
        self.tx = tx
        self.fault = fault
        self.message = message
        super().__init__(message)


class TransactionReverted(Exception):
    """raised when a transaction has been reverted"""
