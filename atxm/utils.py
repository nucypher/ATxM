import contextlib
from typing import Callable, Optional, Union

from cytoolz import memoize
from twisted.internet import reactor
from web3 import Web3
from web3.exceptions import TransactionNotFound
from web3.types import PendingTx as PendingTxData, TxData, TxParams
from web3.types import RPCError, TxReceipt, Wei

from atxm.exceptions import (
    InsufficientFunds,
)
from atxm.logging import log
from atxm.tx import AsyncTx, FutureTx, TxHash


@memoize
def _get_average_blocktime(w3: Web3, sample_size: int) -> float:
    """Returns the average block time in seconds."""
    latest_block = w3.eth.get_block("latest")
    if latest_block.number == 0:
        return 0
    sample_block_number = latest_block.number - sample_size
    if sample_block_number <= 0:
        return 0
    base_block = w3.eth.get_block(sample_block_number)
    delta = latest_block.timestamp - base_block.timestamp
    average_block_time = delta / sample_size
    return average_block_time


def _log_gas_weather(base_fee: Wei, tip: Wei) -> None:
    base_fee_gwei = Web3.from_wei(base_fee, "gwei")
    tip_gwei = Web3.from_wei(tip, "gwei")
    log.info(f"Gas conditions: base {base_fee_gwei} gwei | tip {tip_gwei} gwei")


def _get_receipt(w3: Web3, txhash: TxHash) -> Optional[TxReceipt]:
    try:
        receipt = w3.eth.get_transaction_receipt(txhash)
    except TransactionNotFound:
        return
    return receipt


def fire_hook(hook: Callable, tx: AsyncTx, *args, **kwargs) -> None:
    """
    Fire a callable in a separate thread.
    Try exceptionally hard not to crash the async tasks during dispatch.
    """
    with contextlib.suppress(Exception):

        def _hook() -> None:
            """I'm inside a thread!"""
            try:
                hook(tx, *args, **kwargs)
            except Exception as e:
                log.warn(f"[hook] {e}")

        reactor.callInThread(_hook)
        log.info(f"[hook] fired hook {hook} for transaction #atx-{tx.id}")


def _handle_rpc_error(e: Exception, tx: FutureTx) -> None:
    try:
        error = RPCError(**e.args[0])
    except TypeError:
        log.critical(
            f"[error] transaction #atx-{tx.id}|{tx.params['nonce']} failed with {e}"
        )
    else:
        log.critical(
            f"[error] transaction #atx-{tx.id}|{tx.params['nonce']} failed with {error['code']} | {error['message']}"
        )
        if error["code"] == -32000:
            if "insufficient funds" in error["message"]:
                raise InsufficientFunds
        hook = tx.on_fault
        if hook:
            fire_hook(hook=hook, tx=tx, error=e)


def _make_tx_params(data: TxData) -> TxParams:
    """
    TxData -> TxParams: Creates a transaction parameters
    object from a transaction data object for broadcast.

    This operation is performed in order to "turnaround" the transaction
    data object as queried from the RPC provider (eth_getTransaction) into a transaction
    parameters object for strategics and re-broadcast (LocalAccount.sign_transaction).
    """
    params = TxParams(
        {
            "nonce": data["nonce"],
            "chainId": data["chainId"],
            "gas": data["gas"],
            "to": data["to"],
            "value": data["value"],
            "data": data.get("data", b""),
        }
    )
    if "gasPrice" in data:
        params["type"] = "0x01"
        params["gasPrice"] = data["gasPrice"]
    elif "maxFeePerGas" in data:
        params["type"] = "0x02"
        params["maxFeePerGas"] = data["maxFeePerGas"]
        params["maxPriorityFeePerGas"] = data["maxPriorityFeePerGas"]
    return params
