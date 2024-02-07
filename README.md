Automatic Tx Machine 🏧
========================

[![Tests](https://github.com/nucypher/ATxM/actions/workflows/pytest.yml/badge.svg)](https://github.com/nucypher/ATxM/actions/workflows/pytest.yml)

An async EVM transaction retry machine.
Programmatically queue, broadcast, and retrying (speedup) transactions.
Transactions are queued and broadcasted in a first-in-first-out (FIFO) order.

##### Throttle 

There are three rates, work (fast scan), idle (slow scan) and sleep (zero activity).
This reduces the use of resources when there are no transactions to track.
When a transaction is queued, the machine wakes up and goes to work.
When all work is complete it eases into idle mode and eventually goes to sleep.

##### Retry Strategies

The tracker supports a generic configurable interface for retry strategies (AsynxTxStrategy).
These strategies are used to determine when to retry a transaction and how to do so.
They can be configured to use any kind of custom context, like gas oracles.

##### Crash-Tolerance

Internal state is written to a file to help recover from crashes
and restarts. Internally caches LocalAccount instances in-memory which in
combination with disk i/o is used to recover from async task restarts.

##### Futures

The tracker returns a FutureTx instance when a transaction is queued.
The FutureTx instance can be used to track the transaction's lifecycle and
to retrieve the transaction's receipt when it is finalized.

##### Lifecycle Hooks 

Hooks are fired in a dedicated thread for lifecycle events.

- on_broadcast: When a transaction is broadcasted.
- on_finalized: When a transaction is finalized.
- on_pause: When a transaction is halted by the strategy.
- on_fault: When a transaction reverted or another error occurred.

##### Event Loop Support

Currently, the machine is designed to work Twisted.
However, it can be adapted to work with any event loop.
