import time

import math
from abc import ABC
from datetime import datetime, timedelta
from typing import Optional, Tuple

from web3 import Web3
from web3.types import PendingTx, TxParams

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


class TimeoutStrategy(AsyncTxStrategy):
    """Timeout strategy for pending transactions."""

    _NAME = "timeout"

    # TODO is this too long?
    TIMEOUT = 60 * 60  # 1 hour in seconds

    _WARN_FACTOR = 0.15  # 15% of timeout remaining

    def __init__(self, w3: Web3, timeout: Optional[int] = None):
        super().__init__(w3)
        self.timeout = timeout or self.TIMEOUT
        # use 30s as default in case timeout is too small for warn factor
        self._warn_threshold = max(30, self.timeout * self._WARN_FACTOR)

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
        if time_remaining.seconds < self._warn_threshold:
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


class ExponentialSpeedupStrategy(AsyncTxStrategy):
    """
    Speedup strategy for pending transactions that increases fees by a
    percentage over the prior value every time it is used.
    """

    _SPEEDUP_INCREASE_PERCENTAGE = 0.125  # 12.5%

    _MIN_SPEEDUP_INCREASE = 0.10  # mandated by eth standard

    _MAX_TIP_FACTOR = 3  # max 3x over suggested tip

    _MIN_TIME_BETWEEN_SPEEDUPS = 90  # 90s minimum between speedups

    _NAME = "speedup"

    _GAS_PRICE_FIELD = "gasPrice"
    _MAX_FEE_PER_GAS_FIELD = "maxFeePerGas"
    _MAX_PRIORITY_FEE_PER_GAS_FIELD = "maxPriorityFeePerGas"

    def __init__(
        self,
        w3: Web3,
        speedup_increase_percentage: float = _SPEEDUP_INCREASE_PERCENTAGE,
        max_tip_factor: int = _MAX_TIP_FACTOR,
        min_time_between_speedups: int = _MIN_TIME_BETWEEN_SPEEDUPS,
    ):
        super().__init__(w3)

        if (
            speedup_increase_percentage < self._MIN_SPEEDUP_INCREASE
            or speedup_increase_percentage > 1
        ):
            raise ValueError(
                f"Invalid speedup increase percentage {speedup_increase_percentage}; "
                f"must be in range [0.10, 1]"
            )
        if max_tip_factor <= 1:
            raise ValueError(f"Invalid max tip factor {max_tip_factor}; must be > 1")

        self.speedup_factor = 1 + speedup_increase_percentage
        self.max_tip_factor = max_tip_factor
        self.min_time_between_speedups = min_time_between_speedups

    def _calculate_eip1559_speedup_fee(self, params: TxParams) -> Tuple[int, int, int]:
        current_base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        suggested_tip = self.w3.eth.max_priority_fee
        _log_gas_weather(current_base_fee, suggested_tip)

        # default to 1 if not specified in tx
        prior_max_priority_fee = params.get(self._MAX_PRIORITY_FEE_PER_GAS_FIELD, 0)
        updated_max_priority_fee = math.ceil(
            max(prior_max_priority_fee, suggested_tip) * self.speedup_factor
        )

        current_max_fee_per_gas = params.get(self._MAX_FEE_PER_GAS_FIELD)
        if current_max_fee_per_gas:
            # already previously set, just increase by factor but ensure base fee hasn't
            # also increased. The defaults used by web3py for this value is already pretty
            # high so don't overdo the multiplication factor.
            updated_max_fee_per_gas = math.ceil(
                max(
                    # last attempt param
                    current_max_fee_per_gas * self.speedup_factor,
                    # OR take current conditions and speedup
                    (current_base_fee * self.speedup_factor) + updated_max_priority_fee,
                )
            )
        else:
            # not previously set, set to same default as web3py (transactions.py)
            updated_max_fee_per_gas = math.ceil(
                updated_max_priority_fee + (current_base_fee * 2)
            )

        return suggested_tip, updated_max_priority_fee, updated_max_fee_per_gas

    def _calculate_legacy_speedup_fee(self, params: TxParams) -> int:
        generated_gas_price = (
            self.w3.eth.generate_gas_price(params) or 0
        )  # 0 means no gas strategy
        old_gas_price = params[self._GAS_PRICE_FIELD]

        base_price_to_increase = old_gas_price
        if generated_gas_price > old_gas_price:
            log.info(
                f"[speedup] increase gas price based on updated generated "
                f"gas price value {generated_gas_price} vs prior value {old_gas_price}"
            )
            base_price_to_increase = generated_gas_price

        return math.ceil(base_price_to_increase * self.speedup_factor)

    def execute(self, pending: PendingTx) -> Optional[TxParams]:
        if pending.last_updated + self.min_time_between_speedups > int(time.time()):
            # too soon - don't update
            return None

        params = pending.params

        if self._GAS_PRICE_FIELD in pending.params:
            old_gas_price = params[self._GAS_PRICE_FIELD]
            new_gas_price = self._calculate_legacy_speedup_fee(pending.params)
            log.info(
                f"[speedup] Speeding up legacy transaction #atx-{pending.id} (nonce={params['nonce']}) \n"
                f"gasPrice {old_gas_price} -> {new_gas_price}"
            )
            params[self._GAS_PRICE_FIELD] = new_gas_price
        else:
            old_tip, old_max_fee = (
                params.get(self._MAX_PRIORITY_FEE_PER_GAS_FIELD),
                params.get(self._MAX_FEE_PER_GAS_FIELD),
            )
            suggested_tip, new_tip, new_max_fee = self._calculate_eip1559_speedup_fee(
                params
            )

            # TODO: is this the best way of setting a cap?
            if new_tip > (suggested_tip * self.max_tip_factor):
                # nothing the strategy can do here - don't change the params
                log.warn(
                    f"[speedup] Increasing pending transaction's {self._MAX_PRIORITY_FEE_PER_GAS_FIELD} "
                    f"to {round(Web3.from_wei(new_tip, 'gwei'), 4)} gwei will exceed "
                    f"spending cap factor {self.max_tip_factor}x over suggested tip "
                    f"({round(Web3.from_wei(suggested_tip, 'gwei'), 4)} gwei); "
                    f"don't speed up"
                )
                return None

            tip_increase_message = (
                f"(~+{round(Web3.from_wei(new_tip - old_tip, 'gwei'), 4)} gwei) {old_tip}"
                if old_tip
                else "undefined"
            )
            fee_increase_message = (
                f"(~+{round(Web3.from_wei(new_max_fee - old_max_fee, 'gwei'), 4)} gwei) {old_max_fee}"
                if old_max_fee
                else "undefined"
            )
            log.info(
                f"[speedup] Speeding up transaction #atx-{pending.id} (nonce={params['nonce']}) \n"
                f"{self._MAX_PRIORITY_FEE_PER_GAS_FIELD} {tip_increase_message} -> {new_tip} \n"
                f"{self._MAX_FEE_PER_GAS_FIELD} {fee_increase_message} -> {new_max_fee}"
            )
            params = dict(params)
            params[self._MAX_PRIORITY_FEE_PER_GAS_FIELD] = new_tip
            params[self._MAX_FEE_PER_GAS_FIELD] = new_max_fee

        params = TxParams(params)
        return params
