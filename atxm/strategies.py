from abc import ABC
from datetime import datetime, timedelta
from typing import Tuple, Optional

from web3 import Web3
from web3.types import Gwei, TxParams, Wei, PendingTx

from atxm.exceptions import (
    Fault,
    TransactionFaulted,
)
from atxm.logging import log
from atxm.utils import (
    _log_gas_weather,
)


class AsyncTxStrategy(ABC):
    """Abstract base class for transaction strategies."""

    _NAME = NotImplemented

    def __init__(self, w3: Web3):
        self.w3 = w3
        self.log = log

    @property
    def name(self) -> str:
        """Used to identify the strategy in logs."""
        return self._NAME

    def execute(self, pending: PendingTx) -> Optional[TxParams]:
        """
        Execute the strategy.

        Called by the transaction machine when a
        transaction is ready to be strategized. Accepts a PendingTx
        object with data from the most recent previous attempt
        (like tx.txhash, tx.params, tx.created, etc).

        This method must do one of the following:
        - Raise `TransactionFaulted`to signal the transaction cannot be retried.
        - Returns an updated TxParams dictionary to use in the next attempt.
        - Returns None if the strategy makes no changes to the existing TxParams and
          signal that the machine should just wait for the existing tx

        NOTE: Do not mutate the input `tx` object. Return a new TxParams
        dictionary with the updated transaction parameters. The input
        object is already deeply copied to avoid accidental mutation,
        but it's best to be mindful of this.

        CAUTION: please be mindful that the purpose of this middleware
        is to mutate transaction parameters and not to broadcast
        transactions. Broadcasting transactions is handled by the
        transaction machine and not by the strategies.

        WARNING: The parameters returned by this method will be
        signed by a hot wallet and broadcast to the network immediately.
        Please be mindful of the security implications of the
        parameters you return.

        """
        raise NotImplementedError


class InsufficientFundsPause(AsyncTxStrategy):
    """Pause strategy for pending transactions."""

    _NAME = "insufficient-funds"

    def execute(self, pending: PendingTx) -> Optional[TxParams]:
        balance = self.w3.eth.get_balance(pending._from)
        if balance == 0:
            self.log.warn(
                f"Insufficient funds for transaction #{pending.params['nonce']}"
            )
            raise TransactionFaulted(
                tx=pending,
                fault=Fault.INSUFFICIENT_FUNDS,
                message="Insufficient funds",
            )
        # log.warn(f"Insufficient funds for transaction #{pending.params['nonce']}")
        return None


class TimeoutStrategy(AsyncTxStrategy):
    """Timeout strategy for pending transactions."""

    _NAME = "timeout"

    _TIMEOUT = 60 * 60  # 1 hour in seconds

    _WARN_FACTOR = 0.05  # 10% of timeout remaining

    def __init__(self, w3: Web3, timeout: Optional[int] = None):
        super().__init__(w3)
        self.timeout = timeout or self._TIMEOUT

    def __active_timed_out(self, pending: PendingTx) -> bool:
        """Returns True if the active transaction has timed out."""
        # seconds specificity (ignore microseconds)
        now = datetime.now().replace(microsecond=0)
        creation_time = datetime.fromtimestamp(pending.created).replace(microsecond=0)

        elapsed_time = now - creation_time
        if elapsed_time.seconds > self.timeout:
            return True

        end_time = creation_time + timedelta(seconds=self.timeout)
        time_remaining = end_time - now
        human_end_time = end_time.strftime("%Y-%m-%d %H:%M:%S")
        if time_remaining.seconds < (self.timeout * self._WARN_FACTOR):
            self.log.warn(
                f"[timeout] Transaction {pending.txhash.hex()} will timeout in "
                f"{time_remaining} at {human_end_time}"
            )
        else:
            self.log.info(
                f"[timeout] {pending.txhash.hex()} "
                f"{elapsed_time.seconds}s Elapsed | "
                f"{time_remaining} Remaining | "
                f"Timeout at {human_end_time}"
            )
        return False

    def execute(self, pending: PendingTx) -> Optional[TxParams]:
        if not pending:
            # should never get here
            raise RuntimeError("pending tx should not be None")

        timedout = self.__active_timed_out(pending)
        if timedout:
            raise TransactionFaulted(
                tx=pending,
                fault=Fault.TIMEOUT,
                message="Transaction has timed out",
            )
        return None


class FixedRateSpeedUp(AsyncTxStrategy):
    """Speedup strategy for pending transactions."""

    _SPEEDUP_INCREASE = 0.125  # 12.5%
    _MAX_TIP = Gwei(1)  # gwei maxPriorityFeePerGas per transaction

    _NAME = "speedup"

    def __init__(
        self,
        w3: Web3,
        speedup_percentage: Optional[float] = None,
        max_tip: Optional[Gwei] = None,
    ):
        super().__init__(w3)

        if speedup_percentage and speedup_percentage >= 1:
            raise ValueError(
                f"Invalid speedup increase percentage - {speedup_percentage}"
            )

        self.speedup_factor = 1 + (speedup_percentage or self._SPEEDUP_INCREASE)
        self.max_tip = max_tip or self._MAX_TIP

    def _calculate_speedup_fee(self, pending: TxParams) -> Tuple[Wei, Wei]:
        base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        suggested_tip = self.w3.eth.max_priority_fee
        _log_gas_weather(base_fee, suggested_tip)
        max_priority_fee = round(
            max(pending["maxPriorityFeePerGas"], suggested_tip) * self.speedup_factor
        )
        max_fee_per_gas = round(
            max(
                pending["maxFeePerGas"] * self.speedup_factor,
                (base_fee * 2) + max_priority_fee,
            )
        )
        return max_priority_fee, max_fee_per_gas

    def execute(self, pending: PendingTx) -> Optional[TxParams]:
        params = pending.params
        old_tip, old_max_fee = params["maxPriorityFeePerGas"], params["maxFeePerGas"]
        new_tip, new_max_fee = self._calculate_speedup_fee(params)
        tip_increase = round(Web3.from_wei(new_tip - old_tip, "gwei"), 4)
        fee_increase = round(Web3.from_wei(new_max_fee - old_max_fee, "gwei"), 4)

        if new_tip > self.max_tip:
            # nothing the strategy can do here - don't change the params
            return None

        latest_nonce = self.w3.eth.get_transaction_count(params["from"], "latest")
        pending_nonce = self.w3.eth.get_transaction_count(params["from"], "pending")
        if pending_nonce - latest_nonce > 0:
            log.warn("Overriding pending transaction!")

        log.info(
            f"Speeding up transaction #{params['nonce']} \n"
            f"maxPriorityFeePerGas (~+{tip_increase} gwei) {old_tip} -> {new_tip} \n"
            f"maxFeePerGas (~+{fee_increase} gwei) {old_max_fee} -> {new_max_fee}"
        )
        params = dict(params)
        params["maxPriorityFeePerGas"] = new_tip
        params["maxFeePerGas"] = new_max_fee
        params["nonce"] = latest_nonce
        params = TxParams(params)
        return params
