import os
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

import pytest
from eth_account import Account
from eth_tester import EthereumTester
from hexbytes import HexBytes
from statemachine import State
from twisted.internet.task import Clock
from twisted.logger import globalLogPublisher, textFileLogObserver
from web3.types import TxReceipt

from atxm import AutomaticTxMachine
from atxm.logging import log
from atxm.strategies import ExponentialSpeedupStrategy

observer = textFileLogObserver(sys.stdout)
globalLogPublisher.addObserver(observer)

log.debug("Running tests")


@pytest.fixture
def account(accounts):
    _account = accounts[0]
    return Account.from_key(_account.private_key)


@pytest.fixture
def legacy_transaction(account, w3):
    gas_price = w3.eth.gas_price
    params = {
        "chainId": 1337,
        "nonce": 0,
        "to": account.address,
        "value": 0,
        "gas": 21000,
        "gasPrice": gas_price,
        "data": b"",
    }
    return params


@pytest.fixture
def eip1559_transaction(account, w3):
    base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    tip = w3.eth.max_priority_fee
    params = {
        "chainId": 1337,
        "nonce": 0,
        "to": account.address,
        "value": 0,
        "gas": 21000,
        "maxPriorityFeePerGas": tip,
        "maxFeePerGas": base_fee + tip,
        "data": b"",
    }
    return params


@pytest.fixture
def w3(networks):
    return networks.provider.web3


@pytest.fixture
def strategies(w3):
    _strategy_classes = [
        ExponentialSpeedupStrategy,
    ]
    _strategies = [s(w3) for s in _strategy_classes]
    return _strategies


@pytest.fixture
def machine(w3, strategies):
    clock = Clock()
    _machine = AutomaticTxMachine(w3=w3, strategies=strategies)
    _machine._task.clock = clock
    yield _machine

    _machine.stop()


@pytest.fixture
def clock(machine):
    return machine._task.clock


@pytest.fixture
def interval(machine):
    return 1


@pytest.fixture
def mock_wake_sleep(machine, mocker):
    wake = mocker.patch.object(machine, "_wake")
    sleep = mocker.patch.object(machine, "_sleep")
    return wake, sleep


@pytest.fixture
def disable_auto_mining(ethereum_tester):
    ethereum_tester.disable_auto_mine_transactions()
    yield
    ethereum_tester.enable_auto_mine_transactions()


@pytest.fixture
def ethereum_tester(w3) -> EthereumTester:
    return w3.provider.ethereum_tester


class StateObserver:
    def __init__(self):
        self.transitions: List[Tuple[State, State]] = []

    def on_transition(self, source, target):
        if source.id != target.id:
            self.transitions.append((source, target))


@pytest.fixture
def state_observer(machine):
    _observer = StateObserver()
    machine.add_listener(_observer)

    return _observer


@pytest.fixture
def tx_receipt():
    _tx_receipt = TxReceipt(
        {
            "blockHash": HexBytes(
                "0xf8be31c3eecd1f58432b211e906463b97c3cbfbe60c947c8700dff0ae7348299"
            ),
            "blockNumber": 1,
            "contractAddress": None,
            "cumulativeGasUsed": 21000,
            "effectiveGasPrice": 1875000000,
            "from": "0x1e59ce931B4CFea3fe4B875411e280e173cB7A9C",
            "gasUsed": 21000,
            "logs": [],
            "root": "0x01",
            "status": 1,
            "to": "0x1e59ce931B4CFea3fe4B875411e280e173cB7A9C",
            "transactionHash": HexBytes(
                "0x4798799f1cf30337b72381434d3ff56c43ee1fdfa1f812b8262069b7fb2f5a95"
            ),
            "transactionIndex": 0,
            "type": 2,
        }
    )

    return _tx_receipt


@pytest.fixture
def tempfile_path():
    fd, path = tempfile.mkstemp()
    path = Path(path)
    yield path
    os.close(fd)
    path.unlink()
