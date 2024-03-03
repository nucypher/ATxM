import contextlib
from typing import Callable, Optional, Union

from cytoolz import memoize
from twisted.internet import reactor
from web3 import Web3
from web3.exceptions import TransactionNotFound
from web3.types import TxData, TxParams
from web3.types import RPCError, TxReceipt, Wei

from atxm.exceptions import (
    InsufficientFunds,
    TransactionReverted,
)
from atxm.logging import log
from atxm.tx import AsyncTx, FinalizedTx, FutureTx, PendingTx, TxHash


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


def _log_gas_weather(base_fee: Wei, suggested_tip: Wei) -> None:
    base_fee_gwei = Web3.from_wei(base_fee, "gwei")
    tip_gwei = Web3.from_wei(suggested_tip, "gwei")
    log.info(
        f"[gas] Gas conditions: base {base_fee_gwei} gwei | max tip {tip_gwei} gwei"
    )


def _get_receipt_from_txhash(w3: Web3, txhash: TxHash) -> Optional[TxReceipt]:
    try:
        receipt = w3.eth.get_transaction_receipt(txhash)
    except TransactionNotFound:
        return
    return receipt


def _get_receipt(w3: Web3, pending_tx: PendingTx) -> Optional[TxReceipt]:
    """
    Hits eth_getTransaction and eth_getTransactionReceipt
    for the active pending txhash and checks if
    it has been finalized or reverted.

    Returns the receipt if the transaction has been finalized.
    NOTE: Performs state changes
    """
    try:
        txdata = w3.eth.get_transaction(pending_tx.txhash)
        pending_tx.data = txdata
    except TransactionNotFound:
        log.error(f"[error] Transaction {pending_tx.txhash.hex()} not found")
        return

    receipt = _get_receipt_from_txhash(w3=w3, txhash=txdata["hash"])
    if not receipt:
        return

    status = receipt.get("status")
    if status == 0:
        # If status in response equals 1 the transaction was successful.
        # If it is equals 0 the transaction was reverted by EVM.
        # https://web3py.readthedocs.io/en/stable/web3.eth.html#web3.eth.Eth.get_transaction_receipt
        log.warn(
            f"[reverted] Transaction {txdata['hash'].hex()} was reverted by EVM with status {status}"
        )
        raise TransactionReverted(
            tx=pending_tx, receipt=receipt, message=f"Reverted with EVM status {status}"
        )

    log.info(
        f"[accepted] Transaction {txdata['nonce']}|{txdata['hash'].hex()} "
        f"has been included in block #{txdata['blockNumber']}"
    )
    return receipt


def _get_confirmations(w3: Web3, tx: Union[PendingTx, FinalizedTx]) -> int:
    current_block = w3.eth.block_number
    tx_receipt = _get_receipt_from_txhash(w3=w3, txhash=tx.txhash)
    if not tx_receipt:
        log.info(f"Transaction {tx.txhash.hex()} is pending or unconfirmed")
        return 0

    tx_block = tx_receipt["blockNumber"]
    confirmations = current_block - tx_block
    return confirmations


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
                log.warn(f"[hook] raised {e}")

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
        # legacy
        params["type"] = "0x01"
        params["gasPrice"] = data["gasPrice"]
    elif "maxFeePerGas" in data:
        # EIP-1559
        params["type"] = "0x02"
        params["maxFeePerGas"] = data["maxFeePerGas"]
        params["maxPriorityFeePerGas"] = data["maxPriorityFeePerGas"]
    else:
        raise ValueError(f"unrecognized tx data: {data}")

    return params
