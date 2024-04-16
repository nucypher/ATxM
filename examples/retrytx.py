import os
import sys
from typing import Union

from eth_account import Account
from twisted.internet import reactor
from twisted.logger import globalLogPublisher, textFileLogObserver
from web3 import Web3, HTTPProvider
from web3.middleware import geth_poa_middleware

from atxm.exceptions import InsufficientFunds
from atxm.main import AutomaticTxMachine
from atxm.tx import FaultedTx, FutureTx, PendingTx, FinalizedTx

#
# Configuration
#

observer = textFileLogObserver(sys.stdout)
globalLogPublisher.addObserver(observer)

CHAIN_ID = 80002

ENDPOINT = os.environ["WEB3_PROVIDER_URI"]

PRIVATE_KEY = os.environ["PRIVATE_KEY"]

#
# Setup
#

account = Account.from_key(PRIVATE_KEY)
provider = HTTPProvider(endpoint_uri=ENDPOINT)

w3 = Web3(provider)
w3.middleware_onion.inject(geth_poa_middleware, layer=0)

#
# Prepare Transaction
#

nonce = w3.eth.get_transaction_count(account.address, "pending")

# Legacy transaction
gas_price = w3.eth.gas_price
legacy_transaction = {
    "chainId": CHAIN_ID,
    "nonce": nonce,
    "to": account.address,
    "value": 0,
    "gas": 21000,
    "gasPrice": gas_price,
    "data": b"",
}

# EIP-1559 transaction
base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
tip = w3.eth.max_priority_fee
transaction_eip1559 = {
    "chainId": CHAIN_ID,
    "nonce": nonce + 1,
    "to": account.address,
    "value": 0,
    "gas": 21000,
    "maxPriorityFeePerGas": tip,
    "maxFeePerGas": base_fee + tip,
    "data": b"",
}

#
# Define Hooks
#


def on_broadcast(tx: PendingTx):
    txhash = tx.txhash.hex()
    print(f"[broadcast] Transaction ({tx.id}) has been broadcasted ({txhash})!")


def on_broadcast_failure(tx: FutureTx, e: Exception):
    print(
        f"[broadcast_failed] Transaction ({tx.id}) failed to broadcast {tx.id} with exception {e}"
    )


def on_finalized(tx: FinalizedTx):
    txhash = tx.receipt["transactionHash"].hex()
    if tx.successful:
        print(f"[success] Transaction ({tx.id}) has been finalized ({txhash})!")
    else:
        print(
            f"[failure] Transaction ({tx.id}) has been finalized ({txhash}) with status {tx.receipt['status']}!"
        )


def on_fault(tx: FaultedTx):
    print(f"[fault] Transaction ({tx.id}) has faulted with error {tx.fault.name}!")


def on_insufficient_funds(tx: Union[FutureTx, PendingTx], e: InsufficientFunds):
    print(f"[insufficient_funds] Transaction ({tx.id}) has insufficient funds - {e}!")


#
# Queue Transaction(s)
#

machine = AutomaticTxMachine(w3=w3)
_future_txs = machine.queue_transactions(
    # required
    signer=account,
    params=[legacy_transaction, transaction_eip1559],
    on_broadcast_failure=on_broadcast_failure,
    on_fault=on_fault,
    on_finalized=on_finalized,
    on_insufficient_funds=on_insufficient_funds,
    # optional
    info={"message": "something wonderful is happening..."},
    on_broadcast=on_broadcast,
)

reactor.run()
