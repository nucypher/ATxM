from copy import copy, deepcopy
from typing import List, Optional, Union

from eth_account.signers.local import LocalAccount
from statemachine import State, StateMachine
from twisted.internet import reactor
from twisted.internet.defer import Deferred
from twisted.internet.task import LoopingCall
from web3 import Web3
from web3.types import TxParams

from atxm.exceptions import (
    Fault,
    InsufficientFunds,
    TransactionFaulted,
)
from atxm.strategies import AsyncTxStrategy, TimeoutStrategy
from atxm.tracker import _TxTracker
from atxm.tx import (
    FutureTx,
    PendingTx,
    TxHash,
)
from atxm.utils import (
    _get_average_blocktime,
    _get_confirmations,
    _get_receipt,
    _is_recoverable_send_tx_error,
    _make_tx_params,
    _process_send_raw_transaction_exception,
    fire_hook,
)

from .logging import log


class _Machine(StateMachine):
    """
    Do not import this - it will not work.
    Please import the publicly exposed `AutomaticTxMachine` instead.
    Thanks! :)
    """

    #
    # State Machine:
    #
    #       Pause
    #      ^      ^
    #     /        \
    #    V         v
    #   Idle <---> Busy
    #   | ^        | ^
    #   V |        V |
    #    _          _
    #
    _BUSY = State("Busy")
    _IDLE = State("Idle", initial=True)
    _PAUSED = State("Paused")

    # - State Transitions -
    _transition_to_paused = _BUSY.to(_PAUSED, cond="_pause") | _IDLE.to(
        _PAUSED, cond="_pause"
    )  # Busy/Idle -> Pause
    _transition_to_idle = _BUSY.to(_IDLE, unless=["_busy", "_pause"]) | _PAUSED.to(
        _IDLE, unless=["_busy", "_pause"]
    )  # Busy/Paused -> Idle
    _transition_to_busy = _IDLE.to(_BUSY, cond="_busy") | _PAUSED.to(
        _BUSY, unless="_pause"
    )  # Idle/Pause -> Busy
    # self transitions i.e. remain in same state
    _remain_busy = _BUSY.to.itself(cond="_busy", unless="_pause")
    _remain_idle = _IDLE.to.itself(unless=["_busy", "_pause"])

    _cycle_state = (
        _transition_to_idle
        | _transition_to_paused
        | _transition_to_busy
        | _remain_busy
        | _remain_idle
    )

    # internal
    __CLOCK = reactor  # twisted reactor

    # tweaks
    _TRACKING_CONFIRMATIONS = 300  # blocks until clearing a finalized transaction
    _RPC_THROTTLE = 1  # min. seconds between RPC calls (>1 recommended)
    _MIN_INTERVAL = 1
    _IDLE_INTERVAL = 60 * 5  # seconds
    _BLOCK_INTERVAL = 20  # ~20 blocks
    _BLOCK_SAMPLE_SIZE = 10_000  # blocks

    # max retries (broadcast or active)
    _MAX_RETRY_ATTEMPTS = 3

    class LogObserver:
        """StateMachine observer for logging information about state/transitions."""

        def __init__(self):
            self.log = log

        def on_transition(self, source, target):
            if source.id != target.id:
                self.log.debug(f"[transition] {source.name} --> {target.name}")

    def __init__(
        self,
        w3: Web3,
        tx_exec_timeout: int = TimeoutStrategy.TIMEOUT,
        strategies: Optional[List[AsyncTxStrategy]] = None,
        disk_cache: bool = False,
    ):
        # public
        self.w3 = w3
        self.signers = {}
        self.log = log
        # default TimeoutStrategy using provided timeout - guardrail for users
        self._strategies = [TimeoutStrategy(w3, timeout=tx_exec_timeout)]
        if strategies:
            self._strategies.extend(list(strategies))

        # state
        self._pause = False

        # tx tracking
        self._tx_tracker = _TxTracker(disk_cache=disk_cache)

        # async
        self._task = LoopingCall(self._cycle)
        self._task.clock = self.__CLOCK
        self._task.interval = self._IDLE_INTERVAL

        # busy interval
        self._busy_interval = None

        super().__init__()

        self.add_listener(_Machine.LogObserver())

    @property
    def _busy(self) -> bool:
        """Returns True if the machine is busy."""
        return bool(self._tx_tracker.queue or self._tx_tracker.pending)

    #
    # Async
    #

    def _handle_errors(self, *args, **kwargs):
        """Handles unexpected errors during task processing."""
        self._tx_tracker.commit()
        self.log.warn(
            "[recovery] error during transaction: {}".format(args[0].getTraceback())
        )
        if self._task.running:
            return
        self.log.warn("[recovery] restarting transaction machine!")
        self._start(now=False)

    # State Transitions
    @_transition_to_paused.before
    def _enter_pause_mode(self):
        self.log.info("[pause] pause mode activated")

    @_PAUSED.enter
    def _process_paused(self):
        self._sleep()

    @_transition_to_idle.before
    def _enter_idle_mode(self):
        self._task.interval = self._IDLE_INTERVAL
        self.log.info(
            f"[idle] returning to idle mode with {self._task.interval} second interval"
        )

    @_transition_to_busy.before
    def _enter_busy_mode(self):
        """About to enter busy work mode (speed up interval)"""
        if self._busy_interval is None:
            average_block_time = _get_average_blocktime(
                w3=self.w3, sample_size=self._BLOCK_SAMPLE_SIZE
            )
            self._busy_interval = max(
                round(average_block_time * self._BLOCK_INTERVAL), self._MIN_INTERVAL
            )

        self._task.interval = self._busy_interval
        self.log.info(f"[working] cycle interval is now {self._task.interval} seconds")

    @_BUSY.enter
    def _process_busy(self):
        self.log.info(
            f"[working] tracking {len(self._tx_tracker.queue)} queued "
            f"transaction{'s' if len(self._tx_tracker.queue) > 1 else ''} "
            f"{'and 1 pending transaction' if self._tx_tracker.pending else ''}"
        )

        if self._tx_tracker.pending:
            # There is an active transaction
            self.__handle_active_transaction()
        elif self._tx_tracker.queue:
            # There is no active transaction
            # and there are queued transactions
            self.__broadcast()

    def _cycle(self) -> None:
        """Execute one cycle"""
        self.__monitor_finalized()

        self._cycle_state()

    #
    # Throttle
    #

    def _start(self, now: bool = False) -> Deferred:
        if self._task.running:
            return self._task.deferred
        when = "now" if now else f"in {self._task.interval} seconds"
        self.log.info(f"[atxm] starting async transaction machine {when}")
        d = self._task.start(interval=self._task.interval, now=now)
        d.addErrback(self._handle_errors)
        return d

    def _stop(self):
        """Stop task."""
        if self._task.running:
            self._task.stop()

    def _wake(self) -> None:
        """Runs the looping call immediately."""
        self.log.info("[wake] running looping call now.")
        if self._task.running:
            # TODO instead of stopping/starting, can you set interval to 0
            #  and call reset to have looping call immediately?
            self._stop()

        self._start(now=True)

    def _sleep(self) -> None:
        if self._task.running:
            self.log.info("[sleep] sleeping")
            self._stop()

    #
    # Lifecycle
    #

    def __handle_active_transaction(self) -> None:
        """
        Handles the currently tracked pending transaction.

        The 3 possible outcomes for the pending ("active") transaction in one cycle:

        1. paused
        2. finalized (successful or reverted)
        3. still pending: strategize and retry, do nothing and wait, or fault

        Returns True if the next queued transaction can be broadcasted right now.
        """

        pending_tx = self._tx_tracker.pending

        receipt = _get_receipt(w3=self.w3, pending_tx=pending_tx)

        # Outcome 2: pending transaction is finalized (final success)
        if receipt:
            final_txhash = receipt["transactionHash"]
            confirmations = _get_confirmations(w3=self.w3, tx=pending_tx)
            self.log.info(
                f"[finalized] Transaction #atx-{pending_tx.id} with txhash: {final_txhash.hex()} "
                f"and status {receipt['status']} has been finalized with {confirmations} confirmation(s)"
            )
            self._tx_tracker.finalize_active_tx(receipt=receipt)
            return

        # Outcome 3: re-strategize the pending transaction
        self.__strategize()

    #
    # Broadcast tx
    #

    def __get_signer(self, address: str) -> LocalAccount:
        try:
            signer = self.signers[address]
        except KeyError:
            raise ValueError(f"Signer {address} not found")
        return signer

    def __fire(self, tx: Union[FutureTx, PendingTx], msg: str) -> TxHash:
        """
        Signs and broadcasts a transaction, handling RPC errors
        and internal state changes.
        """
        signer: LocalAccount = self.__get_signer(tx.params["from"])
        try:
            txhash = self.w3.eth.send_raw_transaction(
                signer.sign_transaction(tx.params).rawTransaction
            )
        except Exception as e:
            raise _process_send_raw_transaction_exception(e)

        self.log.info(
            f"[{msg}] fired transaction #atx-{tx.id}|{tx.params['nonce']}|{txhash.hex()}"
        )
        return txhash

    def __strategize(self) -> None:
        """Retry the currently tracked pending transaction with the configured strategy."""
        if not self._tx_tracker.pending:
            raise RuntimeError("No active transaction to strategize")

        _active_copy = deepcopy(self._tx_tracker.pending)
        params_updated = False
        for strategy in self._strategies:
            try:
                params = strategy.execute(pending=_active_copy)
            except TransactionFaulted as e:
                self._tx_tracker.fault(fault_error=e)
                return
            if params:
                # in case the strategy accidentally returns None
                # keep the parameters as they are.
                _active_copy.params.update(params)
                params_updated = True

        if not params_updated:
            # mandatory default timeout strategy prevents this from being a forever wait
            self.log.info(
                f"[wait] strategies made no suggested updates to "
                f"pending tx #{_active_copy.id} - skipping retry round"
            )
            return

        # (!) retry the transaction with the new parameters
        _names = " -> ".join(s.name for s in self._strategies)

        try:
            txhash = self.__fire(tx=_active_copy, msg=_names)
        except InsufficientFunds as e:
            # special case
            self.log.error(
                f"[insufficient funds] transaction #atx-{_active_copy.id}|{_active_copy.params['nonce']} "
                f"failed because of insufficient funds - {e}"
            )
            # get hook from actual pending object (not a deep copy)
            hook = self._tx_tracker.pending.on_insufficient_funds
            fire_hook(hook, _active_copy, e)
            return
        except Exception as e:
            self._tx_tracker.update_active_after_failed_strategy_update(_active_copy)
            self.__handle_retry_failure(_active_copy, e)
            return

        _active_copy.txhash = txhash
        self._tx_tracker.update_active_after_successful_strategy_update(_active_copy)

        pending_tx = self._tx_tracker.pending
        self.log.info(
            f"[retry] transaction #{pending_tx.id} has been re-broadcasted with updated params"
        )
        if pending_tx.on_broadcast:
            fire_hook(hook=pending_tx.on_broadcast, tx=pending_tx)

    def __handle_retry_failure(self, attempted_tx: PendingTx, e: Exception):
        self.log.warn(
            f"[retry] transaction #atx-{attempted_tx.id}|{attempted_tx.params['nonce']} "
            f"failed with updated params - {str(e)}; retry again next round"
        )

        if attempted_tx.retries >= self._MAX_RETRY_ATTEMPTS:
            self.log.error(
                f"[retry] transaction #atx-{attempted_tx.id}|{attempted_tx.params['nonce']} "
                f"failed for { attempted_tx.retries} attempts; tx will no longer be retried"
            )

            fault_error = TransactionFaulted(
                tx=attempted_tx,
                fault=Fault.ERROR,
                message=str(e),
            )
            self._tx_tracker.fault(fault_error=fault_error)

    def __broadcast(self):
        """
        Attempts to broadcast the next `FutureTx` in the queue.
        If the broadcast is not successful, it is re-queued.
        """
        future_tx = self._tx_tracker.head()  # don't pop until successful
        future_tx.params = _make_tx_params(future_tx.params)

        # update nonce as necessary
        signer = self.__get_signer(future_tx._from)
        nonce = self.w3.eth.get_transaction_count(signer.address, "latest")
        if nonce > future_tx.params["nonce"]:
            self.log.warn(
                f"[broadcast] nonce {future_tx.params['nonce']} has been front-run "
                f"by another transaction. Updating queued tx "
                f"nonce {future_tx.params['nonce']} -> {nonce}"
            )
        future_tx.params["nonce"] = nonce

        try:
            txhash = self.__fire(tx=future_tx, msg="broadcast")
        except InsufficientFunds as e:
            # special case
            self.log.error(
                f"[insufficient funds] transaction #atx-{future_tx.id}|{future_tx.params['nonce']} "
                f"failed because of insufficient funds - {e}"
            )
            fire_hook(future_tx.on_insufficient_funds, future_tx, e)
            return
        except Exception as e:
            # notify user of failure and have them decide
            self.__handle_broadcast_failure(future_tx, e)
            return

        self._tx_tracker.morph(tx=future_tx, txhash=txhash)
        pending_tx = self._tx_tracker.pending
        if pending_tx.on_broadcast:
            fire_hook(hook=pending_tx.on_broadcast, tx=pending_tx)

    def __handle_broadcast_failure(self, future_tx: FutureTx, e: Exception):
        is_broadcast_failure = False
        if _is_recoverable_send_tx_error(e):
            if future_tx.retries >= self._MAX_RETRY_ATTEMPTS:
                is_broadcast_failure = True
                self.log.error(
                    f"[broadcast] transaction #atx-{future_tx.id}|{future_tx.params['nonce']} "
                    f"failed after {future_tx.retries} retry attempts"
                )
            else:
                self.log.warn(
                    f"[broadcast] transaction #atx-{future_tx.id}|{future_tx.params['nonce']} "
                    f"failed - {e}; tx will be retried"
                )
                self._tx_tracker.increment_broadcast_retries(future_tx)
        else:
            # non-recoverable
            is_broadcast_failure = True
            self.log.error(
                f"[broadcast] transaction #atx-{future_tx.id}|{future_tx.params['nonce']} "
                f"has non-recoverable failure - {e}"
            )

        if is_broadcast_failure:
            # since reporting failure; reset retry count
            self._tx_tracker.reset_broadcast_retries(future_tx)

            fire_hook(future_tx.on_broadcast_failure, future_tx, e)

    #
    # Monitoring
    #
    def __monitor_finalized(self) -> None:
        """Follow-up on finalized transactions for a little while."""
        if not self._tx_tracker.finalized:
            return

        current_block = self.w3.eth.block_number
        for tx in self._tx_tracker.finalized.copy():
            confirmations = current_block - tx.block_number
            if confirmations >= self._TRACKING_CONFIRMATIONS:
                if tx in self._tx_tracker.finalized:
                    self._tx_tracker.finalized.remove(tx)
                    self.log.info(
                        f"[monitor] stopped tracking {tx.txhash.hex()} after {confirmations} confirmations"
                    )
                continue
            self.log.info(
                f"[monitor] transaction {tx.txhash.hex()} has {confirmations} confirmations"
            )

    #
    # Exposed functions
    #
    def pause(self) -> None:
        """
        Pause the machine's tx processing loop; no txs are processed until unpaused (resume()).
        """
        if not self._pause:
            self._pause = True
            self.log.info("[pause] pause mode requested")
            self._cycle_state()  # force a move to PAUSED state (don't wait for next iteration)

    def resume(self) -> None:
        """Unpauses (resumes) the machine's tx processing loop."""
        if self._pause:
            self._pause = False
            self.log.info("[pause] pause mode deactivated")
            self._wake()

    def queue_transaction(
        self, params: TxParams, signer: LocalAccount, *args, **kwargs
    ) -> FutureTx:
        """
        Queue a new transaction for broadcast and subsequent tracking.
        Optionally provide a dictionary of additional string data
        to log during the transaction's lifecycle for identification.
        """
        previously_busy_or_paused = self._busy or self._pause

        if signer.address not in self.signers:
            self.signers[signer.address] = signer

        params_copy = copy(params)

        from_param = params_copy.get("from")
        if from_param is None:
            params_copy["from"] = signer.address
        if from_param and from_param != signer.address:
            raise ValueError(
                f"Mismatched 'from' value ({from_param}) and 'signer' account ({signer.address})"
            )

        tx = self._tx_tracker.queue_tx(params=params_copy, *args, **kwargs)
        if not previously_busy_or_paused:
            self._wake()

        return tx

    def remove_queued_transaction(self, tx: FutureTx):
        """
        Removes a queued transaction; useful when an existing queued transaction has broadcast
        failures, or a queued transaction is no longer necessary.

        Returns true if transaction was present and removed, false otherwise.
        """
        return self._tx_tracker.remove_queued_tx(tx)
