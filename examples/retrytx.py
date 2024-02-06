from eth_account import Account
from hexbytes import HexBytes
from twisted.internet import reactor
from web3 import Web3, HTTPProvider
from web3.middleware import geth_poa_middleware

from atxm.main import AutomaticTxMachine
from atxm.tx import PendingTx, FinalizedTx

#
# Configuration
#

CHAIN_ID = 80001

# ENDPOINT = os.environ["WEB3_PROVIDER_URI"]
ENDPOINT = "https://polygon-mumbai.infura.io/v3/a11313ddcf61443898b6a47e952d255c"

# PRIVATE_KEY = os.environ["PRIVATE_KEY"]
PRIVATE_KEY = HexBytes(
    "0x900edb9e8214b2353f82aa195e915128f419a92cfb8bbc0f4784f10ef4112b86"
)

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

nonce = w3.eth.get_transaction_count(account.address, 'pending')

# Legacy transaction
gas_price = w3.eth.gas_price
legacy_transaction = {
    'chainId': CHAIN_ID,
    'nonce': nonce,
    'to': account.address,
    'value': 0,
    'gas': 21000,
    'gasPrice': gas_price,
    'data': b'',
}

# EIP-1559 transaction
base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
tip = w3.eth.max_priority_fee
transaction_eip1559 = {
    'chainId': CHAIN_ID,
    'nonce': nonce + 1,
    'to': account.address,
    'value': 0,
    'gas': 21000,
    'maxPriorityFeePerGas': tip,
    'maxFeePerGas': base_fee + tip,
    'data': b'',
}

#
# Define Hooks (optional)
#


def on_broadcast(tx: PendingTx):
    txhash = tx.txhash.hex()
    print(f"[alert] Transaction has been broadcasted ({txhash})!")
    print(f"View on PolygonScan: https://mumbai.polygonscan.com/tx/{txhash}")


def on_transaction_finalized(tx: FinalizedTx):
    txhash = tx.receipt['transactionHash'].hex()
    mumbai_polygonscan = f"https://mumbai.polygonscan.com/tx/{txhash}"
    print(f"[alert] Transaction has been finalized ({txhash})!")
    print(f"View on PolygonScan: {mumbai_polygonscan}")


def on_transaction_capped(tx: PendingTx):
    txhash = tx.txhash.hex()
    print(f"[alert] Transaction has been capped ({txhash})!")


def on_transaction_timeout(tx: PendingTx):
    txhash = tx.txhash.hex()
    print(f"[alert] Transaction has timed out ({txhash})!")


def on_transaction_reverted(tx: FinalizedTx):
    txhash = tx.receipt['transactionHash'].hex()
    print(f"[alert] Transaction reverted ({txhash})!")


def on_error(tx: PendingTx, error: Exception):
    print(f"[alert] Transaction #{tx.id} has errored ({error})!")


#
# Queue Transaction(s)
#

machine = AutomaticTxMachine(w3=w3)
_future_txs = machine.queue_transactions(

    # required
    params=[
        legacy_transaction,
        transaction_eip1559
    ],
    signer=account,

    # optional
    info={"message": f"something wonderful is happening..."},
    on_broadcast=on_broadcast,
    on_revert=on_transaction_reverted,
    on_finalized=on_transaction_finalized,
    on_capped=on_transaction_capped,
    on_timeout=on_transaction_timeout
)

reactor.run()
