Automatic Tx Machine 🏧
========================

An async EVM transaction retry machine.
Programmatically queue, broadcast, and retrying (speedup) transactions.
Transactions are queued and broadcasted in a first-in-first-out (FIFO) order.

##### Throttle 

There are two rates, idle (slow scan) and work (fast scan),
to reduce the use of network resources when there are no transactions to track.
When a transaction is queued, the tracker switches to work mode and
starts scanning for transaction receipts. When the queue is empty,
the tracker switches to idle mode and scans less frequently.

##### Retry Strategies

The tracker supports a generic configurable
interface for retry strategies.

##### Crash-Tolerance

Internal state is written to a file to help recover from crashes
and restarts. Internally caches LocalAccount instances in-memory which in
combination with disk i/o is used to recover from async task restarts.

##### Futures

The tracker returns a FutureTx instance when a transaction is queued.
The FutureTx instance can be used to track the transaction's lifecycle and
to retrieve the transaction's receipt when it is finalized.

##### Lifecycle Hooks 

The tracker provides hooks for transaction lifecycle events.

- on_queued: When a transaction is queued.
- on_broadcast: When a transaction is broadcasted.
- on_finalized: When a transaction is finalized.
- on_capped: When a transaction capped by its retry strategy.
- on_timeout: When a transaction times out.
- on_reverted: When a transaction reverted.
